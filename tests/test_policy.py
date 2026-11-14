import asyncio
import os
import sqlite3
import time
from types import SimpleNamespace

from steady import config as config_mod
from steady.agent import prompts
from steady.bus import Bus
from steady.policy.engine import PolicyEngine
from steady.policy.rules import PolicyParams, Rules, in_quiet_hours
from steady.store.db import Writer
from steady.voice.ears import Ears, ScriptEars
from steady.voice.service import VoiceService
from test_conversation import FakeLLM, FakeSpeaker, call

MIN = 60.0


def at(hh, mm=0):
    """A moment today at hh:mm, local time."""
    return time.mktime(time.localtime()[:3] + (hh, mm, 0, 0, 0, -1))


def tremor(ep_id, seconds, severity="concern", kind="rest_tremor", grade="moderate", hour=10):
    return {"id": ep_id, "kind": kind, "severity": severity, "duration_s": seconds,
            "started_at": at(hour), "metrics": {"grade": grade}}


def fall(ep_id):
    return {"id": ep_id, "kind": "fall_suspected", "severity": "urgent", "duration_s": 0.4, "metrics": {}}


# ---- rules ---------------------------------------------------------------------------------

def test_tremor_needs_moderate_or_worse_for_60_seconds():
    r, t = Rules(), at(10)
    assert not r.decide(tremor(1, 300, severity="notice", grade="mild"), t).speak     # mild: logged only
    d = r.decide(tremor(2, 45), t)
    assert (d.speak, d.severity) == (False, "notice")                                # moderate, but too short
    d = r.decide(tremor(2, 60), t + 15)
    assert (d.speak, d.severity) == (True, "concern")                                # grows past 60 s: check in
    assert not r.decide(tremor(2, 62), t + 17).speak                                 # once per episode


def test_fall_is_urgent_and_skips_the_guardrails():
    r = Rules()
    d = r.decide(fall(1), at(3), worn=False, busy="normal")                          # night, not worn, mid-conversation
    assert (d.speak, d.severity) == (True, "urgent")
    assert r.decide(fall(2), at(3, 1)).speak                                         # no cooldown for falls
    d = r.decide(fall(3), at(3, 2), busy="urgent")
    assert not d.speak and d.defer                                                   # never two urgent at once
    assert r.decide(fall(4), at(3, 3), connected=False).defer


def test_cooldown_is_per_kind_and_45_minutes_for_rest_tremor():
    r, t = Rules(), at(10)
    assert r.decide(tremor(1, 60), t).speak
    d = r.decide(tremor(2, 60), t + 44 * MIN)
    assert not d.speak and not d.defer                                               # already talked about it
    assert r.decide(tremor(3, 60, kind="action_tremor"), t + 10 * MIN).speak          # other kind: own cooldown
    assert r.decide(tremor(4, 60), t + 46 * MIN).speak


def test_at_most_three_check_ins_an_hour_then_deferred():
    r, t = Rules(PolicyParams(cooldown_min={"default": 0})), at(10)
    assert [r.decide(tremor(i, 60), t + i * MIN).speak for i in range(3)] == [True, True, True]
    d = r.decide(tremor(3, 60), t + 30 * MIN)
    assert not d.speak and d.defer
    assert r.decide(tremor(4, 60), t + 61 * MIN).speak                               # the first one is an hour old


def test_quiet_hours_wrap_past_midnight_and_defer():
    q = ("22:00", "07:00")
    assert [in_quiet_hours(at(h, m), q) for h, m in [(21, 59), (22, 0), (3, 0), (6, 59), (7, 0)]] == \
        [False, True, True, True, False]
    assert not in_quiet_hours(at(3), ("00:00", "00:00"))                             # equal times: never quiet
    d = Rules().decide(tremor(1, 90), at(23))
    assert not d.speak and d.defer


