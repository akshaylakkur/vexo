"""The screening loop (SPEC 4.2): one full assessment every hop (2 s), always logged.

Each tick takes the last `window_s` seconds (plus padding for the filters) from the ring
buffer, builds the window, computes features and context in a worker thread, writes one
`windows` row and publishes it on the bus.
Detectors (M2) plug in via `detectors`: callables (features, context, window) -> flag bits.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

import numpy as np

from ..band.ingest import RingBuffer
from ..band.manager import BandManager
from ..bus import Bus
from ..config import Config
from ..store.db import PRUNE_WINDOWS_SQL, Writer
from . import flags as F
from .context import ContextClassifier, ContextParams
from .detectors.fall import FallDetector, FallParams
from .detectors.tremor import TremorDetector, TremorParams
from .episodes import EpisodeTracker
from .features import Features, compute
from .preprocess import Window, build_window
from .rollups import HourlyRollup

log = logging.getLogger("steady.screen")
ARRIVAL_MARGIN_S = 0.15        # wait for the newest frames to arrive before cutting a window

Detector = Callable[[Features, str, Window], int]


class Screener:
    def __init__(self, cfg: Config, bus: Bus, writer: Writer, ring: RingBuffer, band: BandManager) -> None:
        self.cfg = cfg
        self.bus = bus
        self.writer = writer
        self.ring = ring
        self.band = band
        self.window_s = float(cfg.get("sampling.window_s", 4.0))
        self.hop_s = float(cfg.get("sampling.hop_s", 2.0))
        self.pad_s = float(cfg.get("sampling.pad_s", 2.0))
        self.context = ContextClassifier(ContextParams(**(cfg.get("context") or {})))
        self.rollup = HourlyRollup(writer, self.hop_s)
        thresholds = cfg.get("thresholds") or {}
        self.tremor = TremorDetector(TremorParams(**(thresholds.get("tremor") or {})))
        self.fall = FallDetector(FallParams(**(thresholds.get("fall") or {})))
        self.episodes = EpisodeTracker(writer, bus, self.tremor.p)
        self.detectors: list[Detector] = [self.tremor]
        self.last: dict | None = None
        self.ticks = 0
        self.status_every = max(1, int(cfg.get("sampling.status_every_ticks", 5)))    # a log line every 10 s
        self.logged = 0
        self.positives = 0
        self.keep_last = int(cfg.get("storage.keep_last_assessments", 2000))
        self.prune_every = int(cfg.get("storage.prune_every_ticks", 30))
        self.compute_ms = 0.0
        self._last_steps: int | None = None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time() + self.hop_s
        while True:
            await asyncio.sleep(max(0.0, next_tick - loop.time()))
            next_tick += self.hop_s
            if loop.time() > next_tick:  # fell behind (e.g. the Mac slept): realign
                next_tick = loop.time() + self.hop_s
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("screening tick failed")

    async def tick(self) -> dict:
        t_end = time.time() - ARRIVAL_MARGIN_S
        latest = self.ring.latest_time()
        rows = self.ring.since(t_end - self.window_s - self.pad_s - 0.5, t_end)
        epoch_ok = self.band.link_epoch_time <= t_end - self.window_s - self.pad_s
        result = await asyncio.to_thread(self._analyse, rows, t_end)
        window, feats, ctx, flags = result
        falls = await asyncio.to_thread(self.fall.update, self.ring, t_end) if self.band.status.connected else []
        for ev in falls:
            flags |= F.FALL if ev.kind == "fall_suspected" else F.HARD_IMPACT

        activity = self.band.activity
        steps_delta = None
        if activity is not None:
            if self._last_steps is not None and activity.steps >= self._last_steps:
                steps_delta = activity.steps - self._last_steps
            self._last_steps = activity.steps

        valid = bool(window is not None and feats is not None and self.band.status.connected
                     and epoch_ok and window.valid_frac >= 0.9)
        row = {
            "session_id": self.band.status.session_id,
            "t_start": t_end - self.window_s, "t_end": t_end,
            "valid": int(valid),
            "valid_frac": round(window.valid_frac, 4) if window else 0.0,
            "n_samples": window.n_samples if window else 0,
            "lost_samples": window.lost_samples if window else 0,
            "context": ctx if valid else "unknown",
            "activity_state": activity.state if activity else None,
            "cadence": activity.cadence if activity else None,
            "steps_delta": steps_delta,
            "flags": flags if valid else 0,
            "battery": self.band.status.battery,
            "rssi": self.band.status.rssi,
        }
        if feats is not None:
            row.update(feats.as_row())
        # Every assessment is saved. Only the newest `keep_last` stay; older ones are deleted
        # as we go, except positive ones (flags != 0), which are kept for later analysis.
        self.writer.insert("windows", row)
        self.logged += 1
        self.positives += 1 if row["flags"] else 0
        self.rollup.add(row)
        self.ticks += 1
        if self.ticks % self.prune_every == 0:
            self.writer.execute(PRUNE_WINDOWS_SQL, (self.keep_last,))
        self.last = row
        if row["flags"] or self.ticks % self.status_every == 1:      # the terminal is the only display
            num = lambda k: "-" if row.get(k) is None else f"{row[k]:.1f}"  # noqa: E731
            log.info("%s | rhythm %s Hz, tremor %s deg/s | %s%s", row["context"], num("dom_freq"),
                     num("tremor_amp"), ", ".join(F.names(row["flags"])) or "nothing detected",
                     f" | battery {row['battery']}%" if row["battery"] is not None else "")
        await self.episodes.update(row)
        for ev in falls:
            await self.episodes.add_fall(ev)
        live = dict(row)
        live["detections"] = F.names(row["flags"])
        live["checks"] = self.tremor.reasons(feats, row["context"]) if feats is not None and valid else None
        live["data_age_s"] = None if latest is None else round(time.time() - latest, 2)
        live["compute_ms"] = round(self.compute_ms, 1)
        self.bus.publish("window", live)
        return row

    def _analyse(self, rows: np.ndarray, t_end: float):
        t0 = time.perf_counter()
        window = build_window(rows, t_end, self.window_s, self.pad_s) if len(rows) else None
        if window is None or window.valid_frac < 0.5:
            self.compute_ms = (time.perf_counter() - t0) * 1000
            return window, None, "unknown", 0
        act = self.band.activity
        walking_guess = act is not None and act.state in ("walk", "run")
        feats, _ = compute(window, walking=walking_guess)
        ctx = self.context.classify(feats, t_end, act)
        flags = 0
        for detect in self.detectors:
            flags |= int(detect(feats, ctx, window))
        self.compute_ms = (time.perf_counter() - t0) * 1000
        return window, feats, ctx, flags

    def close(self) -> None:
        self.episodes.close_all()
        self.rollup.flush()
        self.writer.execute(PRUNE_WINDOWS_SQL, (self.keep_last,))
