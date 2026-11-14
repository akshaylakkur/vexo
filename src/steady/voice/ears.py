"""The ears: what the person says, as events on an asyncio queue (SPEC 6.2).

Events are (kind, text):
  interim         a guess at the words so far (may still change)
  final           a finished utterance (Deepgram heard a pause)
  speech_started  the person began to speak
  utterance_end   backup end-of-turn signal
  error           speech-to-text stopped

BandEars: the band's microphone -> Deepgram, through the kit's speech helper (subclassed,
not edited). The kit's callbacks arrive on its own thread and are handed to the event loop.
ScriptEars: typed or scripted lines instead of a microphone, for tests and `--say`.
"""
from __future__ import annotations

import asyncio
import logging
import os

from .. import kitpath  # noqa: F401
from ..config import Config, secret

log = logging.getLogger("steady.voice")


class Ears:
    interruptible = True                 # can the person talk over the agent?

    def __init__(self) -> None:
        self.events: asyncio.Queue = asyncio.Queue()

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    def on_listening(self) -> None:
        """The agent has finished speaking and is waiting for the person."""


class ScriptEars(Ears):
    """Lines 'said' one per turn. '<tap>' taps the band, '<silence>' says nothing."""

    interruptible = False

    def __init__(self, lines: list[str], delay_s: float = 0.4) -> None:
        super().__init__()
        self.lines = list(lines)
        self.delay_s = delay_s

    def on_listening(self) -> None:
        if not self.lines:
            return
        line = self.lines.pop(0)
        if line == "<silence>":
            return
        loop = asyncio.get_running_loop()
        if line == "<tap>":
            loop.call_later(self.delay_s, self.events.put_nowait, ("band", "tap"))
        else:
            loop.call_later(self.delay_s, self.events.put_nowait, ("final", line))


class BandEars(Ears):
    def __init__(self, cfg: Config, band_manager) -> None:
        super().__init__()
        self.cfg = cfg
        self.band = band_manager
        self._speech = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.packets = 0

    def _emit(self, kind: str, text: str = "") -> None:        # called from the kit's reader thread
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.events.put_nowait, (kind, text))

    async def start(self) -> None:
        from .stt import SteadySpeech
        self._loop = asyncio.get_running_loop()
        if not os.environ.get("DEEPGRAM_API_KEY"):               # the kit's helper reads the environment
            os.environ["DEEPGRAM_API_KEY"] = secret("DEEPGRAM_API_KEY")
        speech = SteadySpeech(
            self._emit,
            model=self.cfg.get("deepgram.stt_model", "nova-3"),
            endpointing=int(self.cfg.get("deepgram.endpointing_ms", 300)),
            utterance_end_ms=int(self.cfg.get("deepgram.utterance_end_ms", 1000)),
            vad_events="true",
        )
        await asyncio.to_thread(speech.start, lambda text, final: self._emit("final" if final else "interim", text))
        self._speech = speech
        await self.band.mic_on(self._feed)                       # the band's light goes solid blue

    def _feed(self, opus: bytes, seq: int) -> None:
        self.packets += 1
        try:
            self._speech.feed(opus, seq)
        except Exception as e:  # noqa: BLE001  a closed socket must not break the band's handler
            if self.packets % 250 == 1:
                log.warning("speech-to-text: could not send sound (%s)", type(e).__name__)

    async def stop(self) -> None:
        await self.band.mic_off()
        if self._speech is not None:
            speech, self._speech = self._speech, None
            try:
                await asyncio.to_thread(speech.stop, 2.0)
            except Exception as e:  # noqa: BLE001
                log.info("speech-to-text closed with: %s", e)
