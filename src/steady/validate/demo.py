"""Dress rehearsal of the demo, on the pretend band.

    python -m steady.validate.demo            # dry run: nothing is sent and nobody is phoned
    python -m steady.validate.demo --live     # REALLY phones the caregiver and sends the text and the email

The demo, in two acts:
  1. The wearer's hand shakes. Steady speaks first in the headphones, says what it noticed and
     offers help. The wearer says they are fine; the check-in ends well.
  2. The hand shakes again. This time nobody answers, twice. Steady phones the caregiver, they
     talk, the caregiver agrees to check, and afterwards a text and an email go to the caregiver.
The band is simulated (a Parkinson's-type tremor is played into it) and scripted lines stand in
for the wearer's voice in act 1. Everything else is the real pipeline. With --live the phone
call is real: answer it and talk. Needs the OpenClaw gateway that config/local.yaml points to.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import sys
import time

import uvicorn

from .. import config as config_mod
from ..api.server import create_app
from ..app import Steady
from ..voice.ears import ScriptEars
from . import signals as S
from .runner import Player

ACT1 = ["I'm fine, thanks. My hand was just a bit shaky for a moment.", "No, that's all. Goodbye."]


async def main(live: bool) -> int:
    cfg = config_mod.load(overrides={
        "band.transport": "sim", "storage.db_path": "data/demo-rehearsal.db", "mode": "demo",
        "delivery.mode": "live" if live else "dry_run", "call.allow_pretend_band": live,
        "brief.enabled": False, "policy.quiet_hours": ["00:00", "00:00"]})
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            (cfg.db_path.parent / (cfg.db_path.name + suffix)).unlink()
    steady = Steady(cfg)
    steady.band.sim_options = {"auto_tap": False}
    scripts = [list(ACT1), []]
    real_converse = steady.voice.converse

    async def converse(**kw):                       # scripted lines stand in for the band's microphone
        kw["ears"] = ScriptEars(scripts.pop(0) if scripts else [])
        return await real_converse(**kw)
    steady.voice.converse = converse

    await steady.writer.start()
    await steady.tasks.start()
    server = uvicorn.Server(uvicorn.Config(create_app(steady), host="127.0.0.1", port=int(cfg.get("api.port", 8765)),
                                           log_level="warning", lifespan="off", timeout_graceful_shutdown=2))
    server.install_signal_handlers = lambda: None
    band_task = asyncio.create_task(steady.band.run())
    bg = [asyncio.create_task(c) for c in (steady.screener.run(), steady.policy.run(), steady.tasks.watch(),
                                           steady.announcer.run())]
    api = asyncio.create_task(server.serve())
    await steady.caller.start()                     # the call server and its tunnel (off in a dry run)
    q = steady.bus.subscribe("conversation.ended")

    async def conversation_end(timeout: float) -> dict | None:
        try:
            return (await asyncio.wait_for(q.get(), timeout))[1]
        except asyncio.TimeoutError:
            return None

    player = Player()
    t_act = {}
    try:
        for _ in range(100):
            if steady.band.status.connected:
                break
            await asyncio.sleep(0.2)
        sim = steady.band.sim
        sim.set_activity("still", cadence=0)
        sim.play_motion(player)
        await asyncio.sleep(8)

        print("\n=== ACT 1: the hand shakes; the wearer answers ...", flush=True)
        t_act[1] = time.time()
        player.switch(S.tremor(5.0, 25))
        c1 = await conversation_end(240)
        player.switch(S.quiet_rest())
        print(f"act 1 check-in: {c1 and c1['outcome']} ({time.time() - t_act[1]:.0f} s after the shaking began)", flush=True)
        await asyncio.sleep(22)                      # the first tremor episode closes, the demo cooldown passes

        print("\n=== ACT 2: the hand shakes again; nobody answers ...", flush=True)
        if live:
            print(">>> The caregiver's phone will ring in about a minute. Answer it and talk to Steady.", flush=True)
        t_act[2] = time.time()
        player.switch(S.tremor(5.0, 25))
        c2 = await conversation_end(240)
        player.switch(S.quiet_rest())
        print(f"act 2 check-in: {c2 and c2['outcome']}, escalated={c2 and c2['escalated']}", flush=True)
        report = None
        for _ in range(600):                         # the call (up to 3 minutes), then the report
            report = next((t for t in steady.tasks.tasks.values() if t.type == "incident_report"), None)
            if report is not None and report.status in ("done", "failed"):
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(10)                      # the spoken outcome
    finally:
        for t in bg:
            t.cancel()
        steady.screener.close()
        await steady.band.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(band_task), 5)
        await steady.caller.stop()
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(api, 5)
        await steady.voice.close()
        await steady.writer.close()

    db = sqlite3.connect(cfg.db_path)
    db.row_factory = sqlite3.Row
    eps = [dict(r) for r in db.execute("SELECT id, kind, severity, conversation_id FROM episodes ORDER BY id")]
    convs = [dict(r) for r in db.execute("SELECT id, trigger, episode_id, outcome, escalated FROM conversations ORDER BY id")]
    calls = [dict(r) for r in db.execute("SELECT status, result_note FROM tasks WHERE type='caregiver_call'")]
    turns = [dict(r) for r in db.execute("SELECT conversation_id c, role, text FROM turns ORDER BY id")]
    print("\n--- what was said in the headphones ---")
    for t in turns:
        print(f"  [{t['c']}] {t['role']:>9}: {t['text']}")
    print("\nepisodes:", eps, "\nconversations:", convs, "\ncaregiver call:", calls)
    report = next((t for t in steady.tasks.tasks.values() if t.type == "incident_report"), None)
    if report:
        print(f"\nreport to {report.recipient.name}: {report.status} ({report.note})")
        for d in report.deliveries:
            print(f"   {d['channel']}: {'DELIVERED' if d['ok'] else 'NOT delivered'} ({d['detail']}), written by {d['by']}"
                  + (f"\n   subject: {d['subject']}" if d.get("subject") else "") + f"\n   > {d['text']}")
    print("spoken in the headphones afterwards:", steady.announcer.spoken)

    tremors = [e for e in eps if e["kind"] == "rest_tremor"]
    opening = next((t["text"] for t in turns if t["role"] == "assistant"), "")
    checks = [
        ("act 1: the tremor was detected and Steady opened the check-in by itself", len(tremors) >= 1 and len(convs) >= 1
         and convs[0]["trigger"] == "episode"),
        ("act 1: Steady opened with what it noticed, not a general greeting", "shak" in opening.lower() or "trem" in opening.lower()),
        ("act 1: it ended well, only after the wearer said they were fine", bool(convs) and convs[0]["outcome"] == "ok"),
        ("act 2: a second tremor was detected and opened a second check-in", len(tremors) >= 2 and len(convs) >= 2
         and convs[1]["trigger"] == "episode"),
        ("act 2: Steady asked twice, got no answer, and escalated", len(convs) >= 2 and convs[1]["outcome"] == "no_response"
         and convs[1]["escalated"] == 1),
        ("act 2: the caregiver call was raised", len(calls) == 1),
    ]
    if live:
        checks += [
            ("act 2: the caregiver answered and confirmed on the phone", bool(calls) and calls[0]["status"] == "done"),
            ("act 2: afterwards a text AND an email were really sent to the caregiver",
             report is not None and report.status == "done" and sorted(d["channel"] for d in report.deliveries if d["ok"]) == ["email", "imessage"]),
        ]
    else:
        checks += [("act 2: afterwards the text and the email were written (dry run: outbox only)",
                    report is not None and report.status == "done" and len([d for d in report.deliveries if d["ok"]]) == 2)]
    checks.append(("no samples lost", steady.ring.lost == 0))
    print("\n--- verdict ---")
    for name, ok in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(asyncio.run(main("--live" in sys.argv)))