def test_not_worn_busy_and_out_of_reach():
    r, t = Rules(), at(10)
    d = r.decide(tremor(1, 90), t, worn=False)
    assert not d.speak and not d.defer
    assert r.decide(tremor(2, 90), t, busy="normal").defer
    assert r.decide(tremor(3, 90), t, connected=False).defer


def test_demo_mode_speaks_sooner_and_for_any_tremor():
    p = PolicyParams.from_config(config_mod.load(overrides={"mode": "demo"}))
    assert (p.tremor_min_s, p.tremor_min_severity, p.cooldown_s("rest_tremor")) == (20, "notice", 120)
    assert PolicyParams.from_config(config_mod.load()).cooldown_s("rest_tremor") == 45 * MIN
    r = Rules(p)
    assert r.decide(tremor(1, 20, severity="notice", grade="mild"), at(10)).speak


# ---- engine --------------------------------------------------------------------------------

class FakeVoice:
    def __init__(self):
        self.running = None
        self.calls = []

    @property
    def busy(self):
        return self.running is not None

    async def converse(self, **kw):
        self.calls.append(kw)


def fake_steady(voice):
    cfg = config_mod.load(overrides={"policy.quiet_hours": ["00:00", "00:00"]})
    return SimpleNamespace(cfg=cfg, voice=voice, band=SimpleNamespace(status=SimpleNamespace(connected=True)),
                           screener=SimpleNamespace(last={"context": "rest"}), writer=None)


def test_engine_mentions_held_back_findings_and_apologises_when_it_cuts_in():
    async def go():
        voice = FakeVoice()
        engine = PolicyEngine(fake_steady(voice))
        voice.running = "normal"                              # a conversation is running
        assert engine.consider(tremor(1, 70)).defer
        engine.consider(fall(2))                              # urgent: cuts in, apologises, mentions the tremor
        await asyncio.sleep(0)
        engine.consider(tremor(3, 70, kind="action_tremor", hour=11))  # held back for the next conversation
        voice.running = None
        engine._open(trigger="double_tap")
        await asyncio.sleep(0)
        return voice.calls

    calls = asyncio.run(go())
    assert [(c["trigger"], c["urgent"]) for c in calls] == [("episode", True), ("double_tap", False)]
    assert calls[0]["notes"][0] == prompts.PREEMPTING
    assert len(calls[0]["notes"]) == 2 and "10:00" in calls[0]["notes"][1]          # the held-back tremor
    assert len(calls[1]["notes"]) == 1 and "11:00" in calls[1]["notes"][0]          # only what came after
    assert "10:00" not in calls[1]["notes"][0]


def test_urgent_conversation_stops_a_running_one_and_takes_over(tmp_path):
    script = [[("text", "Hi, I noticed your hand shaking. How are you?")],
              [("text", "Sorry, I need to check something important. Are you OK?")],
              [("text", "Good. Goodbye."), call("record_wellbeing", feeling="ok"),
               call("end_conversation", outcome="ok", summary="slipped, fine")]]

    async def go():
        writer = Writer(tmp_path / "v.db", interval=0.02)
        await writer.start()
        cfg = SimpleNamespace(db_path=tmp_path / "v.db", get=lambda key, default=None: default)
        steady = SimpleNamespace(cfg=cfg, writer=writer, bus=Bus(),
                                 band=SimpleNamespace(status=SimpleNamespace(connected=True)),
                                 screener=SimpleNamespace(last={}, episodes=SimpleNamespace(open={})))
        voice = VoiceService(steady)
        voice.speaker, voice.llm = FakeSpeaker(), FakeLLM(script)
        first = asyncio.create_task(voice.converse(trigger="episode", episode=tremor(1, 70), ears=Ears(), silence_s=30))
        while voice.current is None or voice.current.phase != "listening":
            await asyncio.sleep(0.01)
        assert voice.running == "normal" and voice.busy
        second = await voice.converse(trigger="episode", episode=fall(2), urgent=True,
                                      ears=ScriptEars(["I'm fine, I just slipped."], 0.01))
        await writer.close()
        return await first, second, voice

    first, second, voice = asyncio.run(asyncio.wait_for(go(), 20))
    assert first.outcome == "preempted"
    assert second.outcome == "ok" and second.summary == "slipped, fine"
    assert voice.running is None and not voice.busy


