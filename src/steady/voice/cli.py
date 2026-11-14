"""`python -m steady say ...`: speak in the headphones and print the timings."""
from __future__ import annotations

import asyncio
import time

from ..config import Config
from .speaker import Speaker


async def _words(text: str, per_second: float = 25.0):
    """The text a few characters at a time, the way tokens arrive from a model."""
    for word in text.split(" "):
        yield word + " "
        await asyncio.sleep(1.0 / per_second)


async def say(cfg: Config, text: str, stream: bool = False, interrupt_after: float | None = None) -> int:
    speaker = Speaker.from_config(cfg)
    t0 = time.perf_counter()
    await speaker.start()
    print(f"Output: {speaker.player.device_name} (connected in {(time.perf_counter() - t0) * 1000:.0f} ms)")
    try:
        task = asyncio.create_task(speaker.say_stream(_words(text)) if stream else speaker.say(text))
        silence_ms = None
        if interrupt_after is not None:
            while speaker.player.first_audio_at is None and not task.done():
                await asyncio.sleep(0.01)
            await asyncio.sleep(interrupt_after)
            t = time.perf_counter()
            dropped = await speaker.stop()
            silence_ms = (time.perf_counter() - t) * 1000
            print(f"Interrupted: {dropped:.2f} s of queued speech dropped")
        res = await task
        fmt = lambda v: "-" if v is None else f"{v:.0f} ms"  # noqa: E731
        print(f"pieces {res.pieces} | first piece ready {fmt(res.first_piece_ms)} | "
              f"Deepgram first audio {fmt(res.first_byte_ms)} | first sound in headphones {fmt(res.first_audio_ms)} | "
              f"audio {res.audio_s:.1f} s | total {res.total_s:.1f} s | underruns {speaker.player.buffer.underruns}"
              + (f" | stop took {silence_ms:.0f} ms" if silence_ms is not None else ""))
        return 0
    finally:
        await speaker.close()


async def talk(cfg: Config, says: list[str] | None = None, trigger: str = "double_tap",
               kind: str = "rest_tremor", sample_speech: bool = False, silence_s: float | None = None,
               max_s: float | None = None) -> int:
    """Run one conversation now and print its transcript and timings.

    says: typed lines in place of the microphone ('<tap>' taps the band, '<silence>' says nothing).
    trigger "episode": pretend the band just detected `kind`.
    """
    import contextlib

    from ..app import Steady
    from ..kitpath import KIT
    from .ears import ScriptEars

    steady = Steady(cfg)
    if cfg.transport == "sim":
        steady.band.sim_options = {"auto_tap": False}
        if sample_speech:
            from vexo_open import audio
            steady.band.sim_options["audio_packets"] = audio.ogg_packets(KIT / "dashboard" / "speech" / "sample-speech.opus")
    await steady.writer.start()
    tasks = [asyncio.create_task(steady.band.run()), asyncio.create_task(steady.screener.run())]
    # M6: OpenClaw needs steadyd's API to fetch and deliver tasks this conversation creates
    import uvicorn

    from ..api.server import create_app
    server = uvicorn.Server(uvicorn.Config(create_app(steady), host="127.0.0.1", port=int(cfg.get("api.port", 8765)),
                                           log_level="warning", lifespan="off", timeout_graceful_shutdown=2))
    server.install_signal_handlers = lambda: None
    await steady.tasks.start()
    extra = [asyncio.create_task(c) for c in (server.serve(), steady.tasks.watch(), steady.announcer.run())]
    out = None
    try:
        for _ in range(150):
            if steady.band.status.connected:
                break
            await asyncio.sleep(0.2)
        else:
            print("The band did not connect:", steady.band.status.last_error)
            return 1
        episode = None
        if trigger == "episode":
            episode = {"id": None, "kind": kind, "duration_s": 70,
                       "metrics": {"grade": "moderate", "freq_mean_hz": 4.8, "amp_peak_dps": 18}}
        out = await steady.voice.converse(trigger=trigger, episode=episode, urgent=kind == "fall_suspected" and trigger == "episode",
                                          ears=ScriptEars(says) if says else None, silence_s=silence_s, max_s=max_s)
        for _ in range(240):                                  # let tasks from this conversation finish
            await asyncio.sleep(0.5)
            open_tasks = [t for t in steady.tasks.tasks.values()
                          if t.status not in ("done", "failed", "cancelled", "expired", "needs_confirmation")]
            if not open_tasks and steady.announcer._q.empty():
                break
        await asyncio.sleep(4.0)                              # ... and the spoken outcome
    finally:
        server.should_exit = True
        for t in extra[1:]:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(extra[0], 5)
        tasks[1].cancel()
        steady.screener.close()
        await steady.band.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(tasks[0]), 5)
        for t in tasks:
            t.cancel()
        await steady.voice.close()
        await steady.writer.close()
    if out is None:
        return 1
    print("\n--- transcript ---")
    for t in out.turns:
        extra = f"   [{t.latency_ms:.0f} ms to first sound]" if t.latency_ms is not None else ""
        extra += "   [interrupted]" if t.interrupted else ""
        print(f"{t.role:>9}: {t.text}{extra}")
    lat = out.latencies
    for t in steady.tasks.tasks.values():
        print(f"     task {t.id}: {t.type} to {t.recipient.name} -> {t.status} ({t.note})")
        for d in t.deliveries:
            print(f"            delivered by {d['by']} via {d['channel']}: {d['text']}")
    print(f"\noutcome: {out.outcome}{' (escalated)' if out.escalated else ''} | summary: {out.summary or '-'} | "
          f"{out.seconds:.0f} s | reply latency: " +
          (f"median {sorted(lat)[len(lat) // 2]:.0f} ms, worst {max(lat):.0f} ms over {len(lat)} replies" if lat else "-"))
    return 0
