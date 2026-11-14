"""Episode tracker (SPEC 4.7): turns window-level positives into episodes with hysteresis.

A single noisy window never starts an episode and a single quiet one never ends it:
  start  3 of the last 5 windows positive (about 10 s)
  end    3 negative windows in a row
Each episode is one row in `episodes`: opened at start, updated every window, closed at end.
Falls are single events and are written as closed episodes at once.
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import deque
from dataclasses import dataclass, field

from ..bus import Bus
from ..store.db import Writer
from . import flags as F
from .detectors.fall import FallEvent
from .detectors.tremor import TremorParams

log = logging.getLogger("steady.episodes")

START_OF, START_NEED, END_NEED = 5, 3, 3
TRACKED = {F.REST_TREMOR: "rest_tremor", F.ACTION_TREMOR: "action_tremor"}


@dataclass
class _Open:
    id: int
    kind: str
    started_at: float
    last_positive_end: float
    amps: list[float] = field(default_factory=list)
    freqs: list[float] = field(default_factory=list)
    negatives: int = 0
    severity: str = "notice"


class EpisodeTracker:
    def __init__(self, writer: Writer, bus: Bus, tremor: TremorParams | None = None) -> None:
        self.writer = writer
        self.bus = bus
        self.tremor = tremor or TremorParams()
        self._recent: dict[int, deque] = {bit: deque(maxlen=START_OF) for bit in TRACKED}
        self.open: dict[int, _Open] = {}
        self.count = 0

    # ---- helpers -----------------------------------------------------------------------
    def _metrics(self, ep: _Open) -> dict:
        peak = max(ep.amps) if ep.amps else 0.0
        return {
            "windows": len(ep.amps),
            "freq_mean_hz": round(statistics.fmean(ep.freqs), 2) if ep.freqs else None,
            "amp_mean_dps": round(statistics.fmean(ep.amps), 2) if ep.amps else None,
            "amp_peak_dps": round(peak, 2),
            "grade": self.tremor.grade(peak),
        }

    def _severity(self, ep: _Open) -> str:
        return "notice" if self.tremor.grade(max(ep.amps)) == "mild" else "concern"

    def _publish(self, what: str, ep: _Open, ended_at: float | None = None) -> dict:
        payload = {"id": ep.id, "kind": ep.kind, "started_at": ep.started_at, "ended_at": ended_at,
                   "duration_s": round((ended_at or ep.last_positive_end) - ep.started_at, 1),
                   "severity": ep.severity, "metrics": self._metrics(ep)}
        self.bus.publish(f"episode.{what}", payload)
        return payload

    # ---- per window --------------------------------------------------------------------
    async def update(self, row: dict) -> list[tuple[str, dict]]:
        """Feed one assessment. Returns [(what, episode)] for started / escalated / ended."""
        out: list[tuple[str, dict]] = []
        flags = row["flags"] if row["valid"] else 0
        for bit, kind in TRACKED.items():
            positive = bool(flags & bit)
            recent = self._recent[bit]
            recent.append((positive, row["t_start"], row["t_end"], row.get("tremor_amp"), row.get("dom_freq")))
            ep = self.open.get(bit)
            if ep is None:
                hits = [r for r in recent if r[0]]
                if positive and len(hits) >= START_NEED:
                    ep = _Open(id=0, kind=kind, started_at=hits[0][1], last_positive_end=row["t_end"],
                               amps=[h[3] for h in hits if h[3] is not None],
                               freqs=[h[4] for h in hits if h[4] is not None])
                    ep.severity = self._severity(ep)
                    ep.id = await self.writer.insert_returning_id("episodes", {
                        "kind": kind, "status": "open", "started_at": ep.started_at,
                        "severity": ep.severity, "metrics_json": json.dumps(self._metrics(ep))})
                    self.open[bit] = ep
                    self.count += 1
                    log.info("episode %d started: %s (%s)", ep.id, kind, self._metrics(ep))
                    out.append(("started", self._publish("started", ep)))
                continue
            if positive:
                ep.negatives = 0
                ep.last_positive_end = row["t_end"]
                if row.get("tremor_amp") is not None:
                    ep.amps.append(row["tremor_amp"])
                if row.get("dom_freq") is not None:
                    ep.freqs.append(row["dom_freq"])
                new_sev = self._severity(ep)
                escalated = new_sev != ep.severity and new_sev == "concern"
                if escalated:
                    ep.severity = new_sev
                self.writer.execute(
                    "UPDATE episodes SET duration_s=?, severity=?, metrics_json=? WHERE id=?",
                    (ep.last_positive_end - ep.started_at, ep.severity, json.dumps(self._metrics(ep)), ep.id))
                if escalated:
                    out.append(("escalated", self._publish("escalated", ep)))
                else:
                    self._publish("updated", ep)       # the policy re-checks the growing duration
            else:
                ep.negatives += 1
                if ep.negatives >= END_NEED:
                    out.append(("ended", self._close(bit)))
        return out

    def _close(self, bit: int) -> dict:
        ep = self.open.pop(bit)
        self._recent[bit].clear()
        self.writer.execute(
            "UPDATE episodes SET status='closed', ended_at=?, duration_s=?, severity=?, metrics_json=? WHERE id=?",
            (ep.last_positive_end, ep.last_positive_end - ep.started_at, ep.severity,
             json.dumps(self._metrics(ep)), ep.id))
        log.info("episode %d ended: %s after %.0f s", ep.id, ep.kind, ep.last_positive_end - ep.started_at)
        return self._publish("ended", ep, ended_at=ep.last_positive_end)

    # ---- single events -----------------------------------------------------------------
    async def add_fall(self, ev: FallEvent) -> dict:
        severity = "urgent" if ev.kind == "fall_suspected" else "info"
        ep_id = await self.writer.insert_returning_id("episodes", {
            "kind": ev.kind, "status": "closed", "started_at": ev.t_freefall, "ended_at": ev.t_impact,
            "duration_s": ev.t_impact - ev.t_freefall, "severity": severity,
            "metrics_json": json.dumps(ev.metrics)})
        self.count += 1
        payload = {"id": ep_id, "kind": ev.kind, "started_at": ev.t_freefall, "ended_at": ev.t_impact,
                   "duration_s": round(ev.t_impact - ev.t_freefall, 2), "severity": severity, "metrics": ev.metrics}
        log.info("episode %d: %s %s", ep_id, ev.kind, ev.metrics)
        self.bus.publish("episode.started", payload)
        return payload

    def close_all(self) -> None:
        for bit in list(self.open):
            self._close(bit)
