"""Deepgram live speech-to-text: the kit's helper, extended with two more signals.

The kit's DeepgramSpeech already sends the band's Opus packets as they are and reports
interim and final text. For a conversation we also need to know the moment the person
starts to speak (to interrupt the agent) and a backup end-of-turn signal.
"""
from __future__ import annotations

from typing import Callable

from .. import kitpath  # noqa: F401
import speech as kit_speech  # noqa: E402  (vexo-open-kit/examples/python/speech.py)


class SteadySpeech(kit_speech.DeepgramSpeech):
    def __init__(self, on_event: Callable[[str, str], None], **options) -> None:
        super().__init__(**options)
        self.on_event = on_event

    def _message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "SpeechStarted":
            self.on_event("speech_started", "")
        elif kind == "UtteranceEnd":
            self._sentence_ends()                 # whatever was heard is a finished utterance now
            self.on_event("utterance_end", "")
        else:
            super()._message(message)

    def _say(self, text, final) -> None:
        if (text or "").strip():                  # empty guesses are noise for a conversation
            super()._say(text, final)
