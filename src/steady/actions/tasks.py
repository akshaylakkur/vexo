"""Tasks for OpenClaw: create, confirm, dispatch, track, deliver (SPEC 7.3-7.5).

Two paths, and only two:
  voice     the wearer asks for a message or an email -> needs their yes -> OpenClaw words it
  schedule  the end-of-day brief to the contact set up in config -> OpenClaw words it

    created --voice--> needs_confirmation --yes--> confirmed --> sent --> running --> done
       |                      | no / 10 min                  |                   \\-> failed
       \\--schedule-----------+------------------------------/ hook retried 3x

Steady decides the recipient and delivers; OpenClaw's model only writes the words. `send`
refuses anything that is not approved, and delivery re-checks the address allowlist.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field

from ..bus import Bus
from ..config import Config
from ..store.db import Writer
from .approval import CHANNEL, TIER_NAMES, TYPES, tier_for
from .contacts import Contact, Contacts
from .delivery import Delivered, Delivery
from .openclaw import HookError, OpenClawHook

log = logging.getLogger("steady.actions")
FINAL = ("done", "failed", "cancelled", "expired")
RETRY_S = (5, 20, 60)
MAX_SENDS_PER_TASK = 1
PLAIN_FALLBACK = ("daily_brief", "incident_report")     # if OpenClaw does not write it, Steady sends the plain facts


class TaskError(Exception):
    """A refusal with a reason a person (or a model) can act on."""


@dataclass
class Task:
    id: int
    type: str
    tier: int
    origin: str                       # voice | schedule
    status: str
    request: str
    recipient: Contact
    channels: list[str]
    created_at: float
    context: dict = field(default_factory=dict)
    conversation_id: int | None = None
    run_id: str | None = None
    attempts: int = 0
    deliveries: list[dict] = field(default_factory=list)
    sends: int = 0
    note: str = ""
    updated_at: float = 0.0

    @property
    def delivered(self) -> bool:
        return any(d["ok"] for d in self.deliveries)

    def public(self) -> dict:
        """For the voice agent and the bus: names, never addresses."""
        return {"id": self.id, "type": self.type, "tier": self.tier, "tier_name": TIER_NAMES[self.tier],
                "origin": self.origin, "status": self.status, "request": self.request,
                "recipient": self.recipient.public(), "channels": self.channels,
                "delivered": self.delivered, "note": self.note, "created_at": self.created_at,
                "updated_at": self.updated_at}


class TaskManager:
    def __init__(self, cfg: Config, bus: Bus, writer: Writer, hook: OpenClawHook | None = None,
                 delivery: Delivery | None = None, contacts: Contacts | None = None) -> None:
        self.cfg = cfg
        self.bus = bus
        self.writer = writer
        self.hook = hook or OpenClawHook(cfg)
        self._contacts = contacts
        self.delivery = delivery or Delivery(cfg, contacts)
        self.tasks: dict[int, Task] = {}
        self.confirm_expiry_s = float(cfg.get("openclaw.confirm_expiry_s", 600))
        self.timeout_s = float(cfg.get("openclaw.task_timeout_s", 600))
        self.brief_fallback_s = float(cfg.get("brief.fallback_s", 120))
        self.report_fallback_s = float(cfg.get("call.report_fallback_s", 45))
        self.max_per_hour = int(cfg.get("delivery.max_per_hour", 20))
        self._sent_times: list[float] = []
        self._dispatching: set[int] = set()

    @property
    def contacts(self) -> Contacts:
        return self._contacts if self._contacts is not None else Contacts.load()    # re-read: edits apply at once

    @property
    def wearer(self) -> str:
        return self.contacts.sender.name or "the wearer"

    # ---- persistence -------------------------------------------------------------------
    def _save(self, t: Task) -> None:
        t.updated_at = time.time()
        payload = {"request": t.request, "recipient": t.recipient.public(), "channels": t.channels,
                   "origin": t.origin, "deliveries": t.deliveries}
        self.writer.execute(
            "UPDATE tasks SET updated_at=?, status=?, payload_json=?, openclaw_run_id=?, result_note=?, attempts=? WHERE id=?",
            (t.updated_at, t.status, json.dumps(payload), t.run_id, t.note, t.attempts, t.id))
        self.bus.publish("task.updated", t.public())

    def _set(self, t: Task, status: str, note: str | None = None) -> None:
        log.info("task %d (%s): %s -> %s%s", t.id, t.type, t.status, status, f" ({note})" if note else "")
        t.status = status
        if note is not None:
            t.note = note[:500]
        self._save(t)

    async def start(self) -> None:
        """Tasks left unfinished by an earlier run are not resumed: mark them expired."""
        self.writer.execute("UPDATE tasks SET status='expired', result_note='steadyd restarted' "
                            "WHERE status NOT IN ('done','failed','cancelled','expired')")
        await self.writer.flush()

    # ---- create / confirm --------------------------------------------------------------
    async def create(self, task_type: str, request: str, to: str | None = None, origin: str = "voice",
                     context: dict | None = None, conversation_id: int | None = None,
                     channels: list[str] | None = None) -> dict:
        if task_type not in TYPES:
            raise TaskError(f"Unknown task type {task_type!r}. Use one of: {', '.join(TYPES)}.")
        try:
            tier = tier_for(task_type, origin)
        except ValueError as e:
            raise TaskError(str(e)) from None
        book = self.contacts
        recipient = book.find(to)
        if recipient is None:
            names = ", ".join(f"{c.name} ({c.role})" for c in book.items) or "none set up"
            raise TaskError(f"There is no approved contact called {to!r}. Approved contacts: {names}.")
        wanted = CHANNEL.get(task_type) or channels or ["imessage"]
        usable = [c for c in wanted if recipient.address(c)]
        if not usable:
            what = "phone number" if wanted == ["imessage"] else "email address" if wanted == ["email"] else "address"
            raise TaskError(f"{recipient.name} has no approved {what} set up.")
        status = "needs_confirmation" if tier == 1 else "confirmed"
        now = time.time()
        payload = {"request": request[:1000], "recipient": recipient.public(), "channels": usable,
                   "origin": origin, "deliveries": []}
        task_id = await self.writer.insert_returning_id("tasks", {
            "created_at": now, "updated_at": now, "type": task_type, "tier": tier, "urgency": "normal",
            "status": status, "payload_json": json.dumps(payload), "conversation_id": conversation_id})
        t = Task(task_id, task_type, tier, origin, status, request[:1000], recipient, usable, now, context or {},
                 conversation_id, updated_at=now)
        self.tasks[task_id] = t
        log.info("task %d created: %s to %s by %s, tier %d (%s)", task_id, task_type, recipient.name,
                 "/".join(usable), tier, TIER_NAMES[tier])
        self.bus.publish("task.updated", t.public())
        out = t.public()
        out["needs_confirmation"] = tier == 1
        if tier == 1:
            how = {"imessage": "by text message", "email": "by email"}[usable[0]]
            out["confirm_prompt"] = (f"Send this to {recipient.name} {how}? Ask the person, then call confirm_task. "
                                     "A single tap on the band means yes.")
        else:
            self._spawn_dispatch(t)
        return out

    async def confirm(self, task_id: int, confirmed: bool) -> dict:
        t = self._get(task_id)
        if t.status != "needs_confirmation":
            raise TaskError(f"Task {task_id} is {t.status}, not waiting for confirmation.")
        if not confirmed:
            self._set(t, "cancelled", "the person said no")
            return t.public()
        self._set(t, "confirmed", "the person said yes")
        self._spawn_dispatch(t)
        return t.public()

    def _get(self, task_id: int) -> Task:
        t = self.tasks.get(int(task_id))
        if t is None:
            raise TaskError(f"There is no task {task_id}.")
        return t

    # ---- dispatch to OpenClaw ----------------------------------------------------------
    def _spawn_dispatch(self, t: Task) -> None:
        if t.id not in self._dispatching:
            self._dispatching.add(t.id)
            asyncio.get_running_loop().create_task(self._dispatch(t), name=f"dispatch-{t.id}")

    async def _dispatch(self, t: Task) -> None:
        try:
            for i in range(len(RETRY_S) + 1):
                if t.status in FINAL:
                    return
                t.attempts += 1
                try:
                    t.run_id = await self.hook.dispatch(t.id, t.type)
                    self._set(t, "sent", f"OpenClaw run {t.run_id}" if t.run_id else "handed to OpenClaw")
                    return
                except HookError as e:
                    log.warning("task %d: %s (attempt %d)", t.id, e, t.attempts)
                    t.note = str(e)
                    if i < len(RETRY_S):
                        await asyncio.sleep(RETRY_S[i])
            if t.type in PLAIN_FALLBACK:
                await self._plain_brief(t, "OpenClaw could not be reached")
            else:
                self._set(t, "failed", f"could not reach OpenClaw: {t.note}")
        finally:
            self._dispatching.discard(t.id)

    # ---- what OpenClaw calls (through steadyctl and the local API) ---------------------
    async def claim(self, task_id: int) -> dict:
        """The task as OpenClaw's agent needs it. Refuses unapproved and finished tasks."""
        t = self._get(task_id)
        if t.status == "needs_confirmation":
            raise TaskError("This task has not been approved by the wearer. Do nothing and stop.")
        if t.status in FINAL:
            raise TaskError(f"This task is already {t.status}. Do nothing more.")
        if t.status in ("confirmed", "sent"):
            self._set(t, "running", "OpenClaw is working on it")
        return {"task": t.id, "type": t.type, "approved": True, "wearer": self.wearer, "request": t.request,
                "recipient": {"name": t.recipient.name, "role": t.recipient.role},
                "goes_out_as": [{"imessage": "a text message", "email": "an email"}[c] for c in t.channels],
                "context": t.context, "instructions": self._instructions(t)}

    def _instructions(self, t: Task) -> str:
        name, me = t.recipient.name, self.wearer
        email = "email" in t.channels
        send = (f"steadyctl task send {t.id} --subject \"...\" --text \"...\"" if email
                else f"steadyctl task send {t.id} --text \"...\"")
        common = (f" Send it once with `{send}`, then run `steadyctl task done {t.id} --note \"what you did\"`. "
                  f"If you cannot do it, run `steadyctl task fail {t.id} --note \"why\"`. It goes out from {me}'s own "
                  "account, so write nothing they would not say. Never diagnose or give medical advice.")
        third = f" Refer to {me} by name; do not use he or she."
        if t.type == "message_contact":
            return (f"Write the text message {me} asked for, to {name}: first person as {me}, their meaning and tone, "
                    "one to three sentences, no sign-off." + common)
        if t.type == "email_contact":
            return (f"Write the email {me} asked for, to {name}: first person as {me}, a clear subject, a short body, "
                    f"signed with {me}'s name. If they asked for numbers, use only those in `context`, exactly." + common)
        both = len(t.channels) > 1
        if t.type == "incident_report":
            return (f"Write a short report for {name}, who looks after {me}, about what just happened. Use only the "
                    "facts in `context` (`what_happened`, `phone_call`, `someone_is_checking`, `at`), inventing nothing: "
                    "what the wristband detected and when, that Steady checked by voice, and how the phone call went. "
                    "Plain, calm words, under 500 characters, with a subject. "
                    f"Start with 'Steady report for {me}'." + third + common)
        return (f"Write today's brief about {me} for {name}, who looks after them. Use only the facts in `context` "
                "(`summary_text`, `episodes`, `logged_today`), quoting numbers exactly and inventing nothing. Plain, "
                "warm words; say what the band saw and what was noted, not what it means. "
                + ("It goes out as a text message and as an email: keep it under 600 characters and give a subject. "
                   if both else "Keep it under 600 characters. " if not email else "A few short paragraphs. ")
                + f"Start with 'Steady brief for {me}'." + third + common)

    async def send(self, task_id: int, text: str, subject: str | None = None) -> dict:
        t = self._get(task_id)
        if t.status not in ("confirmed", "sent", "running"):
            raise TaskError(f"Task {task_id} is {t.status}: nothing may be sent for it.")
        if t.sends >= MAX_SENDS_PER_TASK:
            raise TaskError("This task has already sent its message. Do not send again; finish the task.")
        now = time.time()
        self._sent_times = [s for s in self._sent_times if now - s < 3600]
        if len(self._sent_times) >= self.max_per_hour:
            raise TaskError("Steady has sent the most messages it may send this hour. Try later.")
        return await self._deliver(t, text, subject, by="openclaw")

    async def _deliver(self, t: Task, text: str, subject: str | None, by: str) -> dict:
        t.sends += 1
        results: list[Delivered] = []
        for channel in t.channels:
            res = await self.delivery.send(t.recipient, channel, text, subject=subject, task_id=t.id)
            results.append(res)
            if res.ok:
                self._sent_times.append(time.time())
            entry = {"t": time.time(), "ok": res.ok, "channel": channel, "by": by, "detail": res.detail,
                     "to": t.recipient.name, "subject": subject if channel == "email" else None,
                     "text": text.strip()[:6000]}
            t.deliveries.append(entry)
            self.writer.insert("deliveries", {"task_id": t.id, "t": entry["t"], "ok": int(res.ok), "channel": channel,
                                              "to_role": t.recipient.role, "by": by, "subject": entry["subject"],
                                              "text": entry["text"], "detail": res.detail})
        self._save(t)
        if not any(r.ok for r in results):
            t.sends -= 1                                           # nothing went out: it may be tried again
            raise TaskError("Delivery failed: " + "; ".join(f"{r.channel}: {r.detail}" for r in results))
        return {"ok": True, "to": t.recipient.name,
                "sent_as": [r.channel for r in results if r.ok],
                "not_sent": {r.channel: r.detail for r in results if not r.ok}}

    async def finish(self, task_id: int, ok: bool, note: str = "") -> dict:
        t = self._get(task_id)
        if t.status in FINAL:
            return t.public()                                    # idempotent
        if t.status == "needs_confirmation":
            raise TaskError("This task was never approved.")
        if ok and not t.delivered:
            raise TaskError("Nothing was delivered for this task yet. Send it with `steadyctl task send` first, "
                            "or report a failure.")
        self._set(t, "done" if ok else "failed", note or ("finished" if ok else "failed"))
        return t.public()

    # ---- housekeeping ------------------------------------------------------------------
    async def watch(self) -> None:
        """Expire unanswered confirmations and time out stuck tasks. The brief has a plain fallback."""
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            for t in list(self.tasks.values()):
                if t.status in FINAL:
                    continue
                age = now - t.created_at
                if t.status == "needs_confirmation":
                    if age > self.confirm_expiry_s:
                        self._set(t, "expired", "not confirmed in time")
                elif t.type in PLAIN_FALLBACK and not t.delivered and t.id not in self._dispatching \
                        and age > (self.brief_fallback_s if t.type == "daily_brief" else self.report_fallback_s):
                    await self._plain_brief(t, f"OpenClaw had not written it after {self.brief_fallback_s:.0f} s")
                elif age > self.timeout_s:
                    if t.delivered:
                        self._set(t, "done", "delivered; OpenClaw did not report back")
                    else:
                        self._set(t, "failed", "OpenClaw did not finish in time")

    async def _plain_brief(self, t: Task, why: str) -> None:
        """The brief without a model: the day's numbers as plain text."""
        if t.type == "incident_report":
            title = f"Steady report for {self.wearer}"
            text = (f"{title}. {t.context.get('what_happened', '')} Phone call: {t.context.get('phone_call', 'not placed')}.")
        else:
            title = f"Steady brief for {self.wearer}"
            text = f"{title}. " + str(t.context.get("summary_text") or "No data today.")
        with contextlib.suppress(TaskError):
            await self._deliver(t, text, title, by="steady_plain")
        if t.delivered:
            self._set(t, "done", f"{why}; Steady sent the plain numbers itself")
        else:
            self._set(t, "failed", f"{why}, and the plain brief could not be delivered: "
                                   + "; ".join(d["detail"] for d in t.deliveries if not d["ok"]))
