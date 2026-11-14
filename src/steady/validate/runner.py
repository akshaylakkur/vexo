"""Runs the validation protocol through the whole pipeline and scores it.

Simulated run: every phase's motion model is played into the pretend band, so the data
takes the same road as on the real band: protocol frames -> ring buffer -> windows ->
features -> context -> detectors -> episodes -> database.
Live run (--live): the same phases, but the wearer of the real band performs them, guided
by spoken instructions (and clicks to pace a tremor) in the headphones.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..app import Steady
from ..config import Config
from ..kitpath import ROOT
from . import signals as S
from .protocol import Phase, phases

log = logging.getLogger("steady.validate")
SYMPTOMS = ("rest_tremor", "action_tremor", "fall_suspected")
OUT = ROOT / "data" / "validation"


class Player:
    """One endless motion for the pretend band; `switch` changes what it plays."""

    def __init__(self) -> None:
        self.motion = S.quiet_rest()
        self.s = 0.0
        self.s0 = 0.0

    def __call__(self, s: float) -> dict:
        self.s = s
        return self.motion(s - self.s0)

    def switch(self, motion) -> None:
        self.s0 = self.s
        self.motion = motion


@dataclass
class Result:
    phase: Phase
    t0: float
    t1: float
    windows: list[dict] = field(default_factory=list)
    episodes: list[dict] = field(default_factory=list)

    @property
    def valid(self) -> list[dict]:
        return [w for w in self.windows if w["valid"]]

    def flagged(self, name: str | None = None) -> int:
        return sum(1 for w in self.valid if (name in w["detections"] if name else
                                             any(d in SYMPTOMS for d in w["detections"])))

    @property
    def symptom_episodes(self) -> list[dict]:
        return [e for e in self.episodes if e["kind"] in SYMPTOMS]

    @property
    def detected(self) -> str | None:
        kinds = [e["kind"] for e in self.symptom_episodes]
        if self.phase.expect in kinds:
            return self.phase.expect
        return kinds[0] if kinds else None

    @property
    def passed(self) -> bool:
        return self.detected == self.phase.expect

    def med(self, key: str) -> float | None:
        vals = [w[key] for w in self.valid if w.get(key) is not None]
        return round(statistics.median(vals), 2) if vals else None

    def contexts(self) -> str:
        counts: dict[str, int] = {}
        for w in self.valid:
            counts[w["context"]] = counts.get(w["context"], 0) + 1
        return ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])) or "-"


async def _say(text: str) -> None:
    print(f"\n>>> {text}", flush=True)
    if sys.platform == "darwin":
        with contextlib.suppress(OSError):
            proc = await asyncio.create_subprocess_exec("say", "-r", "185", text)
            await proc.wait()


def _metronome(hz: float, seconds: float) -> None:
    """Clicks on the default output (the headphones) to pace a mimed tremor."""
    with contextlib.suppress(Exception):
        import numpy as np
        import sounddevice as sd
        rate = 24000
        buf = np.zeros(int(seconds * rate), dtype=np.float32)
        click = (0.5 * np.sin(2 * np.pi * 1500 * np.arange(int(0.012 * rate)) / rate)).astype(np.float32)
        for k in range(int(seconds * hz)):
            i = int(k * rate / hz)
            buf[i:i + len(click)] = click[:len(buf) - i]
        sd.play(buf, rate)


async def run(cfg: Config, live: bool = False, only: list[str] | None = None, gap_s: float = 8.0) -> int:
    todo = [p for p in phases() if (not only or p.key in only) and (not live or p.live)]
    if not todo:
        print("no phases selected")
        return 2
    steady = Steady(cfg)
    player = Player()
    if not live:
        steady.band.sim_options = {"auto_tap": False}
    await steady.writer.start()
    tasks = [asyncio.create_task(steady.band.run(), name="band"),
             asyncio.create_task(steady.screener.run(), name="screener")]
    windows: list[dict] = []
    episodes: list[dict] = []
    q = steady.bus.subscribe("window", "episode.started")

    async def collect() -> None:
        while True:
            topic, payload = await q.get()
            if topic == "window":
                windows.append(payload)
            else:                                   # an episode begins before it is announced:
                episodes.append({**payload, "announced_at": time.time()})   # score it where it was announced
    tasks.append(asyncio.create_task(collect(), name="collect"))

    results: list[Result] = []
    try:
        for _ in range(300 if live else 100):
            if steady.band.status.connected:
                break
            await asyncio.sleep(0.2)
        else:
            print("The band did not connect:", steady.band.status.last_error)
            return 1
        sim = steady.band.sim
        if sim is not None:
            sim.set_activity("still", cadence=0)
            sim.play_motion(player)
        total = sum(p.seconds + gap_s for p in todo) + 10
        print(f"Validation: {len(todo)} phases, about {total / 60:.1f} minutes, on "
              f"{'the real band' if live else 'the pretend band'}.", flush=True)
        if live:
            await _say("Validation starts. Wear the band on your wrist and follow the instructions.")
        await asyncio.sleep(8.0)                                    # fill the first windows at rest

        for i, ph in enumerate(todo, 1):
            if live:
                await _say(ph.live)
                await asyncio.sleep(1.5)
                if ph.metronome_hz:
                    _metronome(ph.metronome_hz, ph.seconds)
            else:
                sim.set_activity(ph.activity, cadence=105 if ph.activity == "walk" else 0)
                player.switch(ph.motion)
            t0 = time.time()
            print(f"[{i}/{len(todo)}] {ph.title} ...", flush=True)
            if live and ph.key == "fall":
                await asyncio.sleep(3.0)
                await _say("now")
                await asyncio.sleep(max(0.0, ph.seconds - (time.time() - t0)))
            else:
                await asyncio.sleep(ph.seconds)
            t1 = time.time()
            results.append(Result(ph, t0, t1))
            if live:
                await _say("Stop. Relax your arm.")
            else:
                sim.set_activity("still", cadence=0)
                player.switch(S.quiet_rest())
            await asyncio.sleep(gap_s)
        await asyncio.sleep(6.0)                                    # let the last windows and episodes arrive
    finally:
        for t in tasks:
            if t.get_name() in ("screener", "collect"):
                t.cancel()
        steady.screener.close()
        await steady.band.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(tasks[0]), 5)
        for t in tasks:
            if not t.done():
                t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await steady.writer.close()

    for r in results:
        r.windows = [w for w in windows if w["t_start"] >= r.t0 + 0.5 and w["t_end"] <= r.t1 + 0.2]
        r.episodes = [e for e in episodes if r.t0 <= e["announced_at"] <= r.t1 + 3.0]
    return report(results, live, steady)


def report(results: list[Result], live: bool, steady: Steady) -> int:
    scored = [r for r in results if not r.phase.limit]
    pos = [r for r in scored if r.phase.expect]
    neg = [r for r in scored if not r.phase.expect]
    limits = [r for r in results if r.phase.limit]
    lines = [f"# Steady validation: {'real band (live)' if live else 'pretend band (simulated signals)'}", "",
             time.strftime("Run at %Y-%m-%d %H:%M:%S"), "",
             "| Phase | Expected | Detected | Result | Flagged windows | Context | Median rhythm | Median tremor amp |",
             "|---|---|---|---|---|---|---|---|"]

    def row(r: Result) -> str:
        verdict = ("as expected" if r.passed else "NOT as expected") if r.phase.limit else ("PASS" if r.passed else "FAIL")
        return (f"| {r.phase.title} | {r.phase.expect or 'nothing'} | {r.detected or 'nothing'} | {verdict} | "
                f"{r.flagged()}/{len(r.valid)} | {r.contexts()} | {r.med('dom_freq')} Hz | {r.med('tremor_amp')} deg/s |")

    lines += [row(r) for r in scored]
    if limits:
        lines += ["", "Known limits of a single wrist sensor (not scored):", "",
                  "| Phase | Expected | Detected | Result | Flagged windows | Context | Median rhythm | Median tremor amp |",
                  "|---|---|---|---|---|---|---|---|"] + [row(r) for r in limits]
        lines += [""] + [f"- {r.phase.title}: {r.phase.note}" for r in limits]
    tp, tn = sum(r.passed for r in pos), sum(r.passed for r in neg)
    lines += ["", f"**Positives detected: {tp}/{len(pos)}. Negatives left alone: {tn}/{len(neg)}.**"]
    neg_flagged = sum(r.flagged() for r in neg)
    neg_windows = sum(len(r.valid) for r in neg)
    lines.append(f"Window level, negatives: {neg_flagged} of {neg_windows} assessments flagged.")
    sc = steady.screener
    lines.append(f"Pipeline: {sc.ticks} assessments, {sc.positives} positive, {sc.episodes.count} episodes, "
                 f"{steady.ring.lost} samples lost.")
    for r in pos:
        for e in r.symptom_episodes:
            delay = e["announced_at"] - r.t0
            lines.append(f"- {r.phase.key}: {e['kind']} ({e['severity']}) announced {delay:.1f} s into the phase, {e['metrics']}")
    text = "\n".join(lines)
    print("\n" + text)
    OUT.mkdir(parents=True, exist_ok=True)
    stem = OUT / time.strftime(f"{'live' if live else 'sim'}-%Y%m%d-%H%M%S")
    stem.with_suffix(".md").write_text(text + "\n")
    stem.with_suffix(".json").write_text(json.dumps([{
        "key": r.phase.key, "title": r.phase.title, "expect": r.phase.expect, "detected": r.detected,
        "passed": r.passed, "limit": r.phase.limit, "t0": r.t0, "t1": r.t1, "valid_windows": len(r.valid),
        "flagged": r.flagged(), "episodes": r.episodes,
        "windows": r.windows} for r in results], indent=1))
    print(f"\nSaved: {Path(stem).relative_to(ROOT)}.md / .json")
    return 0 if all(r.passed for r in scored) else 1
