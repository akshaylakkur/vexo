"""The voice agent's tools (SPEC 6.5). The model only ever picks from these.

M4 has the tools that stay inside Steady. Tools that reach other people (request_task,
confirm_task) arrive with the OpenClaw backend in M6; until then the agent is told it
cannot contact anyone.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..store.db import connect

log = logging.getLogger("steady.agent")


@dataclass
class ConversationState:
    """What the tools and the conversation share."""
    id: int | None = None
    trigger: str = "double_tap"          # episode | double_tap | reminder
    episode: dict | None = None
    urgent: bool = False
    wellbeing: str | None = None         # ok | unsure | not_ok
    tapped_ok: bool = False
    ended: bool = False
    outcome: str | None = None
    summary: str = ""
    logs: list[dict] = field(default_factory=list)
    pending_task: int | None = None      # a task waiting for the person's yes
    user_turns: int = 0                  # how many times the person has spoken or tapped
    pending_since_turn: int = 0          # user_turns when the pending task was created


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


SPECS = [
    _fn("get_status", "What the wristband sees right now: what the arm is doing, any symptom episode in progress, "
        "and the last hour's totals.", {}, []),
    _fn("get_summary", "Symptom totals for a period, with numbers.",
        {"period": {"type": "string", "enum": ["last_hour", "today", "week"]}}, ["period"]),
    _fn("log_entry", "Write down something the person told you: medication taken, a symptom they describe, or a note.",
        {"kind": {"type": "string", "enum": ["medication", "symptom", "note"]},
         "details": {"type": "string", "description": "What they said, in a few words. For medication: name, dose and time if given."}},
        ["kind", "details"]),
    _fn("record_wellbeing", "Record how the person says they feel right now.",
        {"feeling": {"type": "string", "enum": ["ok", "unsure", "not_ok"]},
         "notes": {"type": "string"}}, ["feeling"]),
    _fn("list_contacts", "The people Steady can message or email for the wearer: names, roles and how they are reached.",
        {}, []),
    _fn("request_task", "Send a text message or an email to one of the wearer's approved contacts, in the wearer's "
        "name. It always needs the wearer's yes first: this returns a confirm_prompt to ask.",
        {"type": {"type": "string", "enum": ["message_contact", "email_contact"],
                  "description": "message_contact = a text message; email_contact = an email"},
         "to": {"type": "string", "description": "Who: an approved contact's name or role (see list_contacts)."},
         "request": {"type": "string", "description": "What to say, in the wearer's words, short."}},
        ["type", "to", "request"]),
    _fn("confirm_task", "Pass on the wearer's answer to a confirm_prompt. A single tap on the band means yes.",
        {"task_id": {"type": "integer"}, "confirmed": {"type": "boolean"}}, ["task_id", "confirmed"]),
    _fn("end_conversation", "End this conversation. Say goodbye first.",
        {"outcome": {"type": "string", "enum": ["ok", "follow_up", "needs_help"]},
         "summary": {"type": "string", "description": "One sentence: what happened and how the person is."}},
        ["outcome", "summary"]),
]


class Toolbox:
    def __init__(self, steady, state: ConversationState) -> None:
        self.steady = steady
        self.state = state
        self.calls: list[tuple[str, dict, dict]] = []

    @property
    def specs(self) -> list[dict]:
        return SPECS

    async def call(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            result: dict[str, Any] = {"error": f"There is no tool called {name}."}
        else:
            try:
                result = await handler(**args)
            except TypeError:
                result = {"error": "Wrong arguments for this tool."}
            except Exception as e:  # noqa: BLE001  a tool must never break the conversation
                log.exception("tool %s failed", name)
                result = {"error": f"The tool failed ({type(e).__name__})."}
        self.calls.append((name, args, result))
        return result

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = connect(self.steady.cfg.db_path, readonly=True)
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    async def _get_status(self) -> dict:
        sc = self.steady.screener
        last = sc.last or {}
        open_eps = [{"kind": e.kind, "for_seconds": round(e.last_positive_end - e.started_at),
                     "severity": e.severity} for e in sc.episodes.open.values()]
        return {
            "band_connected": self.steady.band.status.connected,
            "arm_is": last.get("context", "unknown"),
            "tremor_strength_deg_per_s": last.get("tremor_amp"),
            "episodes_in_progress": open_eps,
            "last_hour": (await self._get_summary("last_hour")),
        }

    async def _get_summary(self, period: str = "today") -> dict:
        from ..summary import summarize
        return await summarize(self.steady, period)

    def _tasks(self):
        tasks = getattr(self.steady, "tasks", None)
        if tasks is None:
            raise RuntimeError("tasks unavailable")
        return tasks

    async def _list_contacts(self) -> dict:
        try:
            return {"contacts": self._tasks().contacts.public()}
        except RuntimeError:
            return {"error": "Sending messages is not available right now."}

    async def _request_task(self, type: str, request: str, to: str | None = None, **_ignored) -> dict:
        from ..actions.tasks import TaskError
        if type not in ("message_contact", "email_contact"):
            return {"error": "You can only send a text message (message_contact) or an email (email_contact)."}
        try:
            s = self.state
            out = await self._tasks().create(type, request, to=to, origin="voice", conversation_id=s.id)
        except RuntimeError:
            return {"error": "Sending messages is not available right now. Tell the person you cannot do it."}
        except TaskError as e:
            return {"error": str(e)}
        self.state.pending_task = out["id"]
        self.state.pending_since_turn = self.state.user_turns
        return {"task_id": out["id"], "status": out["status"], "needs_confirmation": True,
                "recipient": out["recipient"]["name"], "confirm_prompt": out["confirm_prompt"]}

    async def _confirm_task(self, task_id: int, confirmed: bool) -> dict:
        from ..actions.tasks import TaskError
        s = self.state
        if confirmed and s.pending_task == int(task_id) and s.user_turns <= s.pending_since_turn:
            # a hard rule, not a prompt: a yes can only come from the person, after they were asked
            return {"error": "The person has not answered yet. Ask them, wait for their answer, then confirm."}
        try:
            out = await self._tasks().confirm(int(task_id), bool(confirmed))
        except (RuntimeError, ValueError):
            return {"error": "No such task."}
        except TaskError as e:
            return {"error": str(e)}
        self.state.pending_task = None
        return {"task_id": out["id"], "status": out["status"],
                "note": "Approved and started; it is on its way, not sent yet." if confirmed else "Cancelled."}

    async def _log_entry(self, kind: str, details: str) -> dict:
        if kind not in ("medication", "symptom", "note"):
            return {"error": "kind must be medication, symptom or note."}
        entry = {"kind": kind, "details": str(details)[:500]}
        self.state.logs.append(entry)
        self.steady.writer.insert("logs", {"t": time.time(), "kind": kind, "data_json": json.dumps({"details": entry["details"]}),
                                           "source": "voice", "conversation_id": self.state.id})
        return {"ok": True}

    async def _record_wellbeing(self, feeling: str, notes: str = "") -> dict:
        if feeling not in ("ok", "unsure", "not_ok"):
            return {"error": "feeling must be ok, unsure or not_ok."}
        self.state.wellbeing = feeling
        self.steady.writer.insert("logs", {"t": time.time(), "kind": "wellbeing",
                                           "data_json": json.dumps({"feeling": feeling, "notes": str(notes)[:500]}),
                                           "source": "voice", "conversation_id": self.state.id})
        return {"ok": True}

    async def _end_conversation(self, outcome: str, summary: str = "") -> dict:
        if outcome not in ("ok", "follow_up", "needs_help"):
            return {"error": "outcome must be ok, follow_up or needs_help."}
        s = self.state
        if outcome == "ok" and s.trigger == "episode" and not (s.wellbeing == "ok" or s.tapped_ok):
            return {"error": "Not yet: the person has not told you they are OK. Ask them, or end with "
                             "outcome follow_up or needs_help."}
        s.ended, s.outcome, s.summary = True, outcome, str(summary)[:500]
        return {"ok": True, "note": "The conversation ends after what you say now."}
