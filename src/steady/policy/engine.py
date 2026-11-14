"""Policy engine (SPEC 5): turns episodes and band gestures into conversations.

Listens to episode.started / updated / escalated (an episode is re-checked every window, so
a tremor that reaches 60 s opens a check-in at that moment) and to double-taps. The rules
decide; this opens the conversation, tells it what was held back earlier ("I also noticed"),
links the conversation to its episode, and phones the caregiver when a possible fall gets no
answer or the person says they need help.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..agent import prompts
from .rules import Decision, PolicyParams, Rules

log = logging.getLogger("steady.policy")
EPISODE_TOPICS = ("episode.started", "episode.updated", "episode.escalated")


class PolicyEngine:
    def __init__(self, steady) -> None:
        self.steady = steady
        self.rules = Rules(PolicyParams.from_config(steady.cfg))
        self.deferred: list[str] = []            # findings to mention in the next conversation
        self._tasks: set[asyncio.Task] = set()   # keeps running conversations referenced

    async def run(self) -> None:
        q = self.steady.bus.subscribe(*EPISODE_TOPICS, "band.event")
        while True:
            topic, payload = await q.get()
            if topic == "band.event":
                if payload.get("kind") == "double-tap" and not self.steady.voice.busy:
                    self._open(trigger="double_tap")
            else:
                self.consider(payload)

    def consider(self, ep: dict, now: float | None = None) -> Decision:
        voice = self.steady.voice
        last = self.steady.screener.last or {}
        d = self.rules.decide(ep, time.time() if now is None else now,
                              connected=self.steady.band.status.connected,
                              worn=last.get("context") != "not_worn", busy=voice.running)
        if d.speak:
            log.info("episode %s (%s, %s): check in now (%s)", ep.get("id"), ep.get("kind"), d.severity, d.reason)
            self._open(trigger="episode", episode=ep, urgent=d.severity == "urgent")
        elif d.defer:
            log.info("episode %s (%s, %s): held back, %s; will mention it next time",
                     ep.get("id"), ep.get("kind"), d.severity, d.reason)
            self.deferred.append(prompts.brief_episode(ep))
        elif d.severity in ("concern", "urgent") and d.reason != "already decided for this episode":
            log.info("episode %s (%s, %s): no check-in, %s", ep.get("id"), ep.get("kind"), d.severity, d.reason)
        return d

    def _open(self, trigger: str, episode: dict | None = None, urgent: bool = False) -> None:
        notes = []
        if urgent and self.steady.voice.running:
            notes.append(prompts.PREEMPTING)
        if self.deferred:
            notes.append(prompts.ALSO_NOTICED.format(what="; ".join(self.deferred[-5:])))
            self.deferred.clear()
        task = asyncio.create_task(self._converse(trigger, episode, urgent, notes), name="conversation")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _converse(self, trigger: str, episode: dict | None, urgent: bool, notes: list[str]) -> None:
        try:
            out = await self.steady.voice.converse(trigger=trigger, episode=episode, urgent=urgent, notes=notes)
        except (Exception, SystemExit) as e:  # noqa: BLE001  a failed conversation must not stop screening
            log.error("conversation failed: %s", e, exc_info=not isinstance(e, SystemExit))  # SystemExit: a missing key
            return
        if out is not None and out.id is not None and episode and episode.get("id") is not None:
            self.steady.writer.execute("UPDATE episodes SET conversation_id=? WHERE id=?", (out.id, episode["id"]))
        if out is not None and urgent and (out.escalated or out.outcome == "needs_help"):
            await self._call_caregiver(episode or {}, out)

    async def _call_caregiver(self, episode: dict, out) -> None:
        """After a possible fall: no answer, or the person needs help. Phone the caregiver."""
        t = episode.get("started_at") or time.time()
        at = time.strftime("%H:%M", time.localtime(t))
        if out.escalated:
            what = (f"At {at} the wristband detected a possible fall: a drop, a hard impact, then stillness. "
                    "Steady asked through the headphones twice and got no answer.")
        else:
            what = (f"At {at} the wristband detected a possible fall. Steady spoke with them through the "
                    f"headphones and they said they need help. {out.summary}").strip()
        log.warning("possible fall, %s: calling the caregiver", "no answer" if out.escalated else "needs help")
        try:
            await self.steady.caller.alert(what, t, conversation_id=out.id, episode_id=episode.get("id"))
        except Exception:  # noqa: BLE001
            log.exception("caregiver call failed")
