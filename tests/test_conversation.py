import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import httpx

from steady.agent import prompts
from steady.agent.ollama import OllamaClient
from steady.agent.tools import ConversationState, Toolbox
from steady.agent.voice_agent import VoiceAgent
from steady.bus import Bus
from steady.store.db import Writer
from steady.voice.conversation import Conversation
from steady.voice.ears import Ears, ScriptEars
from steady.voice.speaker import Spoken


# ---- fakes ---------------------------------------------------------------------------------

class FakeLLM:
    """Replies from a script: each entry is the list of items one model call yields."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def chat_stream(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        for item in self.script.pop(0):
            if item[0] == "text":
                for word in item[1].split(" "):
                    yield "text", word + " "
                    await asyncio.sleep(0)
            else:
                yield item


class FakeSpeaker:
    def __init__(self, word_delay=0.0):
        self.gen = 0
        self.word_delay = word_delay
        self.speaking_now = asyncio.Event()

    async def start(self):
        pass

    async def say_stream(self, tokens):
        gen, text = self.gen, ""
        async for t in tokens:
            if gen != self.gen:
                break
            text += t
            self.speaking_now.set()
            await asyncio.sleep(self.word_delay)
        return Spoken(text=" ".join(text.split()), pieces=1, first_audio_ms=50.0, interrupted=gen != self.gen)

    async def stop(self):
        self.gen += 1
        return 0.0


def call(name, **args):
    return ("tool_calls", [{"name": name, "arguments": args}])


async def setup(tmp_path, script, ears, state=None, speaker=None, **kw):
    writer = Writer(tmp_path / "c.db", interval=0.02)
    await writer.start()
    steady = SimpleNamespace(
        cfg=SimpleNamespace(db_path=tmp_path / "c.db"), writer=writer,
        band=SimpleNamespace(status=SimpleNamespace(connected=True)),
        screener=SimpleNamespace(last={"context": "rest", "tremor_amp": 12.0},
                                 episodes=SimpleNamespace(open={}), rollup=SimpleNamespace(flush=lambda: None)))
    state = state or ConversationState(trigger="episode", episode={
        "id": 7, "kind": "rest_tremor", "duration_s": 70, "metrics": {"grade": "moderate", "freq_mean_hz": 4.8}})
    llm = FakeLLM(script)
    tools = Toolbox(steady, state)
    agent = VoiceAgent(llm, tools, prompts.SYSTEM)
    convo = Conversation(state, agent, ears, speaker or FakeSpeaker(), writer, Bus(), **kw)
    return convo, llm, tools, writer


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def db(tmp_path, sql):
    conn = sqlite3.connect(tmp_path / "c.db")
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql)]


# ---- conversations -------------------------------------------------------------------------

def test_check_in_until_the_person_is_ok(tmp_path):
    script = [
        [("text", "Hi, it's Steady. I noticed your hand shaking. How are you feeling?")],
        [call("record_wellbeing", feeling="ok", notes="tired"), call("log_entry", kind="medication", details="levodopa at 8")],
        [("text", "Glad to hear it. Take care."), call("end_conversation", outcome="ok", summary="Shaking, feels fine.")],
    ]

    async def go():
        convo, llm, tools, writer = await setup(tmp_path, script, ScriptEars(["I'm fine, just tired. I took my levodopa at 8."], 0.01))
        out = await convo.run()
        await writer.close()
        return out, llm, tools

    out, llm, tools = run(go())
    assert out.outcome == "ok" and not out.escalated and out.summary == "Shaking, feels fine."
    assert [(t.role, t.text) for t in out.turns] == [
        ("assistant", "Hi, it's Steady. I noticed your hand shaking. How are you feeling?"),
        ("user", "I'm fine, just tired. I took my levodopa at 8."),
        ("assistant", "Glad to hear it. Take care.")]
    assert out.latencies and all(ms >= 50 for ms in out.latencies)
    # the opening told the model what was detected, as a bracketed system note
    opening = llm.seen[0][-1]["content"]
    assert opening.startswith("[The wristband detected: a moderate shaking of the hand")
    # tool results went back to the model
    assert [m["role"] for m in llm.seen[2][-3:]] == ["assistant", "tool", "tool"]
    assert db(tmp_path, "SELECT outcome, summary, trigger, episode_id, escalated FROM conversations") == [
        {"outcome": "ok", "summary": "Shaking, feels fine.", "trigger": "episode", "episode_id": 7, "escalated": 0}]
    assert [r["role"] for r in db(tmp_path, "SELECT role FROM turns ORDER BY id")] == ["assistant", "user", "assistant"]
    assert sorted(r["kind"] for r in db(tmp_path, "SELECT kind FROM logs")) == ["medication", "wellbeing"]


def test_cannot_end_as_ok_before_the_person_says_so_and_a_tap_counts(tmp_path):
    script = [
        [("text", "Hi. How are you feeling?")],
        [("text", "It is the afternoon. Bye."), call("end_conversation", outcome="ok", summary="asked the time")],
        [("text", "Before I go, are you feeling OK?")],
        [("text", "Good. Goodbye."), call("end_conversation", outcome="ok", summary="tapped OK")],
    ]

    async def go():
        convo, llm, tools, writer = await setup(tmp_path, script, ScriptEars(["What time is it?", "<tap>"], 0.01))
        out = await convo.run()
        await writer.close()
        return out, llm, tools

    out, llm, tools = run(go())
    assert "error" in tools.calls[0][2] and tools.calls[1][2]["ok"]
    assert out.outcome == "ok" and out.summary == "tapped OK"
    assert ("event", "band tapped once (yes / I'm OK)") in [(t.role, t.text) for t in out.turns]
    assert llm.seen[3][-1]["content"] == prompts.TAP


def test_silence_asks_once_more_then_closes_and_escalates_when_urgent(tmp_path):
    script = [[("text", "Are you OK? Can you answer me?")], [("text", "Can you hear me? Say yes or tap the band.")]]

    async def go():
        state = ConversationState(trigger="episode", urgent=True, episode={"id": 1, "kind": "fall_suspected", "metrics": {}})
        convo, llm, tools, writer = await setup(tmp_path, script, ScriptEars([]), state=state, silence_s=0.15)
        out = await convo.run()
        await writer.close()
        return out, llm

    out, llm = run(go())
    assert out.outcome == "no_response" and out.escalated
    assert llm.seen[0][-1]["content"] == prompts.OPEN_URGENT
    assert llm.seen[1][-1]["content"].startswith("[The person has not answered")
    assert [t.role for t in out.turns] == ["assistant", "event", "assistant", "event"]
    assert db(tmp_path, "SELECT outcome, escalated FROM conversations") == [{"outcome": "no_response", "escalated": 1}]


def test_talking_over_the_agent_stops_it(tmp_path):
    long_reply = "Hi, it's Steady. " + "I noticed quite a lot of shaking in your hand just now. " * 6
    script = [[("text", long_reply)], [("text", "Of course. I'll check on you later."),
                                       call("end_conversation", outcome="follow_up", summary="asked for a minute")]]

    async def go():
        ears, speaker = Ears(), FakeSpeaker(word_delay=0.01)
        convo, llm, tools, writer = await setup(tmp_path, script, ears, speaker=speaker)
        task = asyncio.create_task(convo.run())
        await speaker.speaking_now.wait()
        await asyncio.sleep(0.05)
        ears.events.put_nowait(("interim", "wait"))             # one word: not yet an interruption
        await asyncio.sleep(0.03)
        assert convo.phase == "speaking"
        ears.events.put_nowait(("interim", "wait stop"))        # two words: the agent stops
        await asyncio.sleep(0.1)
        assert convo.phase == "listening"
        ears.events.put_nowait(("final", "Wait, stop. I need a minute."))
        out = await task
        await writer.close()
        return out, llm

    out, llm = run(go())
    first = out.turns[0]
    assert first.interrupted and len(first.text) < 200          # cut off well before the end
    assert out.turns[1].text == "Wait, stop. I need a minute." and out.outcome == "follow_up"
    # the model is told what it actually got to say, not the whole reply it wrote
    assert llm.seen[1][-2]["content"].endswith("[interrupted by the person]")


# ---- Ollama client -------------------------------------------------------------------------

def ndjson(*chunks):
    return "\n".join(json.dumps(c) for c in chunks) + "\n"


def test_ollama_stream_text_tool_calls_fallback_and_key_in_header():
    requests = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        requests.append((body["model"], req.headers.get("authorization"), str(req.url), body))
        if body["model"] == "broken":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=ndjson(
            {"message": {"role": "assistant", "content": "Hi "}, "done": False},
            {"message": {"role": "assistant", "content": "there."}, "done": False},
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "record_wellbeing", "arguments": {"feeling": "ok"}}},
                {"function": {"name": "log_entry", "arguments": "{\"kind\": \"note\", \"details\": \"x\"}"}}]}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True}))

    async def go():
        llm = OllamaClient("the-key", "broken", fallback="good", transport=httpx.MockTransport(handler))
        items = [i async for i in llm.chat_stream([{"role": "user", "content": "hello"}], tools=[{"x": 1}])]
        await llm.close()
        return items

    items = run(go())
    assert [m for m, *_ in requests] == ["broken", "good"]
    assert requests[1][1] == "Bearer the-key" and "the-key" not in requests[1][2]
    assert requests[1][3]["stream"] is True and requests[1][3]["tools"] == [{"x": 1}]
    assert items == [("text", "Hi "), ("text", "there."), ("tool_calls", [
        {"name": "record_wellbeing", "arguments": {"feeling": "ok"}},
        {"name": "log_entry", "arguments": {"kind": "note", "details": "x"}}])]


# ---- speech-to-text events -----------------------------------------------------------------

def test_speech_events_from_deepgram_messages():
    from steady.voice.stt import SteadySpeech
    events = []
    sp = SteadySpeech(lambda kind, text: events.append((kind, text)), endpointing=300, vad_events="true")
    assert sp.query["vad_events"] == "true" and sp.query["mip_opt_out"] == "true" and sp.query["encoding"] == "opus"
    sp.on_text = lambda text, final: events.append(("final" if final else "interim", text))
    sp.finals, sp._parts = [], []

    def results(text, is_final=False, speech_final=False):
        return {"type": "Results", "is_final": is_final, "speech_final": speech_final,
                "channel": {"alternatives": [{"transcript": text}]}}

    sp._message({"type": "SpeechStarted"})
    sp._message(results(""))                                   # an empty guess is dropped
    sp._message(results("I'm"))
    sp._message(results("I'm fine", is_final=True, speech_final=True))
    sp._message(results("just tired", is_final=True))          # heard, but no pause reported yet
    sp._message({"type": "UtteranceEnd"})                      # the backup signal finishes it
    assert events == [("speech_started", ""), ("interim", "I'm"), ("final", "I'm fine"),
                      ("interim", "just tired"), ("final", "just tired"), ("utterance_end", "")]


# ---- tools ---------------------------------------------------------------------------------

def test_summary_tool_reads_rollups_and_episodes(tmp_path):
    async def go():
        convo, llm, tools, writer = await setup(tmp_path, [], ScriptEars([]))
        hour = float(int(time.time() // 3600) * 3600)
        writer.insert("hourly_rollups", {"hour_start": hour, "worn_s": 1800, "rest_s": 1000, "rest_tremor_s": 120,
                                         "steps": 340, "falls": 0})
        writer.insert("episodes", {"kind": "rest_tremor", "status": "closed", "started_at": time.time() - 60,
                                   "duration_s": 48, "severity": "concern"})
        out = await tools.call("get_summary", {"period": "last_hour"})
        bad = await tools.call("no_such_tool", {})
        await writer.close()
        return out, bad

    out, bad = run(go())
    assert out["rest_tremor_seconds"] == 120 and out["rest_tremor_percent_of_resting_time"] == 12.0
    assert out["minutes_monitored"] == 30.0 and out["steps"] == 340
    assert out["episodes"] == {"rest_tremor": {"count": 1, "total_seconds": 48}}
    assert "error" in bad
