"""Cuts a stream of LLM tokens into pieces worth speaking (SPEC 6.2).

A piece is sent to text-to-speech as soon as it is complete, so the voice starts before
the model has finished. Cut points, in order of preference:
  1. the end of a sentence (. ! ? followed by a space), but not inside "3.5" or after "Dr."
  2. a comma, semicolon or colon once the piece is at least `soft_chars` long
  3. nothing for `max_wait_s`: whatever is there (the caller asks with `take_stale`)
"""
from __future__ import annotations

import re
import time

ABBREVIATIONS = {"dr", "mr", "mrs", "ms", "prof", "st", "vs", "etc", "e.g", "i.e", "no", "approx"}
_END = re.compile(r'([.!?]+["\')\]]?)(\s+)')
_SOFT = re.compile(r"([,;:])(\s+)")


class SentenceSplitter:
    def __init__(self, soft_chars: int = 60, max_wait_s: float = 1.2, min_chars: int = 2) -> None:
        self.soft_chars = soft_chars
        self.max_wait_s = max_wait_s
        self.min_chars = min_chars
        self._buf = ""
        self._since = 0.0

    def _is_sentence_end(self, upto: int, mark: str) -> bool:
        before = self._buf[:upto]
        if mark.startswith(".") and not mark.startswith(".."):
            last = before.split()[-1].lower().rstrip(".") if before.split() else ""
            if last in ABBREVIATIONS or (len(last) == 1 and last.isalpha()):
                return False                       # "Dr." or an initial
        return True

    def feed(self, text: str) -> list[str]:
        """Add tokens; get back the pieces that are now complete."""
        if not self._buf.strip():
            self._since = time.monotonic()
        self._buf += text
        out: list[str] = []
        while True:
            cut = None
            for m in _END.finditer(self._buf):
                if self._is_sentence_end(m.start(1), m.group(1)):
                    cut = m.end()
                    break
            if cut is None and len(self._buf) >= self.soft_chars:
                soft = [m for m in _SOFT.finditer(self._buf) if m.end() >= self.soft_chars // 2]
                if soft:
                    cut = soft[0].end()
            if cut is None:
                break
            piece, self._buf = self._buf[:cut].strip(), self._buf[cut:]
            self._since = time.monotonic()
            if len(piece) >= self.min_chars:
                out.append(piece)
        return out

    def take_stale(self) -> str | None:
        """The unfinished piece, if it has waited longer than max_wait_s (cut at a word)."""
        if not self._buf.strip() or time.monotonic() - self._since < self.max_wait_s:
            return None
        head, sep, tail = self._buf.rpartition(" ")
        if not sep or not head.strip():
            return None
        self._buf, self._since = tail, time.monotonic()
        return head.strip()

    def flush(self) -> str | None:
        """The model has finished: whatever is left."""
        piece, self._buf = self._buf.strip(), ""
        return piece if len(piece) >= self.min_chars else None
