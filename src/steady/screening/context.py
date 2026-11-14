"""Context classification (SPEC 4.5): what is the arm doing in this window?

Order: not_worn -> walking -> rest -> active. Detectors only run in their own context.
"""
from __future__ import annotations

from dataclasses import dataclass

from .features import Features


@dataclass
class ContextParams:
    rest_vol_rms_dps: float = 10.0
    walking_min_cadence: float = 60.0
    not_worn_gyro_rms_dps: float = 0.5
    not_worn_acc_dyn_rms_g: float = 0.003
    not_worn_after_s: float = 600.0
    activity_max_age_s: float = 3.0


@dataclass
class Activity:
    time: float
    state: str          # still | walk | run
    cadence: float
    steps: int


class ContextClassifier:
    def __init__(self, params: ContextParams) -> None:
        self.p = params
        self._still_since: float | None = None

    def reset(self) -> None:
        self._still_since = None

    def classify(self, f: Features, t_end: float, activity: Activity | None) -> str:
        p = self.p
        motionless = f.gyro_rms < p.not_worn_gyro_rms_dps and f.acc_dyn_rms < p.not_worn_acc_dyn_rms_g
        if motionless:
            if self._still_since is None:
                self._still_since = t_end
            if t_end - self._still_since >= p.not_worn_after_s:
                return "not_worn"
        else:
            self._still_since = None

        fresh = activity is not None and (t_end - activity.time) <= p.activity_max_age_s
        if fresh and activity.state in ("walk", "run") and activity.cadence > p.walking_min_cadence:
            return "walking"
        if f.vol_rms < p.rest_vol_rms_dps:
            return "rest"
        return "active"
