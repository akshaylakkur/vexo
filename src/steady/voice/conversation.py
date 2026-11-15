"""One spoken conversation, from opening to closing (SPEC 6.4).

    OPENING -> SPEAKING <-> LISTENING -> THINKING ... -> CLOSING
                                           silence: ask once more, then close

The agent speaks first. A turn ends when speech-to-text reports a finished utterance.
There are no interruptions: Steady always finishes what it is saying, then listens. What the
person says or taps while it speaks is kept and answered when it has finished. (Only an
urgent finding, a possible fall, stops a running conversation.) A single tap on the band means "yes / I'm OK", a
Tilts are ignored. After `silence_s` without an answer the agent asks once more; after a
second silence the conversation closes as no_response (an urgent one is marked escalated).
The band's motion stream and the detectors keep running the whole time.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from ..agent import prompts
from ..agent.tools import ConversationState
from ..agent.voice_agent import VoiceAgent
from ..bus import Bus
from ..store.db import Writer
from .ears import Ears
from .speaker import Speaker, Spoken

log = logging.getLogger("steady.talk")


@dataclass
class Turn:
    role: str                    # assistant | user | event
    text: str
    t: float
    latency_ms: float | None = None
    interrupted: bool = False


@dataclass
class Outcome:
    id: int | None
    outcome: str
    summary: str
    escalated: bool
    turns: list[Turn] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def latencies(self) -> list[float]:
        return [t.latency_ms for t in self.turns if t.role == "assistant" and t.latency_ms is not None]


class Conversation:
    def __init__(self, state: ConversationState, agent: VoiceAgent, ears: Ears, speaker: Speaker,
                 writer: Writer, bus: Bus, silence_s: float = 12.0, max_s: float = 600.0,
                 first_today: bool = False, gesture_ok=None) -> None:
        self.state = state
        self.agent = agent
        self.ears = ears
        self.speaker = speaker
        self.writer = writer
        self.bus = bus
        self.silence_s = silence_s
        self.max_s = max_s
        self.first_today = first_today
        self.gesture_ok = gesture_ok            # () -> bool: is the wrist calm enough for a tap to mean a tap?
        self._notes: list[str] = []             # things detected meanwhile, to say at the next pause
        self.ignored_gestures = 0
        self.turns: list[Turn] = []
        self.phase = "idle"
        self._reply: asyncio.Task | None = None
        self._reply_started = 0.0
        self._heard_at: float | None = None      # when the utterance being answered was finished
        self._band_q: asyncio.Queue | None = None
        self._preempted = False

    def preempt(self) -> None:
        """Something urgent needs the headphones: stop at once, outcome "preempted"."""
        self._preempted = True

    # ---- small helpers -----------------------------------------------------------------
    def _set(self, phase: str) -> None:
        self.phase = phase
        self.bus.publish("conversation.state", {"id": self.state.id, "state": phase, "trigger": self.state.trigger})

    def _turn(self, role: str, text: str, latency_ms: float | None = None, interrupted: bool = False) -> None:
        turn = Turn(role, text, time.time(), latency_ms, interrupted)
        self.turns.append(turn)
        self.writer.insert("turns", {"conversation_id": self.state.id, "t": turn.t, "role": role, "text": text,
                                     "latency_ms": None if latency_ms is None else int(latency_ms)})
        self.bus.publish("conversation.turn", {"id": self.state.id, "role": role, "text": text, "final": True,
                                               "latency_ms": latency_ms, "interrupted": interrupted})
        log.info("%s: %s%s", role, text, f"  ({latency_ms:.0f} ms)" if latency_ms is not None else "")

    def _start_reply(self, user_text: str | None = None, note: str | None = None) -> None:
        self._set("thinking")
        self._reply_started = time.perf_counter()

        async def run() -> Spoken:
            tokens = self.agent.respond(user_text, note)
            try:
                self._set("speaking")
                return await self.speaker.say_stream(tokens)
            finally:
                with contextlib.suppress(Exception):
                    await tokens.aclose()            # stop the model if we were cut off
        self._reply = asyncio.create_task(run(), name="reply")

    async def _finish_reply(self) -> Spoken | None:
        """Wait for the running reply, record it, and return it."""
        task, self._reply = self._reply, None
        if task is None:
            return None
        try:
            res = await task
        except Exception as e:  # noqa: BLE001  the model or the voice failed: say so in the log, keep the conversation
            log.error("reply failed: %s", e)
            self._turn("event", f"reply failed: {e}")
            return None
        latency = None
        if res.first_audio_ms is not None:
            waited = (self._reply_started - self._heard_at) * 1000 if self._heard_at else 0.0
            latency = res.first_audio_ms + max(0.0, waited)
        if res.interrupted:
            self.agent.mark_interrupted(res.text)
        if res.text:
            self._turn("assistant", res.text, latency, res.interrupted)
        self._heard_at = None
        return res

    async def _interrupt(self) -> None:
        if self._reply is not None and not self._reply.done():
            await self.speaker.stop()
            await self._finish_reply()

    # ---- the conversation --------------------------------------------------------------
    async def run(self) -> Outcome:
        s = self.state
        t_open = time.time()
        s.id = await self.writer.insert_returning_id("conversations", {
            "started_at": t_open, "trigger": s.trigger, "episode_id": (s.episode or {}).get("id")})
        self._set("opening")
        self._band_q = self.bus.subscribe("band.event")
        forward = asyncio.create_task(self._forward_band())
        escalated = False
        outcome = "no_response"
        try:
            await asyncio.gather(self.ears.start(), self.speaker.start())
            if s.urgent:
                opening = prompts.OPEN_URGENT
            elif s.trigger == "episode" and s.episode:
                opening = prompts.OPEN_EPISODE.format(what=prompts.describe_episode(s.episode))
            else:
                opening = prompts.OPEN_REQUEST
            if self.first_today:
                opening += "\n[" + prompts.FIRST_TODAY + "]"
            opening += "".join("\n[" + note + "]" for note in s.notes)
            self._start_reply(note=opening)

            strikes = 0
            listening_since = time.monotonic()
            wrapping_up = False
            held: list[str] = []                 # what the person said while Steady was speaking
            held_tap = False
            while True:
                if self._preempted:
                    self._turn("event", "stopped: something urgent needs checking")
                    outcome = "preempted"
                    break
                if self._reply is not None and self._reply.done():
                    await self._finish_reply()
                    if s.ended:
                        outcome = s.outcome or "ok"
                        break
                    if held or held_tap:                         # said or tapped while Steady was speaking
                        text, held, tapped, held_tap = " ".join(held), [], held_tap, False
                        strikes = 0
                        self._heard_at = time.perf_counter()
                        if tapped:
                            s.tapped_ok = True
                            self._turn("event", "band tapped once (yes / I'm OK)")
                        if text:
                            self._turn("user", text)
                        self._start_reply(user_text=text or None, note=prompts.TAP if tapped else None)
                        continue
                    if self._notes:                              # detected meanwhile: say it now, at the pause
                        notes, self._notes = self._notes, []
                        self._turn("event", "symptom detected during the conversation")
                        self._start_reply(note="\n".join(notes))
                        continue
                    self._set("listening")
                    listening_since = time.monotonic()
                    self.ears.on_listening()
                if time.time() - t_open > self.max_s + 20:       # the goodbye never got through
                    self._turn("event", "time limit reached: closing")
                    outcome = "follow_up"
                    break
                if not wrapping_up and time.time() - t_open > self.max_s:
                    wrapping_up = True
                    await self._interrupt()
                    self._start_reply(note=prompts.WRAP_UP)
                    continue
                if self._reply is None and time.monotonic() - listening_since > self.silence_s:
                    strikes += 1
                    if strikes >= 2 or wrapping_up:
                        escalated = s.urgent or s.trigger == "episode"     # a symptom, and nobody answers
                        self._turn("event", "no answer twice: closing" + (" (escalated)" if escalated else ""))
                        break
                    self._turn("event", f"no answer for {self.silence_s:.0f} s: asking once more")
                    self._set("checking_silence")
                    self._start_reply(note=prompts.SILENCE.format(seconds=round(self.silence_s)))
                    continue
                if self._notes and self._reply is None:              # nobody is speaking: say it now
                    notes, self._notes = self._notes, []
                    self._turn("event", "symptom detected during the conversation")
                    self._start_reply(note="\n".join(notes))
                    continue
                try:
                    kind, text = await asyncio.wait_for(self.ears.events.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue

                # No interruptions: while Steady is thinking or speaking it finishes first. What the
                # person says or taps meanwhile is kept and answered as soon as it has finished.
                busy = self._reply is not None
                if kind == "interim":
                    if not busy:
                        self.bus.publish("conversation.turn", {"id": s.id, "role": "user", "text": text, "final": False})
                        listening_since = time.monotonic()
                elif kind == "final" and text.strip():
                    if busy:
                        held.append(text.strip())
                        continue
                    strikes = 0
                    self._heard_at = time.perf_counter()
                    self._turn("user", text.strip())
                    self._start_reply(user_text=text.strip())
                elif kind == "band" and text == "tap":
                    if busy:
                        held_tap = True
                        continue
                    strikes = 0
                    s.tapped_ok = True
                    self._heard_at = time.perf_counter()
                    self._turn("event", "band tapped once (yes / I'm OK)")
                    self._start_reply(note=prompts.TAP)
                elif kind == "error":
                    self._turn("event", "speech-to-text stopped")
                    outcome = "follow_up"
                    break
        finally:
            self._set("closing")
            if self._reply is not None:
                if not self._reply.done():
                    await self.speaker.stop()
                await self._finish_reply()
            forward.cancel()
            self.bus.unsubscribe(self._band_q)
            with contextlib.suppress(Exception):
                await self.ears.stop()                     # microphone off; the light returns to screening
            self.writer.execute(
                "UPDATE conversations SET ended_at=?, outcome=?, summary=?, escalated=? WHERE id=?",
                (time.time(), outcome, s.summary, int(escalated), s.id))
            last_words = next((t.text for t in reversed(self.turns) if t.role == "user"), None)
            self.bus.publish("conversation.ended", {      # for whoever wants to act on how it ended
                "id": s.id, "outcome": outcome, "escalated": escalated, "summary": s.summary,
                "trigger": s.trigger, "episode": s.episode, "started_at": t_open, "last_user_words": last_words})
            self._set("idle")
        return Outcome(s.id, outcome, s.summary, escalated, self.turns, time.time() - t_open)

    def notify(self, note: str, episode: dict | None = None) -> None:
        """Something was detected while this conversation is running: say it at the next pause,
        without cutting anyone off."""
        self._notes.append(note)
        if episode and self.state.episode is None:
            self.state.episode = episode

    async def _forward_band(self) -> None:
        """Taps on the band become conversation events. Tilts are ignored (wearing the band
        causes them), and so are taps while the wrist is shaking (the shaking causes them)."""
        while True:
            _, ev = await self._band_q.get()
            if ev.get("kind") not in ("tap", "double-tap"):
                continue
            if self.gesture_ok is not None and not self.gesture_ok():
                self.ignored_gestures += 1
                if self.ignored_gestures == 1:
                    log.info("taps ignored while the wrist is shaking")
                continue
            self.ears.events.put_nowait(("band", ev["kind"]))
