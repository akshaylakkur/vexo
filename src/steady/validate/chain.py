"""The whole chain on the pretend band: detection -> policy -> conversation -> message.

    python -m steady.validate.chain            # dry run: messages go to data/outbox.jsonl
    python -m steady.validate.chain --live     # REALLY sends the text, the email and the brief

1. A Parkinson's-type rest tremor is played into the pretend band. The policy (demo
   thresholds) must open a check-in by itself. In it the wearer (scripted lines instead of a
   microphone) says they are OK and asks for a text to Priya, then says yes: OpenClaw must
   word it and Steady must deliver it (dry run: written to data/outbox.jsonl, never sent).
2. A fall is played and nobody answers. The urgent check-in must open, ask twice, close as
   no_response, and send no message. (The pretend band never phones anyone either.)
With --live the wearer also asks for an email, the end-of-day brief is sent (text and
email), and everything is really delivered to the people in config/contacts.yaml.
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



def scripts(live: bool, book) -> list[list[str]]:
    """What the wearer says in each conversation (scripted lines instead of a microphone)."""
    carer = book.find("caregiver")
    first = [f"I'm okay, thanks, just a bit tired. Please text {carer.name} that I had some shaking this afternoon "
             "but I'm fine.", "Yes."]
    if live:
        other = next((c for c in book.items if c.email and c is not carer), carer)
        first += [f"Also, please email {other.name} to say my tremor was a little stronger today and ask if we "
                  "should talk about it at my next appointment.", "Yes."]
    first.append("No, that's all. Goodbye.")
    return [first, []]                      # the second conversation is the fall: nobody answers


async def main(live: bool = False) -> int:
    cfg = config_mod.load(overrides={
        "band.transport": "sim", "storage.db_path": "data/chain.db", "mode": "demo",
        "delivery.mode": "live" if live else "dry_run", "brief.enabled": False,
        "brief.via": ["imessage", "email"], "policy.quiet_hours": ["00:00", "00:00"]})
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            (cfg.db_path.parent / (cfg.db_path.name + suffix)).unlink()
    steady = Steady(cfg)
    steady.band.sim_options = {"auto_tap": False}
    from ..actions.contacts import Contacts, mask
    book = Contacts.load()
    lines = scripts(live, book)
    if live:
        print("LIVE: real messages will be sent as", book.sender.name, "to:")
        for c in book.items:
            print(f"  {c.name} ({c.role}): text {mask(c.phone)} | email {mask(c.email)}")
    real_converse = steady.voice.converse

    async def converse(**kw):                       # scripted lines stand in for the band's microphone
        kw["ears"] = ScriptEars(lines.pop(0) if lines else [])
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
        c1 = await wait_conversation(300)
        player.switch(S.quiet_rest())
        print(f"check-in ended after {time.time() - t0:.0f} s: {c1 and c1['outcome']}", flush=True)
        for _ in range(120):                         # let OpenClaw finish the message
            if steady.tasks.tasks and all(t.status in ("done", "failed", "cancelled", "expired")
                                          for t in steady.tasks.tasks.values()):
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(12)                      # the tremor episode closes; spoken outcome plays
        brief_task = None
        if live:
            print("\n=== end-of-day brief ...", flush=True)
            out = await steady.brief.send_now(force=True)
            print("scheduler:", out, flush=True)
            brief_task = out.get("task_id")
            for _ in range(240):
                if brief_task is None or steady.tasks.tasks[brief_task].status in ("done", "failed"):
                    break
                await asyncio.sleep(0.5)
            await asyncio.sleep(6)
        print("\n=== 2. fall, nobody answers ...", flush=True)
        tasks_before = len(steady.tasks.tasks)
        player.switch(S.fall(3.0))
        c2 = await wait_conversation(90)
        await asyncio.sleep(8)                       # anything that would be sent would be created by now
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
    calls = [dict(r) for r in db.execute("SELECT status, result_note, episode_id FROM tasks WHERE type='caregiver_call'")]
    print("\nepisodes:", eps, "\nconversations:", convs)
    for t in tasks:
        print(f"\ntask {t.id}: {t.type} to {t.recipient.name} -> {t.status} ({t.note})")
        for d in t.deliveries:
            print(f"   {d['channel']}: {'DELIVERED' if d['ok'] else 'NOT delivered'} ({d['detail']}), written by {d['by']}"
                  + (f"\n   subject: {d['subject']}" if d.get("subject") else "") + f"\n   > {d['text']}")
    print("\ncaregiver phone call after the fall:", calls)
    print("spoken in the headphones:", steady.announcer.spoken)
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
        ("no message was sent after the fall", len(tasks) == tasks_before),
        ("the fall raised the caregiver phone call (the pretend band does not dial: see call_local)",
         len(calls) == 1 and calls[0]["episode_id"] == (fall or {}).get("id") and "pretend band" in (calls[0]["result_note"] or "")),
        ("no samples lost", steady.ring.lost == 0),
    ]
    if live:
        by_type = {t.type: t for t in tasks}
        ok_all = lambda t, n: t is not None and t.status == "done" and sum(d["ok"] for d in t.deliveries) == n \
            and all(d["by"] == "openclaw" for d in t.deliveries)  # noqa: E731
        checks[4] = ("spoken request became an approved text, worded by OpenClaw and REALLY sent by iMessage",
                     ok_all(by_type.get("message_contact"), 1))
        checks += [("spoken request became an approved email, REALLY sent", ok_all(by_type.get("email_contact"), 1)),
                   ("end-of-day brief REALLY sent by text and by email", ok_all(by_type.get("daily_brief"), 2))]
    print("\n--- verdict ---")
    for name, ok in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(asyncio.run(main(live="--live" in sys.argv)))
