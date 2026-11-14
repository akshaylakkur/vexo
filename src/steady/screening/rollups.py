"""Hourly rollups (SPEC 4.8): what reports and the agent read instead of raw windows.

Accumulates every window in memory and upserts the current hour's row once a minute and
when the hour changes, so the database is never more than a minute behind.
"""
from __future__ import annotations

import statistics
import time

from ..store.db import Writer

from .flags import ACTION_TREMOR as FLAG_ACTION_TREMOR
from .flags import DYSKINESIA as FLAG_DYSKINESIA
from .flags import FALL as FLAG_FALL
from .flags import REST_TREMOR as FLAG_REST_TREMOR


class HourlyRollup:
    def __init__(self, writer: Writer, hop_s: float, flush_every_s: float = 60.0) -> None:
        self.writer = writer
        self.hop_s = hop_s
        self.flush_every_s = flush_every_s
        self._hour: float | None = None
        self._last_flush = 0.0
        self._reset()

    def _reset(self) -> None:
        self.windows = 0
        self.valid = 0
        self.secs = {"worn": 0.0, "rest": 0.0, "active": 0.0, "walking": 0.0,
                     "rest_tremor": 0.0, "action_tremor": 0.0, "dysk": 0.0}
        self.tremor_amps: list[float] = []
        self.vigors: list[float] = []
        self.swings: list[float] = []
        self.step_regs: list[float] = []
        self.steps = 0
        self.falls = 0
        self.valid_fracs: list[float] = []

    def add(self, row: dict) -> None:
        hour = float(int(row["t_end"] // 3600) * 3600)
        if self._hour is not None and hour != self._hour:
            self.flush()
            self._reset()
        self._hour = hour
        self.windows += 1
        self.valid_fracs.append(row.get("valid_frac") or 0.0)
        self.steps += max(0, row.get("steps_delta") or 0)
        if row["valid"]:
            self.valid += 1
            ctx = row["context"]
            if ctx != "not_worn":
                self.secs["worn"] += self.hop_s
            if ctx in ("rest", "active", "walking"):
                self.secs[ctx] += self.hop_s
            flags = row.get("flags") or 0
            if flags & FLAG_REST_TREMOR:
                self.secs["rest_tremor"] += self.hop_s
                self.tremor_amps.append(row["tremor_amp"])
            if flags & FLAG_ACTION_TREMOR:
                self.secs["action_tremor"] += self.hop_s
            if flags & FLAG_FALL:
                self.falls += 1
            if flags & FLAG_DYSKINESIA:
                self.secs["dysk"] += self.hop_s
            if ctx == "active" and row.get("vigor") is not None:
                self.vigors.append(row["vigor"])
            if ctx == "walking":
                if row.get("arm_swing_deg") is not None:
                    self.swings.append(row["arm_swing_deg"])
                if row.get("step_reg") is not None:
                    self.step_regs.append(row["step_reg"])
        if time.monotonic() - self._last_flush >= self.flush_every_s:
            self.flush()

    def flush(self) -> None:
        if self._hour is None or self.windows == 0:
            return
        self._last_flush = time.monotonic()
        med = lambda xs: statistics.median(xs) if xs else None  # noqa: E731
        s = self.secs
        row = {
            "hour_start": self._hour, "windows": self.windows, "valid_windows": self.valid,
            "worn_s": s["worn"], "rest_s": s["rest"], "active_s": s["active"], "walking_s": s["walking"],
            "rest_tremor_s": s["rest_tremor"], "action_tremor_s": s["action_tremor"],
            "tremor_pct_rest": (100.0 * s["rest_tremor"] / s["rest"]) if s["rest"] else None,
            "tremor_amp_mean": statistics.fmean(self.tremor_amps) if self.tremor_amps else None,
            "dysk_s": s["dysk"], "vigor_median": med(self.vigors), "steps": self.steps,
            "arm_swing_median": med(self.swings), "step_reg_median": med(self.step_regs),
            "freezes": 0, "falls": self.falls,
            "valid_frac": statistics.fmean(self.valid_fracs) if self.valid_fracs else None,
        }
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.writer.execute(f"INSERT OR REPLACE INTO hourly_rollups ({cols}) VALUES ({marks})", row.values())
