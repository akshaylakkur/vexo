"""The mouth: text (whole, or streaming from the model) -> headphones, interruptible.

    speaker = Speaker.from_config(cfg); await speaker.start()
    await speaker.say("Hi, it's Steady.")
    await speaker.say_stream(token_iterator)      # speaks each sentence as it completes
    await speaker.stop()                          # interruption: silent within ~20 ms
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator

from ..config import Config, secret
from .player import Player
from .sentences import SentenceSplitter
from .tts import DeepgramTTS

log = logging.getLogger("steady.voice")
NOTICE = "Note: the text Steady speaks is sent over the internet to Deepgram (deepgram.com) to be turned into speech."


@dataclass
class Spoken:
    text: str = ""
    pieces: int = 0
    first_piece_ms: float | None = None     # start -> first piece sent to Deepgram (waiting for the model)
    first_byte_ms: float | None = None      # first piece sent -> first audio received
    first_audio_ms: float | None = None     # start -> first sample played in the headphones
    total_s: float = 0.0
    audio_s: float = 0.0
    interrupted: bool = False


class Speaker:
    def __init__(self, tts: DeepgramTTS, player: Player) -> None:
        self.tts = tts
        self.player = player
        self._generation = 0            # bumps on stop(): a running say() notices and gives up
        self.speaking = False
        self._noticed = False

    @classmethod
    def from_config(cls, cfg: Config, url: str | None = None) -> "Speaker":
        player = Player(cfg.get("audio.output_device"))
        kwargs = {"url": url} if url else {}
        tts = DeepgramTTS(secret("DEEPGRAM_API_KEY"), player.write,
                          voice=cfg.get("deepgram.tts_voice", "aura-2-thalia-en"), **kwargs)
        return cls(tts, player)

    async def start(self) -> None:
        if not self._noticed:
            print(NOTICE, flush=True)
            self._noticed = True
        self.player.start()
        await self.tts.connect()

    async def say(self, text: str) -> Spoken:
        async def one() -> AsyncIterator[str]:
            yield text + " "
        return await self.say_stream(one())

    async def say_stream(self, tokens: AsyncIterator[str]) -> Spoken:
        gen = self._generation
        res = Spoken()
        splitter = SentenceSplitter()
        t0 = time.perf_counter()
        t_first_send = None
        bytes0 = self.tts.bytes_received
        self.tts.first_byte_at = None
        self.player.mark_utterance()
        self.speaking = True

        async def send(piece: str) -> None:
            nonlocal t_first_send
            if gen != self._generation:
                return
            if t_first_send is None:
                t_first_send = time.perf_counter()
                res.first_piece_ms = (t_first_send - t0) * 1000
            await self.tts.speak(piece)
            res.pieces += 1
            res.text += (" " if res.text else "") + piece

        # A pump task reads the model's tokens into a queue, so a slow pause (a tool call)
        # never cancels the model's stream; we only time out on the queue.
        queue: asyncio.Queue = asyncio.Queue()
        done = object()

        async def pump() -> None:
            try:
                async for token in tokens:
                    queue.put_nowait(token)
                queue.put_nowait(done)
            except Exception as e:  # noqa: BLE001  handed to the consumer below
                queue.put_nowait(e)

        pump_task = asyncio.create_task(pump(), name="token-pump")
        try:
            while gen == self._generation:
                try:
                    token = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    stale = splitter.take_stale()        # the model paused mid-sentence: speak what we have
                    if stale:
                        await send(stale)
                    continue
                if token is done:
                    break
                if isinstance(token, Exception):
                    raise token
                for piece in splitter.feed(token):
                    await send(piece)
            rest = splitter.flush()
            if rest:
                await send(rest)
            if gen == self._generation:
                await self.tts.wait_flushed()
                await self.player.drain()
        finally:
            self.speaking = False
            if not pump_task.done():
                pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump_task
        res.interrupted = gen != self._generation
        res.total_s = time.perf_counter() - t0
        res.audio_s = (self.tts.bytes_received - bytes0) / (self.tts.sample_rate * 2)
        if t_first_send and self.tts.first_byte_at:
            res.first_byte_ms = (self.tts.first_byte_at - t_first_send) * 1000
        if self.player.first_audio_at:
            res.first_audio_ms = (self.player.first_audio_at - t0) * 1000
        return res

    async def stop(self) -> float:
        """Interrupt: stop the sound now and drop what is queued. Returns seconds dropped."""
        self._generation += 1
        dropped = self.player.stop()
        await self.tts.clear()
        self.player.stop()
        return dropped

    async def close(self) -> None:
        await self.tts.close()
        self.player.close()
