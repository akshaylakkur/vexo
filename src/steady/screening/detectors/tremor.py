"""Tremor detection per window (SPEC 4.6).

A window is a tremor window when the wrist's turning is dominated by ONE steady rhythm in
the tremor range, strong enough to be real and not so violent that it must be a deliberate
action. Which kind depends on the rhythm and on what the arm is doing:

  rest tremor    arm at rest, rhythm 3.5-7.5 Hz          (the classic Parkinson's sign)
  action tremor  arm at rest/held, rhythm 7.5-12 Hz      (postural, essential-tremor range)
                 arm in use, rhythm 4-12 Hz dominating the movement

Never while walking: arm swing and step impacts put rhythm into the same range.
All numbers are starting values; calibration replaces `noise_floor_dps`.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import flags as F
from ..features import Features


@dataclass
class TremorParams:
    rest_band: tuple[float, float] = (3.5, 7.5)
    action_band: tuple[float, float] = (4.0, 12.0)
    min_ratio: float = 0.5          # share of 0.5-15 Hz power inside the tremor band
    min_conc: float = 0.4           # share of power within +-1 Hz of the peak (one rhythm, not noise)
    min_prom: float = 5.0           # peak vs median spectrum
    min_amp_dps: float = 4.0        # absolute floor, deg/s RMS in 3.5-12 Hz
    noise_factor: float = 3.0       # ... or this many times the calibrated noise floor
    noise_floor_dps: float | None = None
    max_amp_dps: float = 150.0      # beyond this it is a deliberate, vigorous action
    max_acc_dyn_g: float = 0.35     # likewise: the whole forearm is being thrown about
    mild_below_dps: float = 10.0
    marked_above_dps: float = 30.0

    @property
    def amp_floor(self) -> float:
        if self.noise_floor_dps:
            return max(self.min_amp_dps, self.noise_factor * self.noise_floor_dps)
        return self.min_amp_dps

    def grade(self, amp: float) -> str:
        if amp < self.mild_below_dps:
            return "mild"
        return "marked" if amp > self.marked_above_dps else "moderate"


class TremorDetector:
    def __init__(self, params: TremorParams | None = None) -> None:
        self.p = params or TremorParams()

    def reasons(self, f: Features, ctx: str) -> dict[str, bool]:
        """Each criterion on its own, for tuning and the validation report."""
        p = self.p
        return {
            "context": ctx in ("rest", "active"),
            "rhythmic": f.peak_conc >= p.min_conc and f.peak_prom >= p.min_prom,
            "strong_enough": f.tremor_amp >= p.amp_floor,
            "not_violent": f.tremor_amp <= p.max_amp_dps and f.acc_dyn_rms <= p.max_acc_dyn_g,
            "rest_band": p.rest_band[0] <= f.dom_freq <= p.rest_band[1] and f.rest_ratio >= p.min_ratio,
            "action_band": p.action_band[0] <= f.dom_freq <= p.action_band[1] and f.action_ratio >= p.min_ratio,
        }

    def __call__(self, f: Features, ctx: str, window=None) -> int:
        r = self.reasons(f, ctx)
        if not (r["context"] and r["rhythmic"] and r["strong_enough"] and r["not_violent"]):
            return 0
        if ctx == "rest":
            if r["rest_band"]:
                return F.REST_TREMOR
            if r["action_band"] and f.dom_freq > self.p.rest_band[1]:
                return F.ACTION_TREMOR          # held posture, fast rhythm
            return 0
        return F.ACTION_TREMOR if r["action_band"] else 0
