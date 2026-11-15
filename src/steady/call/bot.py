"""One phone call to a caregiver, as a Pipecat pipeline.

    Twilio audio -> keypad (press 1) -> Deepgram STT -> context -> Ollama -> Deepgram TTS -> Twilio audio

Steady speaks first: who is calling, what happened, and a request to check on the wearer and
confirm (press 1 or say yes). It cannot be interrupted: it finishes speaking, then listens. The model can only record the answer, look up the band's latest
status, or hang up. If nobody answers twice, the call ends.
"""
from __future__ import annotations  # noqa: I001  (loguru is set up before Pipecat loads)

import asyncio
import logging
import sys
from typing import TYPE_CHECKING

from loguru import logger as _pipecat_log

_pipecat_log.remove()                              # Pipecat logs with loguru: warnings only
_pipecat_log.add(sys.stderr, level="WARNING")

from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, EndWorkerFrame, Frame,
                                   FunctionCallResultProperties, InputDTMFFrame, InterimTranscriptionFrame,
                                   InterruptionFrame, LLMFullResponseEndFrame, LLMMessagesAppendFrame, LLMRunFrame,
                                   LLMTextFrame, TranscriptionFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

if TYPE_CHECKING:
    from .server import CallSession

log = logging.getLogger("steady.call")
OLLAMA_OPENAI_URL = "https://ollama.com/v1"       # Ollama Cloud's OpenAI-compatible endpoint
PHONE_RATE = 8000

OPENING = "[The call was just answered. Speak first.]"
PRESSED_ONE = ("[They pressed 1: they will check on {wearer} now. Thank them in one sentence and ask whether they have "
               "any questions before they go. When they have none, say goodbye and call end_call.]")
NO_ANSWER = ("[Nobody has answered for a while. Say the key message once more, briefly, and ask them to press 1 "
             "or say yes if they will check on {wearer}.]")


TOOLS = ToolsSchema(standard_tools=[
    FunctionSchema("confirm_checking", "Record the answer: will they check on the wearer now?",
                   {"will_check": {"type": "boolean"},
                    "note": {"type": "string", "description": "What they said, in a few words."}}, ["will_check"]),
    FunctionSchema("get_latest_status", "What the wristband shows right now and how long ago the event was.", {}, []),
    FunctionSchema("nothing_for_me", "What you just heard was other people talking in the background, not the person "
                   "on the call talking to you. Call this and say nothing.", {}, []),
    FunctionSchema("end_call", "Hang up. Say goodbye first, in the same turn.", {}, []),
])


class Keypad(FrameProcessor):
    """Pressing 1 confirms, whatever the model is doing."""

    def __init__(self, session: CallSession) -> None:
        super().__init__()
        self.session = session

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, InputDTMFFrame) and frame.button == KeypadEntry.ONE and not self.session.acknowledged.is_set():
            self.session.acknowledge("pressed 1")
            note = PRESSED_ONE.format(wearer=self.session.wearer)
            await self.push_frame(LLMMessagesAppendFrame(messages=[{"role": "user", "content": note}], run_llm=True))


class Ears(FrameProcessor):
    """Steady's turn-taking on the phone, by words and without interruptions.

    Steady always finishes what it is saying; words spoken over it are dropped (so is the
    "Hello?" of someone answering). After it stops, what the caregiver says is collected until
    no more words have come for `turn_end_s` (or `max_turn_s` at most), and then given to the
    model as one turn. No voice-activity detection: in a room where other people are talking it
    never hears the caregiver stop.
    """

    def __init__(self, session: CallSession, turn_end_s: float = 1.0, max_turn_s: float = 8.0, on_turn=None) -> None:
        super().__init__()
        self.session = session
        self.turn_end_s, self.max_turn_s, self.on_turn = turn_end_s, max_turn_s, on_turn
        self._bot_speaking = False
        self._opened = False                    # Steady has finished its opening message
        self._parts: list[str] = []
        self._turn_began = 0.0
        self._timer: asyncio.Task | None = None

    def _restart_timer(self) -> None:
        if self._timer:
            self._timer.cancel()
        left = self.max_turn_s - (asyncio.get_running_loop().time() - self._turn_began)
        self._timer = asyncio.create_task(self._end_turn(max(0.05, min(self.turn_end_s, left))))

    async def _end_turn(self, after: float) -> None:
        await asyncio.sleep(after)
        text, self._parts, self._timer = " ".join(self._parts), [], None
        if not text or self._bot_speaking:
            return
        if self.on_turn:
            self.on_turn()
        await self.push_frame(LLMMessagesAppendFrame(messages=[{"role": "user", "content": text}], run_llm=True))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._parts.clear()                 # no interruptions: words over Steady are not answered
            if self._timer:
                self._timer.cancel()
                self._timer = None
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._opened = True
        if isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            text = frame.text.strip()
            final = isinstance(frame, TranscriptionFrame)
            if final and text:
                self.session.heard.append(text)
                log.info("call to %s: heard %r%s", self.session.to_name, text,
                         "" if self._opened and not self._bot_speaking else " (while Steady was speaking: not answered)")
            if text and self._opened and not self._bot_speaking:
                if not self._parts and self._timer is None:
                    self._turn_began = asyncio.get_running_loop().time()
                if final:
                    self._parts.append(text)
                if self._parts or not final:
                    self._restart_timer()       # still talking: wait for the pause
            return                              # not passed on: turns are decided here
        await self.push_frame(frame, direction)


