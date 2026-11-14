"""Deepgram Aura-2 text-to-speech over one WebSocket (SPEC 6.2).

Text goes in as `Speak` messages, `Flush` ends a piece, raw PCM comes back in binary
messages while it is still being generated. `Clear` drops what Deepgram has queued (an
interruption). The key travels in a header, never in the address, and is never logged.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Callable
from urllib.parse import urlencode

import websockets

log = logging.getLogger("steady.voice")
URL = "wss://api.deepgram.com/v1/speak"          # the only place the key and the text go


class TTSError(Exception):
    pass


class DeepgramTTS:
    def __init__(self, key: str, on_audio: Callable[[bytes], None], voice: str = "aura-2-thalia-en",
                 sample_rate: int = 24000, url: str = URL) -> None:
        self._key = key
        self.on_audio = on_audio
        self.voice = voice
        self.sample_rate = sample_rate
        self.url = url
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._sent = 0                 # Flush messages sent
        self._flushed = 0              # Flushed answers received
        self._progress = asyncio.Event()
        self._dropping = False         # between Clear and Cleared: late audio is thrown away
        self._send_lock = asyncio.Lock()
        self.first_byte_at: float | None = None
        self.bytes_received = 0
        self.connects = 0
        self._clear_no = 0

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._reader is not None and not self._reader.done()

    async def connect(self) -> None:
        if self.connected:
            return
        query = urlencode({"model": self.voice, "encoding": "linear16", "sample_rate": self.sample_rate})
        try:
            self._ws = await websockets.connect(f"{self.url}?{query}",
                                                additional_headers={"Authorization": "Token " + self._key},
                                                open_timeout=10, max_size=None)
        except websockets.InvalidStatus as e:
            raise TTSError(f"Deepgram text-to-speech refused the connection (status {e.response.status_code})") from None
        except (OSError, asyncio.TimeoutError) as e:
            raise TTSError(f"Could not reach Deepgram text-to-speech ({type(e).__name__})") from None
        self.connects += 1
        self._sent = self._flushed = 0
        self._dropping = False
        self._reader = asyncio.create_task(self._read(self._ws), name="tts-reader")

    async def _read(self, ws) -> None:
        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    if self._dropping:
                        continue
                    if self.first_byte_at is None:
                        self.first_byte_at = time.perf_counter()
                    self.bytes_received += len(msg)
                    self.on_audio(bytes(msg))
                    continue
                kind = json.loads(msg).get("type")
                if kind == "Flushed":
                    if not self._dropping:          # a Flushed for speech that was cleared does not count
                        self._flushed += 1
                elif kind == "Cleared":
                    self._dropping = False          # what follows belongs to speech sent after the Clear
                elif kind in ("Warning", "Error"):
                    log.warning("tts: Deepgram sent a %s", kind)      # its text is not repeated here
                self._progress.set()
        except websockets.ConnectionClosed:
            pass
        finally:
            self._flushed = self._sent        # nothing more will come: do not leave waiters hanging
            self._progress.set()

    async def _send(self, *messages: dict) -> None:
        async with self._send_lock:
            for attempt in (1, 2):            # the socket may have idled out: reconnect once
                await self.connect()
                try:
                    for m in messages:
                        await self._ws.send(json.dumps(m))
                    return
                except websockets.ConnectionClosed:
                    self._ws = None
                    if attempt == 2:
                        raise TTSError("The connection to Deepgram text-to-speech closed") from None

    async def speak(self, text: str) -> None:
        """Queue one piece of text; its audio starts arriving at once."""
        text = text.strip()
        if not text:
            return
        await self._send({"type": "Speak", "text": text}, {"type": "Flush"})
        self._sent += 1                             # after sending: a (re)connect resets the counters

    async def wait_flushed(self, timeout: float = 30.0) -> bool:
        """Until Deepgram has sent the audio of everything spoken so far."""
        end = time.monotonic() + timeout
        while self._flushed < self._sent:
            left = end - time.monotonic()
            if left <= 0:
                return False
            self._progress.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._progress.wait(), left)
        return True

    async def clear(self) -> None:
        """Interruption: Deepgram drops its queue; audio still on the way is discarded."""
        if not self.connected:
            return
        # Messages are answered in order, so everything up to "Cleared" belongs to the speech
        # being dropped, and everything after it to speech sent after this call.
        self._dropping = True
        self._clear_no += 1
        mine = self._clear_no
        self._sent = self._flushed = 0
        self._progress.set()
        with contextlib.suppress(TTSError):
            await self._send({"type": "Clear"})

        def give_up() -> None:                      # in case "Cleared" never comes
            if self._clear_no == mine:
                self._dropping = False
        asyncio.get_running_loop().call_later(1.0, give_up)

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "Close"}))
                await self._ws.close()
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        self._ws = self._reader = None