# ---- the whole system on the pretend band (SPEC 13, M5 "done when") --------------------------

def test_tremor_and_fall_open_the_right_conversations_on_pretend_band(tmp_path):
    """A moderate rest tremor opens a check-in by itself; a fall then stops it and opens an urgent one.

    The tremor rule is shortened to 10 s so this runs in about 40 s. Set STEADY_FULL_SCENARIO=1
    to run it with the real 60 s rule (about 90 s).
    """
    from steady.app import Steady
    from steady.validate import signals as S
    from steady.validate.runner import Player

    min_s = 60 if os.environ.get("STEADY_FULL_SCENARIO") else 10
    cfg = config_mod.load(overrides={"band.transport": "sim", "storage.db_path": str(tmp_path / "p.db"),
                                     "policy.tremor_min_s": min_s, "policy.quiet_hours": ["00:00", "00:00"],
                                     "voice.silence_s": 300})
    script = [[("text", "Hi, it's Steady. I noticed your hand shaking. How are you feeling?")],
              [("text", "Sorry, I need to check something important. Are you OK?")],
              [("text", "Glad you're OK. Goodbye."), call("record_wellbeing", feeling="ok"),
               call("end_conversation", outcome="ok", summary="slipped, OK")]]
    ears = [Ears(), ScriptEars(["I'm OK, I just slipped."], 0.05)]      # nobody answers the tremor check-in

    async def go():
        s = Steady(cfg)
        s.band.sim_options = {"auto_tap": False}
        s.voice.speaker, s.voice.llm = FakeSpeaker(), FakeLLM(script)
        converse = s.voice.converse
        s.voice.converse = lambda **kw: converse(**kw, ears=ears.pop(0))
        states = s.bus.subscribe("conversation.state")
        await s.writer.start()
        tasks = [asyncio.create_task(c) for c in (s.band.run(), s.screener.run(), s.policy.run())]
        try:
            while not s.band.status.connected:
                await asyncio.sleep(0.1)
            player = Player()
            s.band.sim.set_activity("still", cadence=0)
            s.band.sim.play_motion(player)
            await asyncio.sleep(6)
            player.switch(S.tremor(4.8, 20))                            # moderate rest tremor
            while (await states.get())[1]["state"] != "listening":      # the check-in opened and asked
                pass
            player.switch(S.fall(4.0))
            ended = 0
            while ended < 2:
                ended += (await states.get())[1]["state"] == "idle"
        finally:
            tasks[1].cancel()
            s.screener.close()
            await s.band.stop()
            await asyncio.wait_for(tasks[0], 5)
            tasks[2].cancel()
            await s.writer.close()
        return s

    s = asyncio.run(asyncio.wait_for(go(), min_s + 80))
    conn = sqlite3.connect(tmp_path / "p.db")
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT c.id, c.trigger, c.outcome, c.started_at, e.kind, e.started_at AS ep_start, e.conversation_id "
        "FROM conversations c JOIN episodes e ON e.id = c.episode_id ORDER BY c.id")]
    assert [(r["kind"], r["trigger"], r["outcome"]) for r in rows] == [
        ("rest_tremor", "episode", "preempted"), ("fall_suspected", "episode", "ok")]
    assert rows[0]["started_at"] - rows[0]["ep_start"] >= min_s                 # not before the tremor lasted min_s
    assert all(r["conversation_id"] == r["id"] for r in rows)                   # episodes link to their check-in
    assert s.voice.llm.seen[1][-1]["content"].startswith(prompts.OPEN_URGENT)
    assert prompts.PREEMPTING in s.voice.llm.seen[1][-1]["content"]
