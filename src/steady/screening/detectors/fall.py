"""Fall detection on the raw 208 Hz samples (SPEC 4.6).

Three stages, all required for `fall_suspected`:
  1. free-fall   total acceleration below 0.35 g for at least 80 ms
  2. impact      a peak of at least 3 g within 1 s after the free-fall
  3. aftermath   in the 10 s after the impact: lying still, or the arm's orientation
                 changed by more than 45 degrees compared with before the fall
Free-fall + impact without the aftermath is a `hard_impact` (logged, not a symptom).

`update(ring, now)` is called every screening tick; it returns the events decided in that
tick. Needs the accelerometer range at 8 g, or impacts clip at 2 g.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ...band.ingest import RingBuffer


@dataclass
class FallParams:
    freefall_g: float = 0.35
    freefall_min_s: float = 0.08
    impact_g: float = 3.0
    impact_within_s: float = 1.0
    aftermath_s: float = 10.0
    settle_s: float = 2.0             # ignore the bounce right after the impact
    still_dyn_g: float = 0.08
    orientation_deg: float = 45.0
    refractory_s: float = 15.0        # one fall, one event


@dataclass
class FallEvent:
    kind: str                         # fall_suspected | hard_impact
    t_freefall: float
    t_impact: float
    metrics: dict = field(default_factory=dict)


def _angle_deg(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    return math.degrees(math.acos(float(np.clip(np.dot(a, b) / (na * nb), -1.0, 1.0))))


class FallDetector:
    def __init__(self, params: FallParams | None = None) -> None:
        self.p = params or FallParams()
        self._scanned_to = 0.0
        self._pending: list[tuple[float, float, float, float]] = []   # t_ff, t_impact, peak_g, ff_s
        self._quiet_until = 0.0

    def reset(self) -> None:
        self._pending.clear()

    def update(self, ring: RingBuffer, now: float) -> list[FallEvent]:
        p = self.p
        # 1+2: scan new samples (re-reading 1.5 s so a free-fall near the edge is seen whole)
        rows = ring.since(max(self._scanned_to - 1.5, now - 30.0), now)
        if len(rows) > 10:
            t = rows[:, 0]
            mag = np.linalg.norm(rows[:, 2:5], axis=1)
            low = mag < p.freefall_g
            i, n = 0, len(rows)
            while i < n:
                if not low[i]:
                    i += 1
                    continue
                j = i
                while j + 1 < n and low[j + 1]:
                    j += 1
                t_ff, dur = t[i], t[j] - t[i]
                if dur >= p.freefall_min_s and t_ff > self._quiet_until:
                    after = (t > t[j]) & (t <= t[j] + p.impact_within_s)
                    whole = t[-1] >= t[j] + p.impact_within_s        # the impact window has fully arrived
                    if after.any() and mag[after].max() >= p.impact_g:
                        k = int(np.argmax(np.where(after, mag, 0.0)))
                        self._pending.append((float(t_ff), float(t[k]), float(mag[k]), float(dur)))
                        self._quiet_until = float(t[k]) + p.refractory_s
                    elif whole:
                        self._quiet_until = float(t[j])              # a free-fall with no impact: a toss, a drop of the arm
                i = j + 1
            self._scanned_to = float(t[-1])

        # 3: decide pending impacts once their aftermath has arrived
        events: list[FallEvent] = []
        still_pending = []
        for t_ff, t_imp, peak, dur in self._pending:
            if now < t_imp + p.aftermath_s:
                still_pending.append((t_ff, t_imp, peak, dur))
                continue
            post = ring.since(t_imp + p.settle_s, t_imp + p.aftermath_s)
            pre = ring.since(t_ff - 3.0, t_ff - 0.5)
            if len(post) < 50:
                continue                                              # the link dropped: cannot judge
            acc = post[:, 2:5]
            g_post = acc[len(acc) // 2:].mean(axis=0)
            dyn = acc - acc.mean(axis=0)
            dyn_rms = float(np.sqrt(np.mean(np.sum(dyn ** 2, axis=1))))
            turned = _angle_deg(pre[:, 2:5].mean(axis=0), g_post) if len(pre) > 20 else 0.0
            still = dyn_rms < p.still_dyn_g
            kind = "fall_suspected" if (still or turned > p.orientation_deg) else "hard_impact"
            events.append(FallEvent(kind, t_ff, t_imp, {
                "impact_g": round(peak, 2), "freefall_s": round(dur, 3),
                "aftermath_dyn_g": round(dyn_rms, 4), "orientation_change_deg": round(turned, 1),
                "still": still}))
        self._pending = still_pending
        return events
