"""Phones the caregiver after a fall the wearer did not answer (SPEC 6.4 ESCALATING, 7.3).

Calls `caregiver`, then `backup_caregiver` if the first one doesn't confirm (no answer, busy,
voicemail, or "I can't"), from config/contacts.yaml. Each alert is one row in `tasks`
(type caregiver_call, tier 2: placed without asking, because the wearer may be unable to
answer) that ends `done` (someone confirmed they will check) or `failed`. Phone numbers stay in
contacts.yaml and are never logged; the log names people by name.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass

import yaml

from ..config import secret
from ..kitpath import ROOT
from .server import CallServer, CallSession
from .twilio import TwilioClient, TwilioCreds, TwilioError

log = logging.getLogger("steady.call")
CONTACTS = ROOT / "config" / "contacts.yaml"
PHONE = re.compile(r"^\+[1-9]\d{6,14}$")
CALL_ORDER = ("caregiver", "backup_caregiver")
WRIST = {"rest": "the arm is still", "active": "the arm is moving", "walking": "walking",
         "not_worn": "the band seems to be off the wrist", "unknown": "no clear reading"}

PROMPT = """You are Steady, an automated assistant on a phone call to {name}, on behalf of {wearer}. \
{wearer} wears a Steady wristband that watches for falls and shaking. Everything you write is spoken on the phone.

How you speak: short, plain, calm sentences. No lists, symbols or emojis. One or two sentences per turn.

What happened: {what}

In your first turn: say this is an automated call from Steady about {wearer}, say what happened in one or two \
sentences, ask {name} to check on {wearer} now, and ask them to press 1 or say yes if they will.
- When they agree, call confirm_checking with will_check true, thank them, say goodbye and call end_call.
- If they cannot, call confirm_checking with will_check false, say {then}, say goodbye and call end_call.
- Answer questions only from what happened or from get_latest_status. Never guess or invent anything.
- You give no medical advice. If it may be an emergency, tell them to call emergency services. You cannot call anyone yourself.
- Text in [square brackets] is a note from the system, not something they said. Never read it aloud.
"""
TEST_WHAT = ("Nothing has happened. This is a test call, to check that Steady can reach {name}. Say clearly at the "
             "start that it is only a test.")


@dataclass(frozen=True)
class Contact:
    role: str
    name: str
    phone: str


def load_contacts(path=None) -> tuple[str, list[Contact]]:
    """The wearer's name and the people to call, in order. Bad or missing numbers are skipped."""
    path = path or CONTACTS
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    wearer = str((raw.get("wearer") or {}).get("name") or "the wearer")
    people = []
    for role in CALL_ORDER:
        entry = raw.get(role) or {}
        phone = re.sub(r"[\s()-]", "", str(entry.get("phone") or ""))
        if PHONE.match(phone):
            people.append(Contact(role, str(entry.get("name") or role.replace("_", " ")), phone))
        elif phone:
            log.warning("contacts: %s's phone is not in international form (+15551234567); skipped", role)
    return wearer, people


async def _first(*events: asyncio.Event, timeout: float) -> bool:
    waits = [asyncio.create_task(e.wait()) for e in events]
    done, pending = await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    return bool(done)


