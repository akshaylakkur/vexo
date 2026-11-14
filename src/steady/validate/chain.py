"""The whole chain on the pretend band: detection -> policy -> conversation -> message.

    python -m steady.validate.chain

1. A Parkinson's-type rest tremor is played into the pretend band. The policy (demo
   thresholds) must open a check-in by itself. In it the wearer (scripted lines instead of a
   microphone) says they are OK and asks for a text to Priya, then says yes: OpenClaw must
   word it and Steady must deliver it (dry run: written to data/outbox.jsonl, never sent).
2. A fall is played and nobody answers. The urgent check-in must open, ask twice, close as
   no_response, and send NOTHING: there is no emergency path.
Needs the OpenClaw gateway that config/local.yaml points to.
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

SCRIPTS = [
    ["I'm okay, thanks, just a bit tired. Please text Priya that I had some shaking this afternoon but I'm fine.",
     "Yes.", "No, that's all. Goodbye."],
    [],                                     # the fall: nobody answers
]


async def main() -> int:
    cfg = config_mod.load(overrides={
        "band.transport": "sim", "storage.db_path": "data/chain.db", "delivery.mode": "dry_run", "mode": "demo",
        "brief.enabled": False, "policy.quiet_hours": ["00:00", "00:00"]})
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            (cfg.db_path.parent / (cfg.db_path.name + suffix)).unlink()
    steady = Steady(cfg)
    steady.band.sim_options = {"auto_tap": False}
    scripts = list(SCRIPTS)
    real_converse = steady.voice.converse

    async def converse(**kw):                       # scripted lines stand in for the band's microphone
        kw["ears"] = ScriptEars(scripts.pop(0) if scripts else [])
        kw["silence_s"] = 5
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
    ended: list[dict] = []
    q = steady.bus.subscribe("conversation.ended")

    async def wait_conversation(timeout: float) -> dict | None:
        try:
            _, c = await asyncio.wait_for(q.get(), timeout)
            ended.append(c)
            return c
        except asyncio.TimeoutError:
            return None

    player = Player()
    try:
        for _ in range(100):
            if steady.band.status.connected:
                break
            await asyncio.sleep(0.2)
        sim = steady.band.sim
        sim.set_activity("still", cadence=0)
        sim.play_motion(player)
        await asyncio.sleep(8)
        print("\n=== 1. rest tremor (5 Hz, 20 deg/s) ...", flush=True)
        t0 = time.time()
        player.switch(S.tremor(5.0, 20))
        c1 = await wait_conversation(150)
        player.switch(S.quiet_rest())
        print(f"check-in ended after {time.time() - t0:.0f} s: {c1 and c1['outcome']}", flush=True)
        for _ in range(120):                         # let OpenClaw finish the message
            if steady.tasks.tasks and all(t.status in ("done", "failed", "cancelled", "expired")
                                          for t in steady.tasks.tasks.values()):
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(12)                      # the tremor episode closes; spoken outcome plays
        print("\n=== 2. fall, nobody answers ...", flush=True)
        tasks_before = len(steady.tasks.tasks)
        player.switch(S.fall(3.0))
        c2 = await wait_conversation(90)
        await asyncio.sleep(5)                       # anything that would be sent would be created by now
        print(f"urgent check-in: {c2 and c2['outcome']}, escalated={c2 and c2['escalated']}", flush=True)
    finally:
        for t in bg:
            t.cancel()
        steady.screener.close()
        await steady.band.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(band_task), 5)
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(api, 5)
        await steady.voice.close()
        await steady.writer.close()

    db = sqlite3.connect(cfg.db_path)
    db.row_factory = sqlite3.Row
    eps = [dict(r) for r in db.execute("SELECT id, kind, severity, conversation_id FROM episodes ORDER BY id")]
    convs = [dict(r) for r in db.execute("SELECT id, trigger, episode_id, outcome, escalated FROM conversations ORDER BY id")]
    tasks = list(steady.tasks.tasks.values())
    print("\nepisodes:", eps, "\nconversations:", convs)
    for t in tasks:
        print(f"task {t.id}: {t.type} to {t.recipient.name} -> {t.status}")
        for d in t.deliveries:
            print(f"   {d['channel']} (written by {d['by']}): {d['text']}")
    tremor = next((e for e in eps if e["kind"] == "rest_tremor"), None)
    fall = next((e for e in eps if e["kind"] == "fall_suspected"), None)
    checks = [
        ("tremor detected", tremor is not None),
        ("policy opened a check-in for it by itself", bool(convs) and convs[0]["trigger"] == "episode"
         and tremor is not None and convs[0]["episode_id"] == tremor["id"]),
        ("check-in ended OK only after the person said so", bool(convs) and convs[0]["outcome"] == "ok"),
        ("episode linked to its conversation", tremor is not None and tremor["conversation_id"] == (convs[0]["id"] if convs else None)),
        ("spoken request became an approved text to Priya, worded by OpenClaw and delivered",
         len(tasks) >= 1 and tasks[0].type == "message_contact" and tasks[0].status == "done"
         and tasks[0].delivered and tasks[0].deliveries[0]["by"] == "openclaw"),
        ("fall detected", fall is not None),
        ("urgent check-in opened, asked twice, closed without an answer",
         len(convs) >= 2 and convs[1]["outcome"] == "no_response" and convs[1]["episode_id"] == (fall or {}).get("id")),
        ("nothing was sent after the fall (no emergency path)", len(tasks) == tasks_before == 1),
        ("no samples lost", steady.ring.lost == 0),
    ]
    print("\n--- verdict ---")
    for name, ok in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(asyncio.run(main()))
