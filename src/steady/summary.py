"""Symptom totals for a period, with numbers. Shared by the voice agent, the local API
(steadyctl summary) and OpenClaw tasks, so every message quotes the same figures."""
from __future__ import annotations

import asyncio
import time

from .store.db import connect


def period_start(period: str, now: float | None = None) -> float:
    now = now or time.time()
    if period == "last_hour":
        return now - 3600
    if period == "week":
        return now - 7 * 86400
    return time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))      # today, from midnight


def _query(db_path, sql: str, params: tuple) -> list[dict]:
    conn = connect(db_path, readonly=True)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


async def summarize(steady, period: str = "today") -> dict:
    """Flushes pending writes first, so the numbers include the last few seconds."""
    if period not in ("last_hour", "today", "week"):
        period = "today"
    since = period_start(period)
    with_rollup = getattr(getattr(steady, "screener", None), "rollup", None)
    if with_rollup is not None:
        with_rollup.flush()
    await steady.writer.flush()
    db = steady.cfg.db_path
    rolls = await asyncio.to_thread(_query, db, "SELECT * FROM hourly_rollups WHERE hour_start >= ?", (since - 3600,))
    eps = await asyncio.to_thread(_query, db, "SELECT kind, severity, duration_s FROM episodes WHERE started_at >= ?", (since,))
    tot = lambda k: round(sum(r[k] or 0 for r in rolls))  # noqa: E731
    by_kind: dict[str, dict] = {}
    for e in eps:
        k = by_kind.setdefault(e["kind"], {"count": 0, "total_seconds": 0})
        k["count"] += 1
        k["total_seconds"] += round(e["duration_s"] or 0)
    rest = tot("rest_s")
    return {
        "period": period,
        "minutes_monitored": round(tot("worn_s") / 60, 1),
        "minutes_arm_resting": round(rest / 60, 1),
        "rest_tremor_seconds": tot("rest_tremor_s"),
        "rest_tremor_percent_of_resting_time": round(100 * tot("rest_tremor_s") / rest, 1) if rest else None,
        "steps": tot("steps"),
        "falls_suspected": tot("falls"),
        "episodes": by_kind,
    }


def as_text(s: dict) -> str:
    label = {"last_hour": "In the last hour", "today": "Today", "week": "In the last 7 days"}[s["period"]]
    parts = [f"{label}: monitored for {s['minutes_monitored']} minutes ({s['minutes_arm_resting']} with the arm resting)."]
    pct = s["rest_tremor_percent_of_resting_time"]
    parts.append(f"Resting tremor: {s['rest_tremor_seconds']} seconds" + (f" ({pct}% of resting time)." if pct is not None else "."))
    for kind, v in s["episodes"].items():
        parts.append(f"{kind.replace('_', ' ')}: {v['count']} episode(s), {v['total_seconds']} s in total.")
    parts.append(f"Steps: {s['steps']}. Possible falls: {s['falls_suspected']}.")
    return " ".join(parts)
