"""Policy rules (SPEC 5): which findings also get a voice check-in, and when to hold back.

    fall_suspected                          urgent   speak at once, whatever else is going on
    tremor, moderate or worse, >= 60 s      concern  speak, unless a guardrail holds it back
    tremor, mild or shorter                 notice   logged only (for the daily brief)

Guardrails for non-urgent findings, in order: band out of reach, band not worn, a conversation
already running, quiet hours, the cooldown for that kind of finding, at most 3 check-ins an
hour. A finding held back by the band, a running conversation, quiet hours or the hourly
limit is "deferred": the next conversation mentions it. Each episode gets one decision once
it reaches concern. Pure logic with the clock passed in, so it is tested without a band.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

RANK = {"info": 0, "notice": 1, "concern": 2, "urgent": 3}
TREMORS = ("rest_tremor", "action_tremor")


@dataclass
class PolicyParams:
    quiet_hours: tuple[str, str] = ("22:00", "07:00")
    cooldown_min: dict = field(default_factory=lambda: {"default": 30, "rest_tremor": 45})
    max_checkins_per_hour: int = 3
    tremor_min_s: float = 60.0
    tremor_min_severity: str = "concern"     # concern = moderate or worse; notice = any

    @classmethod
    def from_config(cls, cfg) -> "PolicyParams":
        raw = dict(cfg.get("policy") or {})
        demo = raw.pop("demo", None) or {}
        if cfg.get("mode") == "demo":
            raw.update(demo)
        if "quiet_hours" in raw:
            raw["quiet_hours"] = tuple(raw["quiet_hours"])
        return cls(**raw)

    def cooldown_s(self, kind: str) -> float:
        return 60.0 * float(self.cooldown_min.get(kind, self.cooldown_min.get("default", 30)))


@dataclass
class Decision:
    speak: bool
    severity: str
    reason: str
    defer: bool = False                      # mention it in the next conversation


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_quiet_hours(now: float, quiet: tuple[str, str]) -> bool:
    lt = time.localtime(now)
    t, start, end = lt.tm_hour * 60 + lt.tm_min, _minutes(quiet[0]), _minutes(quiet[1])
    return start <= t < end if start <= end else (t >= start or t < end)


class Rules:
    def __init__(self, params: PolicyParams | None = None) -> None:
        self.p = params or PolicyParams()
        self._last: dict[str, tuple[float, int]] = {}      # kind -> (when, severity rank) of the last check-in
        self._checkins: deque[float] = deque()             # non-urgent check-ins, for the hourly limit
        self._decided: set[int] = set()                    # episodes that already had their decision

    def severity(self, ep: dict) -> str:
        kind = ep.get("kind")
        if kind == "fall_suspected":
            return "urgent"
        if kind in TREMORS:
            strong = RANK.get(ep.get("severity", "notice"), 1) >= RANK[self.p.tremor_min_severity]
            return "concern" if strong and (ep.get("duration_s") or 0) >= self.p.tremor_min_s else "notice"
        return ep.get("severity") or "info"

    def mark_decided(self, ep: dict, now: float) -> None:
        """A check-in for this episode was opened another way (a double-tap during it): count it
        as this episode's check-in, so the rules do not open a second one."""
        if ep.get("id") is not None:
            self._decided.add(ep["id"])
        self._last[ep.get("kind", "")] = (now, RANK.get(self.severity(ep), 1))

    def decide(self, ep: dict, now: float, *, connected: bool = True, worn: bool = True,
               busy: str | None = None) -> Decision:
        """busy: None, or the urgency ("normal" / "urgent") of the conversation already running."""
        sev = self.severity(ep)
        if RANK[sev] < RANK["concern"]:
            return Decision(False, sev, "logged only")
        ep_id = ep.get("id")
        if ep_id is not None:
            if ep_id in self._decided:
                return Decision(False, sev, "already decided for this episode")
            self._decided.add(ep_id)
        kind = ep.get("kind", "")

        if sev == "urgent":
            if not connected:
                return Decision(False, sev, "band out of reach", defer=True)
            if busy == "urgent":
                return Decision(False, sev, "an urgent conversation is already running", defer=True)
            self._last[kind] = (now, RANK[sev])
            return Decision(True, sev, "urgent" + (": stops the current conversation" if busy else ""))

        if not connected:
            return Decision(False, sev, "band out of reach", defer=True)
        if not worn:
            return Decision(False, sev, "band not worn")
        if busy:
            return Decision(False, sev, "a conversation is already running", defer=True)
        if in_quiet_hours(now, self.p.quiet_hours):
            return Decision(False, sev, "quiet hours", defer=True)
        last = self._last.get(kind)
        if last and now - last[0] < self.p.cooldown_s(kind) and RANK[sev] <= last[1]:
            return Decision(False, sev, f"cooldown ({self.p.cooldown_s(kind) / 60:.0f} min)")
        while self._checkins and now - self._checkins[0] >= 3600:
            self._checkins.popleft()
        if len(self._checkins) >= self.p.max_checkins_per_hour:
            return Decision(False, sev, f"already {len(self._checkins)} check-ins this hour", defer=True)
        self._checkins.append(now)
        self._last[kind] = (now, RANK[sev])
        return Decision(True, sev, sev)