class Mouth(FrameProcessor):
    """Keeps what Steady said on the call, for the log."""

    def __init__(self, session: CallSession) -> None:
        super().__init__()
        self.session = session
        self._text = ""

    def _flush(self, cut: bool = False) -> None:
        text = " ".join(self._text.split())
        self._text = ""
        if text:
            self.session.said.append(text)
            log.info("call to %s: said %r%s", self.session.to_name, text, " (cut off by the caregiver)" if cut else "")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            self._text += frame.text
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._flush()
        elif isinstance(frame, InterruptionFrame):
            self._flush(cut=True)
        await self.push_frame(frame, direction)


def build(transport, session: CallSession, stt, llm, tts, idle_s: float = 10.0,
          turn_end_s: float = 1.0) -> PipelineWorker:
    """The pipeline for one call. stt, llm and tts are Pipecat services (fakes in tests)."""
    context = LLMContext(messages=[{"role": "system", "content": session.prompt},
                                   {"role": "user", "content": OPENING}], tools=TOOLS)
    idles = 0

    def spoke() -> None:
        nonlocal idles
        idles = 0

    # Turn-taking is Ears' job (see there), so the aggregator gets no speech frames and never
    # interrupts Steady. It still times how long nobody has said anything (on_user_turn_idle).
    user, assistant = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(user_idle_timeout=idle_s))
    pipeline = Pipeline([transport.input(), Keypad(session), stt, Ears(session, turn_end_s, on_turn=spoke), user, llm,
                         Mouth(session), tts, transport.output(), assistant])
    worker = PipelineWorker(pipeline, params=PipelineParams(audio_in_sample_rate=PHONE_RATE,
                                                            audio_out_sample_rate=PHONE_RATE))

    async def confirm_checking(params) -> None:
        note = str(params.arguments.get("note") or "")[:200]
        if params.arguments.get("will_check"):
            session.acknowledge("said yes" + (f": {note}" if note else ""))
        else:
            session.how = "said they cannot" + (f": {note}" if note else "")
            log.info("call to %s: %s", session.to_name, session.how)
        await params.result_callback({"ok": True})

    async def get_latest_status(params) -> None:
        await params.result_callback(session.latest())

    async def nothing_for_me(params) -> None:          # background talk: stay silent
        await params.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))

    async def end_call(params) -> None:
        await params.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))
        await worker.queue_frame(EndWorkerFrame(reason="end_call"))     # after the goodbye has been spoken

    llm.register_function("confirm_checking", confirm_checking)
    llm.register_function("get_latest_status", get_latest_status)
    llm.register_function("nothing_for_me", nothing_for_me)
    llm.register_function("end_call", end_call)

    @transport.event_handler("on_client_connected")
    async def on_connected(_transport, _client) -> None:
        await worker.queue_frames([LLMRunFrame()])                    # Steady speaks first

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_transport, _client) -> None:
        session.finished.set()
        await worker.cancel()

    @user.event_handler("on_user_turn_idle")
    async def on_idle(_aggregator) -> None:
        nonlocal idles
        idles += 1
        if idles == 1 and not session.acknowledged.is_set():
            note = NO_ANSWER.format(wearer=session.wearer)
            await worker.queue_frame(LLMMessagesAppendFrame(messages=[{"role": "user", "content": note}], run_llm=True))
        else:
            await worker.queue_frame(EndWorkerFrame(reason="no answer"))

    return worker


def services(cfg, session: CallSession):
    """The real speech and model services for a call."""
    from pipecat.services.deepgram.stt import DeepgramSTTService
    from pipecat.services.deepgram.tts import DeepgramTTSService
    from pipecat.services.ollama.llm import OLLamaLLMService

    from ..config import secret
    dg = secret("DEEPGRAM_API_KEY")
    stt = DeepgramSTTService(api_key=dg, settings=DeepgramSTTService.Settings(
        model=cfg.get("deepgram.stt_model", "nova-3"), keyterm=session.keyterms or None))
    llm = OLLamaLLMService(base_url=OLLAMA_OPENAI_URL, api_key=secret("OLLAMA_API_KEY"),
                           settings=OLLamaLLMService.Settings(model=cfg.get("models.voice_model")))
    tts = DeepgramTTSService(api_key=dg, settings=DeepgramTTSService.Settings(
        voice=cfg.get("deepgram.tts_voice", "aura-2-thalia-en")))
    return stt, llm, tts


async def run(websocket, call_data, session: CallSession, cfg, creds) -> None:
    """Run the call on Twilio's media-stream websocket until it ends."""
    from pipecat.serializers.twilio import TwilioFrameSerializer
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
    from pipecat.workers.runner import WorkerRunner

    serializer = TwilioFrameSerializer(stream_sid=call_data["stream_id"], call_sid=call_data["call_id"],
                                       account_sid=creds.account_sid, auth_token=creds.auth_token)
    transport = FastAPIWebsocketTransport(websocket, FastAPIWebsocketParams(
        audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False, serializer=serializer))
    worker = build(transport, session, *services(cfg, session))
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    try:
        await runner.run()
    except asyncio.CancelledError:
        # Hanging up cancels the pipeline, and that surfaces here as a cancellation. It is the
        # normal end of a call, not an error; only pass it on if this handler itself is being
        # cancelled (the server is shutting down).
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
    finally:
        session.finished.set()
