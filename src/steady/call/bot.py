"""One phone call to a caregiver, as a Pipecat pipeline.

    Twilio audio -> keypad (press 1) -> Deepgram STT -> context -> Ollama -> Deepgram TTS -> Twilio audio

Steady speaks first: who is calling, what happened, and a request to check on the wearer and
confirm (press 1 or say yes). The model can only record the answer, look up the band's latest
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
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (EndWorkerFrame, Frame, FunctionCallResultProperties, InputDTMFFrame,
                                   LLMMessagesAppendFrame, LLMRunFrame, TranscriptionFrame)
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
PRESSED_ONE = "[They pressed 1: they will check on {wearer} now. Thank them in one sentence, say goodbye, call end_call.]"
NO_ANSWER = ("[Nobody has answered for a while. Say the key message once more, briefly, and ask them to press 1 "
             "or say yes if they will check on {wearer}.]")


TOOLS = ToolsSchema(standard_tools=[
    FunctionSchema("confirm_checking", "Record the answer: will they check on the wearer now?",
                   {"will_check": {"type": "boolean"},
                    "note": {"type": "string", "description": "What they said, in a few words."}}, ["will_check"]),
    FunctionSchema("get_latest_status", "What the wristband shows right now and how long ago the event was.", {}, []),
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
    """Keeps what the caregiver said, for the log and the task's note."""

    def __init__(self, session: CallSession) -> None:
        super().__init__()
        self.session = session

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            self.session.heard.append(frame.text.strip())
            log.info("call to %s: heard %r", self.session.to_name, frame.text.strip())
        await self.push_frame(frame, direction)


def build(transport, session: CallSession, stt, llm, tts, idle_s: float = 10.0) -> PipelineWorker:
    """The pipeline for one call. stt, llm and tts are Pipecat services (fakes in tests)."""
    context = LLMContext(messages=[{"role": "system", "content": session.prompt},
                                   {"role": "user", "content": OPENING}], tools=TOOLS)
    user, assistant = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(), user_idle_timeout=idle_s))
    pipeline = Pipeline([transport.input(), Keypad(session), stt, Ears(session), user, llm, tts,
                         transport.output(), assistant])
    worker = PipelineWorker(pipeline, params=PipelineParams(audio_in_sample_rate=PHONE_RATE,
                                                            audio_out_sample_rate=PHONE_RATE))
    idles = 0

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

    async def end_call(params) -> None:
        await params.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))
        await worker.queue_frame(EndWorkerFrame(reason="end_call"))     # after the goodbye has been spoken

    llm.register_function("confirm_checking", confirm_checking)
    llm.register_function("get_latest_status", get_latest_status)
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
