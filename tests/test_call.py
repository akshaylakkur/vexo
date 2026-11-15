import asyncio
import logging
import base64
import sqlite3
import time
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from steady import config as config_mod
from steady.call import bot, caller as caller_mod
from steady.call.caller import CaregiverCaller
from steady.call.server import CallServer, CallSession
from steady.call.twilio import TwilioClient, TwilioCreds, TwilioError, twiml_stream
from steady.policy.engine import PolicyEngine
from steady.store.db import Writer

CREDS = TwilioCreds("AC" + "1" * 32, "secret-token-0123456789abcdef0123", "+15550001111")


def session(token="tok", **kw):
    return CallSession(token=token, to_name="Priya", to_number="+15552223333", wearer="Margaret",
                       prompt="You are Steady.", latest=lambda: {"band_connected": True}, **kw)


# ---- Twilio --------------------------------------------------------------------------------

def test_twiml_streams_the_answered_call_to_our_socket_with_its_token():
    root = ET.fromstring(twiml_stream("wss://abc.trycloudflare.com/ws?x=1&y=2", "t0k&<>"))
    stream = root.find("Connect/Stream")
    assert stream.get("url") == "wss://abc.trycloudflare.com/ws?x=1&y=2"
    assert stream.find("Parameter").attrib == {"name": "token", "value": "t0k&<>"}


def test_call_request_has_callbacks_limits_and_auth_and_refusals_say_why():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(201, json={"sid": "CA123"})
        return httpx.Response(401, json={"message": "Authenticate"})

    async def go():
        client = TwilioClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        sid = await client.call("+15552223333", "https://t/twiml/k", "https://t/status/k", ring_s=25, max_s=180)
        try:
            await client.call("+15552223333", "https://t/twiml/k", "https://t/status/k")
        except TwilioError as e:
            return sid, str(e)
        return sid, None

    sid, error = asyncio.run(go())
    assert sid == "CA123"
    assert error is not None and "401" in error and "Authenticate" in error
    req = seen[0]
    assert req.url.path == f"/2010-04-01/Accounts/{CREDS.account_sid}/Calls.json"
    assert base64.b64decode(req.headers["authorization"].split()[1]).decode() == f"{CREDS.account_sid}:{CREDS.auth_token}"
    form = httpx.QueryParams(req.content.decode())
    assert (form["To"], form["From"], form["Url"], form["StatusCallback"]) == (
        "+15552223333", CREDS.from_number, "https://t/twiml/k", "https://t/status/k")
    assert form.get_list("StatusCallbackEvent") == ["initiated", "ringing", "answered", "completed"]
    assert (form["Timeout"], form["TimeLimit"]) == ("25", "180")
    assert CREDS.auth_token not in repr(CREDS)


# ---- the call server -----------------------------------------------------------------------

def server_with(s: CallSession) -> CallServer:
    srv = CallServer(config_mod.load(overrides={"call.public_url": "https://abc.trycloudflare.com"}), CREDS)
    srv.add(s)
    return srv


def test_server_refuses_unknown_tokens_and_records_the_call_status():
    s = session()
    client = TestClient(server_with(s).app())
    assert client.get("/health").text == "ok"
    assert client.post("/twiml/nope").status_code == 403
    assert client.post("/status/nope", data={"CallStatus": "completed"}).status_code == 403
    r = client.post("/twiml/tok")
    assert r.status_code == 200 and 'url="wss://abc.trycloudflare.com/ws"' in r.text
    client.post("/status/tok", data={"CallStatus": "ringing"})
    assert s.status == "ringing" and not s.finished.is_set()
    client.post("/status/tok", data={"CallStatus": "no-answer"})
    assert s.status == "no-answer" and s.finished.is_set()


class FakeService(bot.FrameProcessor):
    """Stands in for speech-to-text, the model or text-to-speech: passes frames along."""

    def __init__(self, seen=None):
        super().__init__()
        self.seen = seen

    def register_function(self, name, handler):
        pass

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if self.seen is not None and type(frame).__name__ == "LLMContextFrame":
            self.seen.append(frame.context.get_messages()[-1]["content"])
        await self.push_frame(frame, direction)


