"""Runs conversations, one at a time; an urgent one stops a normal one (SPEC 5.3, 6)."""
from __future__ import annotations

import asyncio
import logging
import time

from ..agent import prompts
from ..agent.ollama import OllamaClient
from ..agent.tools import ConversationState, Toolbox
from ..agent.voice_agent import VoiceAgent
from ..config import secret
from ..store.db import connect
from ..screening.gestures import wrist_calm
from .conversation import Conversation, Outcome
from .ears import BandEars, Ears
from .speaker import Speaker

log = logging.getLogger("steady.talk")


class VoiceService:
    def __init__(self, steady) -> None:
        self.steady = steady
        self.cfg = steady.cfg
        self.speaker: Speaker | None = None
        self.llm: OllamaClient | None = None
        self.current: Conversation | None = None
        self.running: str | None = None          # urgency of the conversation running: normal | urgent
        self._preempt = False
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def _first_today(self) -> bool:
        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        conn = connect(self.cfg.db_path, readonly=True)
        try:
            return conn.execute("SELECT count(*) FROM conversations WHERE started_at >= ?", (midnight,)).fetchone()[0] == 0
        finally:
            conn.close()

    async def converse(self, trigger: str = "double_tap", episode: dict | None = None, urgent: bool = False,
                       ears: Ears | None = None, silence_s: float | None = None,
                       max_s: float | None = None, notes: list[str] | None = None) -> Outcome | None:
        """Run one conversation to its end. Returns None if one is already running, except that an
        urgent conversation stops a normal one (outcome "preempted") and then takes over."""
        if self._lock.locked():
            if not (urgent and self.running == "normal"):
                log.info("a conversation is already running; %s ignored", trigger)
                return None
            log.warning("urgent: stopping the current conversation")
            self._preempt = True
            if self.current is not None:
                self.current.preempt()
        async with self._lock:
            self.running, self._preempt = ("urgent" if urgent else "normal"), False
            try:
                if self.speaker is None:
                    self.speaker = Speaker.from_config(self.cfg)
                if self.llm is None:
                    self.llm = OllamaClient(secret("OLLAMA_API_KEY"), self.cfg.get("models.voice_model"),
                                            self.cfg.get("models.voice_model_fallback"))
                state = ConversationState(trigger=trigger, episode=episode, urgent=urgent, notes=list(notes or []))
                agent = VoiceAgent(self.llm, Toolbox(self.steady, state), prompts.SYSTEM)
                await self.steady.writer.flush()
                first = await asyncio.to_thread(self._first_today)
                if ears is None:
                    ears = BandEars(self.cfg, self.steady.band)
                    await self.steady.band.flash(self.cfg.get("led.uncalibrated_colour", "#ffa000"))
                convo = Conversation(
                    state, agent, ears, self.speaker, self.steady.writer, self.steady.bus,
                    silence_s=float(silence_s if silence_s is not None else self.cfg.get("voice.silence_s", 12)),
                    max_s=float(max_s) if max_s else 60.0 * float(self.cfg.get("voice.max_minutes", 10)),
                    first_today=first,
                    gesture_ok=lambda: wrist_calm(getattr(self.steady.screener, "last", None)))
                self.current = convo
                if self._preempt:                  # an urgent finding arrived while this one was opening
                    convo.preempt()
                try:
                    out = await convo.run()
                finally:
                    self.current = None
            finally:
                self.running = None
            log.info("conversation %s ended: %s (%s) in %.0f s", out.id, out.outcome, out.summary, out.seconds)
            return out

    async def close(self) -> None:
        if self.speaker:
            await self.speaker.close()
        if self.llm:
            await self.llm.close()
