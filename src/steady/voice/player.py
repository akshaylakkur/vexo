"""Plays streamed PCM in the headphones (SPEC 6.2).

sounddevice pulls audio from a buffer in its own callback thread, 20 ms at a time, so
`stop()` (an interruption) silences the output within one block. 24 kHz, mono, 16-bit.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

log = logging.getLogger("steady.voice")
RATE = 24000
BLOCK = RATE // 50              # 20 ms
BYTES_PER_SAMPLE = 2


class PcmBuffer:
    """Thread-safe byte queue: the network side writes, the audio callback reads."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._size = 0
        self._lock = threading.Lock()
        self.underruns = 0
        self.played_bytes = 0

    def write(self, pcm: bytes) -> None:
        if pcm:
            with self._lock:
                self._chunks.append(pcm)
                self._size += len(pcm)

    def read(self, n: int) -> bytes:
        """Exactly n bytes: audio, padded with silence when the buffer runs dry."""
        out = bytearray()
        with self._lock:
            while self._chunks and len(out) < n:
                chunk = self._chunks[0]
                take = n - len(out)
                if len(chunk) <= take:
                    out += chunk
                    self._chunks.pop(0)
                else:
                    out += chunk[:take]
                    self._chunks[0] = chunk[take:]
            got = len(out)
            self._size -= got
            self.played_bytes += got
            if got and got < n:
                self.underruns += 1
        if got < n:
            out += b"\x00" * (n - got)
        return bytes(out)

    def clear(self) -> int:
        with self._lock:
            dropped, self._chunks, self._size = self._size, [], 0
        return dropped

    @property
    def seconds(self) -> float:
        return self._size / (RATE * BYTES_PER_SAMPLE)


def find_output(fragment: str | None):
    """(index, name) of the first output device whose name contains `fragment`, else None."""
    import sounddevice as sd
    if fragment:
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] > 0 and fragment.lower() in dev["name"].lower():
                return i, dev["name"]
    return None


class Player:
    def __init__(self, device: str | None = None) -> None:
        self.wanted = device
        self.buffer = PcmBuffer()
        self.device_name: str | None = None
        self._stream = None
        self._carry = b""
        self.first_audio_at: float | None = None      # when the first sample of this utterance was played

    def start(self) -> None:
        if self._stream is not None:
            return
        import sounddevice as sd
        found = find_output(self.wanted)
        if found is None and self.wanted:
            log.warning("audio: no output device matching %r; using the system default", self.wanted)
        index = found[0] if found else None
        self.device_name = found[1] if found else sd.query_devices(kind="output")["name"]

        def callback(outdata, frames, _time, status) -> None:
            had = self.buffer.seconds > 0
            outdata[:] = self.buffer.read(frames * BYTES_PER_SAMPLE)
            if had and self.first_audio_at is None:
                self.first_audio_at = time.perf_counter()

        self._stream = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK,
                                          device=index, callback=callback)
        self._stream.start()
        log.info("audio: playing on %s", self.device_name)

    def write(self, pcm: bytes) -> None:
        """Queue PCM bytes (any length; an odd byte is carried to the next call)."""
        data = self._carry + pcm
        cut = len(data) - (len(data) % BYTES_PER_SAMPLE)
        self._carry = data[cut:]
        self.buffer.write(data[:cut])

    def stop(self) -> float:
        """Silence now. Returns the seconds of speech that were dropped."""
        self._carry = b""
        return self.buffer.clear() / (RATE * BYTES_PER_SAMPLE)

    def mark_utterance(self) -> None:
        self.first_audio_at = None

    @property
    def playing(self) -> bool:
        return self.buffer.seconds > 0

    async def drain(self, timeout: float = 60.0) -> None:
        """Wait until everything queued has been played."""
        end = time.monotonic() + timeout
        played = False
        while self.buffer.seconds > 0 and time.monotonic() < end:
            played = True
            await asyncio.sleep(0.02)
        if played and self.buffer.played_bytes:
            await asyncio.sleep(0.12)      # the device's own output latency

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