def test_twilio_media_stream_runs_the_pipecat_call_and_pressing_1_confirms(monkeypatch):
    """The real Pipecat pipeline over a pretend Twilio media stream (speech and model faked),
    on a real web server with a real websocket, so the hang-up is what Twilio's would be."""
    import json
    import socket

    import uvicorn
    import websockets
    from pipecat.serializers.twilio import TwilioFrameSerializer

    model_saw = []

    async def no_hangup(self):
        pass

    monkeypatch.setattr(bot, "services", lambda cfg, s: (FakeService(), FakeService(model_saw), FakeService()))
    monkeypatch.setattr(TwilioFrameSerializer, "_hang_up_call", no_hangup)
    s = session()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    errors = []

    class Catch(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                errors.append(record.getMessage())

    async def go():
        server = uvicorn.Server(uvicorn.Config(server_with(s).app(), host="127.0.0.1", port=port,
                                               log_level="error", lifespan="off"))
        server.install_signal_handlers = lambda: None
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.02)
        silence = base64.b64encode(b"\xff" * 160).decode()           # 20 ms of mu-law silence
        ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws")
        send = lambda msg: ws.send(json.dumps(msg))                   # noqa: E731
        await send({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        await send({"event": "start", "streamSid": "MZ1", "start": {
            "streamSid": "MZ1", "callSid": "CA1", "accountSid": CREDS.account_sid,
            "customParameters": {"token": "tok"}, "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}}})
        for _ in range(10):
            await send({"event": "media", "streamSid": "MZ1", "media": {"payload": silence, "track": "inbound"}})
        deadline = time.time() + 10
        while not model_saw and time.time() < deadline:
            await asyncio.sleep(0.05)
        await send({"event": "dtmf", "streamSid": "MZ1", "dtmf": {"track": "inbound_track", "digit": "1"}})
        while len(model_saw) < 2 and time.time() < deadline:
            await asyncio.sleep(0.05)
        await send({"event": "stop", "streamSid": "MZ1"})
        await ws.close()                                              # the caregiver hangs up
        await asyncio.wait_for(s.finished.wait(), 8)                  # ... and the call ends by itself
        await asyncio.sleep(0.2)
        server.should_exit = True
        await serving

    catch = Catch()
    logging.getLogger().addHandler(catch)
    try:
        asyncio.run(asyncio.wait_for(go(), 30))
    finally:
        logging.getLogger().removeHandler(catch)
    assert model_saw[0] == bot.OPENING                                 # Steady spoke first
    assert s.acknowledged.is_set() and s.how == "pressed 1"
    assert model_saw[1] == bot.PRESSED_ONE.format(wearer="Margaret")   # and is told to thank and say goodbye
    assert s.finished.is_set() and errors == []                        # a hang-up is a clean end, not an error


def test_a_stream_without_a_known_token_is_closed():
    client = TestClient(server_with(session()).app())
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"event": "connected"})
        ws.send_json({"event": "start", "streamSid": "MZ1", "start": {
            "streamSid": "MZ1", "callSid": "CA1", "customParameters": {"token": "wrong"}}})
        msg = ws.receive()
    assert msg["type"] == "websocket.close" and msg["code"] == 1008


# ---- who gets called -----------------------------------------------------------------------

class FakeTwilio:
    """Plays each call's outcome: 'ack' (pressed 1), 'decline', 'no-answer' or 'refused'."""

    def __init__(self, server, outcomes):
        self.server, self.outcomes, self.called = server, list(outcomes), []

    async def call(self, to, twiml_url, status_url, ring_s, max_s):
        self.called.append(to)
        outcome = self.outcomes.pop(0)
        if outcome == "refused":
            raise TwilioError("Twilio refused the call (400): not a valid phone number")
        s = self.server.sessions[twiml_url.rsplit("/", 1)[1]]
        loop = asyncio.get_running_loop()
        if outcome == "ack":
            loop.call_later(0.01, s.acknowledge, "pressed 1")
            loop.call_later(0.05, s.finished.set)                      # goodbye, then they hang up
        else:
            s.how = "said they cannot" if outcome == "decline" else ""
            s.status = "completed" if outcome == "decline" else outcome
            loop.call_later(0.01, s.finished.set)
        return "CA" + str(len(self.called))


def run_alert(tmp_path, monkeypatch, outcomes, contacts=True, ready=True):
    path = tmp_path / "contacts.yaml"
    if contacts:
        path.write_text('sender: {name: Margaret}\ncontacts:\n  caregiver: {name: Priya, phone: "+1 555 222 3333"}\n'
                        '  backup_caregiver: {name: Sam, phone: "+15554445555"}\n  doctor: {name: Dr P, email: p@x.example}\n')
    monkeypatch.setattr(caller_mod, "CONTACTS", path)

    async def go():
        writer = Writer(tmp_path / "c.db", interval=0.02)
        await writer.start()
        cfg = config_mod.load(overrides={"call.public_url": "https://t.example"})
        steady = SimpleNamespace(cfg=cfg, writer=writer, screener=SimpleNamespace(last={"context": "rest"}),
                                 band=SimpleNamespace(status=SimpleNamespace(connected=True, battery=80)))
        c = CaregiverCaller(steady)
        c.server = CallServer(cfg, CREDS)
        c.twilio = FakeTwilio(c.server, outcomes)
        c.ready = ready
        ok = await c.alert("At 14:05 the wristband detected a possible fall.", time.time() - 120, episode_id=9)
        await asyncio.sleep(0.1)                                        # the confirmed call ends
        await writer.close()
        return ok, c

    ok, c = asyncio.run(asyncio.wait_for(go(), 10))
    conn = sqlite3.connect(tmp_path / "c.db")
    conn.row_factory = sqlite3.Row
    return ok, c, dict(conn.execute("SELECT * FROM tasks").fetchone())


def test_no_answer_from_the_caregiver_then_the_backup_confirms(tmp_path, monkeypatch):
    ok, c, task = run_alert(tmp_path, monkeypatch, ["no-answer", "ack"])
    assert ok and c.twilio.called == ["+15552223333", "+15554445555"]        # in order, spaces removed
    assert (task["type"], task["tier"], task["urgency"], task["status"], task["attempts"], task["episode_id"]) == (
        "caregiver_call", 2, "urgent", "done", 2, 9)
    assert "Priya: no-answer" in task["result_note"] and "Sam confirmed (pressed 1)" in task["result_note"]
    assert c.server.sessions == {}                                            # tokens die with their calls


def test_nobody_confirms_and_the_task_fails_with_the_reasons(tmp_path, monkeypatch):
    ok, c, task = run_alert(tmp_path, monkeypatch, ["refused", "decline"])
    assert not ok and len(c.twilio.called) == 2
    assert task["status"] == "failed" and "not a valid phone number" in task["result_note"]
    assert "Sam: said they cannot" in task["result_note"]


def test_with_calls_off_the_alert_is_still_recorded_as_failed(tmp_path, monkeypatch):
    ok, c, task = run_alert(tmp_path, monkeypatch, [], contacts=False, ready=False)
    assert not ok and c.twilio.called == []
    assert task["status"] == "failed" and "no caregiver phone number" in task["result_note"]


def test_the_pretend_band_never_phones_anyone(tmp_path, monkeypatch):
    monkeypatch.setattr(caller_mod, "CONTACTS", tmp_path / "none.yaml")
    steady = SimpleNamespace(cfg=config_mod.load(overrides={"band.transport": "sim"}))
    assert "the pretend band never phones anyone" in CaregiverCaller(steady).problems()


def test_latest_status_tells_the_caregiver_what_the_band_sees_now():
    steady = SimpleNamespace(cfg=config_mod.load(), screener=SimpleNamespace(last={"context": "not_worn"}),
                             band=SimpleNamespace(status=SimpleNamespace(connected=True, battery=64)))
    status = CaregiverCaller(steady)._latest(time.time() - 90)
    assert status == {"minutes_since_event": 1.5, "band_connected": True,
                      "wrist_now": "the band seems to be off the wrist", "battery_percent": 64}


# ---- when it happens -----------------------------------------------------------------------

def test_a_symptom_nobody_answers_or_a_call_for_help_phones_the_caregiver_then_reports():
    alerts, reports = [], []

    class Voice:
        running = None
        busy = False

        def __init__(self, out):
            self.out = out

        async def converse(self, **kw):
            return self.out

    async def alert(what, t, **kw):
        alerts.append((what, kw))
        return len(alerts) == 1                                   # the first call is confirmed, the others are not

    async def create(task_type, request, **kw):
        reports.append((task_type, kw))

    def engine_for(out):
        steady = SimpleNamespace(cfg=config_mod.load(), voice=Voice(out), writer=SimpleNamespace(execute=lambda *a: None),
                                 caller=SimpleNamespace(alert=alert, last_notes=["Priya confirmed (said yes)"]),
                                 tasks=SimpleNamespace(create=create))
        return PolicyEngine(steady)

    now = time.time()
    fall = {"id": 3, "kind": "fall_suspected", "started_at": now}
    tremor = {"id": 4, "kind": "rest_tremor", "started_at": now, "duration_s": 25, "metrics": {"grade": "moderate", "freq_mean_hz": 5.0}}
    outcome = lambda **kw: SimpleNamespace(**{"id": 5, "summary": "", "escalated": False, **kw})  # noqa: E731

    async def go():
        await engine_for(outcome(outcome="no_response", escalated=True))._converse("episode", fall, True, [])
        await engine_for(outcome(outcome="needs_help", summary="She cannot get up."))._converse("episode", fall, True, [])
        await engine_for(outcome(outcome="no_response", escalated=True))._converse("episode", tremor, False, [])   # a tremor nobody answers
        await engine_for(outcome(outcome="ok"))._converse("episode", fall, True, [])                  # she is fine: no call
        await engine_for(outcome(outcome="ok"))._converse("episode", tremor, False, [])               # fine: no call
        await engine_for(outcome(outcome="no_response"))._converse("double_tap", None, False, [])     # not a symptom: no call

    asyncio.run(go())
    assert len(alerts) == 3
    assert "a possible fall" in alerts[0][0] and "got no answer" in alerts[0][0]
    assert alerts[0][1] == {"conversation_id": 5, "episode_id": 3}
    assert "they said they need help. She cannot get up." in alerts[1][0]
    assert "shaking of the hand while the arm was resting" in alerts[2][0] and "got no answer" in alerts[2][0]
    # after every call, a text and an email to the caregiver saying what happened and how the call went
    assert [r[0] for r in reports] == ["incident_report"] * 3
    kw = reports[0][1]
    assert kw["to"] == "caregiver" and kw["origin"] == "call" and kw["channels"] == ["imessage", "email"]
    assert kw["context"]["someone_is_checking"] is True and kw["context"]["phone_call"] == "Priya confirmed (said yes)"
    assert reports[2][1]["context"]["someone_is_checking"] is False


def test_dry_run_mode_places_no_call_unless_you_ask_for_a_test_call(tmp_path, monkeypatch):
    monkeypatch.setattr(caller_mod, "CONTACTS", tmp_path / "none.yaml")
    steady = SimpleNamespace(cfg=config_mod.load(overrides={"delivery.mode": "dry_run"}))
    c = CaregiverCaller(steady)
    assert any("dry_run" in p for p in c.problems())
    c.explicit = True                                    # `steady call`: an explicit test call
    assert not any("dry_run" in p for p in c.problems())


def test_the_wearer_is_told_about_the_call_and_can_ask_for_it(tmp_path, monkeypatch):
    from steady.agent import prompts
    said, opened = [], []

    class Voice:
        running = None
        busy = False

        async def converse(self, **kw):
            opened.append(kw)
            return SimpleNamespace(id=5, summary="", escalated=True, outcome="no_response")

    outcomes = [True, False]

    async def alert(what, t, **kw):
        return outcomes.pop(0)

    caller = SimpleNamespace(alert=alert, ready=True, who=lambda: ["Priya", "Sam"])
    steady = SimpleNamespace(cfg=config_mod.load(), voice=Voice(), writer=SimpleNamespace(execute=lambda *a: None),
                             caller=caller, announcer=SimpleNamespace(queue=said.append))
    fall = {"id": 3, "kind": "fall_suspected", "started_at": time.time()}

    async def go():
        eng = PolicyEngine(steady)
        eng._open(trigger="episode", episode=fall, urgent=True)
        await asyncio.gather(*eng._tasks)
        eng._open(trigger="episode", episode=fall, urgent=True)
        await asyncio.gather(*eng._tasks)
        eng._open(trigger="double_tap")                                    # not a symptom: no call note
        await asyncio.gather(*eng._tasks)

    asyncio.run(go())
    assert opened[0]["notes"] == [prompts.CALL_IF_HELP.format(name="Priya")]      # "I need help" will phone Priya
    assert opened[2]["notes"] == []
    assert said == ["I'm phoning Priya now.", "Someone is coming to check on you.",
                    "I'm phoning Priya now.", "I couldn't reach anyone by phone. If you can, please call for help."]
