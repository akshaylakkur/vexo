"""Short spoken messages outside a conversation: how a message or the brief ended.
Waits while a conversation is running, so it never talks over one."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("steady.voice")


class Announcer:
    def __init__(self, steady) -> None:
        self.steady = steady
        self._q: asyncio.Queue = asyncio.Queue(maxsize=20)
        self.spoken: list[str] = []

    def queue(self, text: str) -> bool:
        text = " ".join(text.split())[:300]
        if not text:
            return False
        try:
            self._q.put_nowait(text)
        except asyncio.QueueFull:
            log.warning("too many messages waiting to be spoken; dropped one")
            return False
        return True

    async def run(self) -> None:
        tq = self.steady.bus.subscribe("task.updated")
        outcomes = asyncio.get_running_loop().create_task(self._task_outcomes(tq))
        try:
            while True:
                text = await self._q.get()
                voice = self.steady.voice
                while voice.busy:                      # never talk over a conversation
                    await asyncio.sleep(0.5)
                try:
                    if voice.speaker is None:
                        from .speaker import Speaker
                        voice.speaker = Speaker.from_config(self.steady.cfg)
                    await voice.speaker.start()
                    await voice.speaker.say(text)
                    self.spoken.append(text)
                    log.info("announced: %s", text)
                except Exception as e:  # noqa: BLE001  no headphones / no network: log, keep going
                    log.warning("could not speak %r: %s", text, e)
        finally:
            outcomes.cancel()

    async def _task_outcomes(self, q: asyncio.Queue) -> None:
        """Tell the wearer how a task ended, in one sentence."""
        said: set[tuple[int, str]] = set()
        while True:
            _, t = await q.get()
            key = (t["id"], t["status"])
            if key in said or t["status"] not in ("done", "failed"):
                continue
            said.add(key)
            who = (t.get("recipient") or {}).get("name")
            if t["status"] == "done":
                text = {"message_contact": f"Your message to {who} has been sent.",
                        "email_contact": f"Your email to {who} has been sent.",
                        "daily_brief": f"Today's brief has been sent to {who}.",
                        "incident_report": f"I've also sent {who} a text and an email about it."}.get(t["type"], "That is done.")
            else:
                what = {"message_contact": "your message", "email_contact": "your email",
                        "daily_brief": "today's brief", "incident_report": "the report"}.get(t["type"], "that")
                text = f"Sorry, I couldn't send {what} to {who}."
            self.queue(text)