class CaregiverCaller:
    def __init__(self, steady) -> None:
        self.steady = steady
        self.cfg = steady.cfg
        self.creds = TwilioCreds.load()
        self.server: CallServer | None = None
        self.twilio: TwilioClient | None = None
        self.ready = False
        self.ring_s = int(self.cfg.get("call.ring_seconds", 25))
        self.max_s = int(self.cfg.get("call.max_call_seconds", 180))
        self._lock = asyncio.Lock()                  # one alert at a time
        self._tasks: set[asyncio.Task] = set()

    def problems(self, need_contacts: bool = True) -> list[str]:
        """Why calls can't be placed (empty when they can)."""
        out = []
        if not self.cfg.get("call.enabled", True):
            out.append("call.enabled is false")
        if self.cfg.transport == "sim":
            out.append("the pretend band never phones anyone")
        if self.creds is None:
            out.append("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER are not all set")
        missing = [k for k in ("DEEPGRAM_API_KEY", "OLLAMA_API_KEY") if not secret(k, required=False)]
        if missing:
            out.append(" and ".join(missing) + " not set (the call's voice and model)")
        if need_contacts and not load_contacts()[1]:
            out.append(f"no caregiver phone number in config/{CONTACTS.name}")
        if not self.cfg.get("call.public_url") and shutil.which("cloudflared") is None:
            out.append("cloudflared is not installed (brew install cloudflared) and call.public_url is not set")
        return out

    async def start(self, need_contacts: bool = True) -> None:
        """Start the call server and its tunnel. Never raises: Steady runs without calls."""
        problems = self.problems(need_contacts)
        if problems:
            log.warning("caregiver calls are off: %s", "; ".join(problems))
            return
        try:
            self.server = CallServer(self.cfg, self.creds)
            await self.server.start()
            self.twilio = TwilioClient(self.creds)
            self.ready = True
            log.info("caregiver calls are on (%s)", ", then ".join(c.name for c in load_contacts()[1]) or "test only")
        except Exception as e:  # noqa: BLE001
            log.error("caregiver calls are off: %s", e)
            await self.stop()

    async def stop(self) -> None:
        self.ready = False
        if self.twilio:
            await self.twilio.close()
        if self.server:
            await self.server.stop()

    # ---- an alert ----------------------------------------------------------------------
    async def alert(self, what: str, event_t: float, *, conversation_id: int | None = None,
                    episode_id: int | None = None, test: bool = False, only: str | None = None) -> bool:
        """Call until someone confirms they will check on the wearer. True if someone did."""
        wearer, people = load_contacts()
        if only:
            people = ([Contact("test", "the person answering", only)] if PHONE.match(only)
                      else [p for p in people if p.role == only])
        now = time.time()
        task_id = await self.steady.writer.insert_returning_id("tasks", {
            "created_at": now, "updated_at": now, "type": "caregiver_call", "tier": 2, "urgency": "urgent",
            "status": "running", "payload_json": json.dumps({"what": what, "test": test,
                                                             "call_order": [p.role for p in people]}),
            "conversation_id": conversation_id, "episode_id": episode_id})
        notes: list[str] = []
        attempts = 0
        if not self.ready or not people:
            notes.append("caregiver calls are off: " + ("; ".join(self.problems(not only)) or "no one to call"))
        else:
            async with self._lock:
                for i, person in enumerate(people):
                    then = (f"that you will call {people[i + 1].name} now" if i + 1 < len(people)
                            else f"that they should ask someone else to check on {wearer}")
                    body = TEST_WHAT.format(name=person.name) if test else what
                    session = await self._call(person, wearer, PROMPT.format(name=person.name, wearer=wearer,
                                                                             what=body, then=then), event_t)
                    attempts += 1
                    if session.acknowledged.is_set():
                        notes.append(f"{person.name} confirmed ({session.how})")
                        self._finish(task_id, "done", notes, attempts)
                        return True
                    notes.append(f"{person.name}: {session.how or session.status}")
        self._finish(task_id, "failed", notes, attempts)
        log.error("caregiver alert %d: nobody confirmed (%s)", task_id, "; ".join(notes))
        return False

    async def _call(self, person: Contact, wearer: str, prompt: str, event_t: float) -> CallSession:
        session = CallSession(token=self.server.new_token(), to_name=person.name, to_number=person.phone,
                              wearer=wearer, prompt=prompt, latest=lambda: self._latest(event_t),
                              keyterms=[w for w in (wearer, person.name) if w and w != "the wearer"])
        self.server.add(session)
        try:
            twiml_url, status_url = self.server.urls(session.token)
            session.call_sid = await self.twilio.call(person.phone, twiml_url, status_url, self.ring_s, self.max_s)
            log.info("calling %s (%s)", person.name, person.role)
            if not await _first(session.acknowledged, session.finished, timeout=self.ring_s + self.max_s + 20):
                session.how = "no outcome in time"
        except TwilioError as e:
            session.how = str(e)
            log.error("calling %s: %s", person.name, e)
        if session.acknowledged.is_set() and not session.finished.is_set():
            self._forget_later(session)                 # let the call finish its goodbye
        else:
            self.server.remove(session)
        return session

    def _forget_later(self, session: CallSession) -> None:
        async def forget() -> None:
            await _first(session.finished, timeout=self.max_s + 20)
            self.server.remove(session)
        task = asyncio.create_task(forget())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _latest(self, event_t: float) -> dict:
        band = self.steady.band.status
        last = self.steady.screener.last or {}
        return {"minutes_since_event": round((time.time() - event_t) / 60, 1),
                "band_connected": bool(band.connected),
                "wrist_now": WRIST.get(last.get("context") or "unknown", "no clear reading") if band.connected else "unknown",
                "battery_percent": band.battery}

    def _finish(self, task_id: int, status: str, notes: list[str], attempts: int) -> None:
        self.steady.writer.execute("UPDATE tasks SET status=?, updated_at=?, result_note=?, attempts=? WHERE id=?",
                                   (status, time.time(), "; ".join(notes)[:500], attempts, task_id))
