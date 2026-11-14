"""The end-of-day brief (the second of Steady's two outgoing paths).

Once a day, at `brief.time`, Steady gathers the day's tracked data and creates a
`daily_brief` task for the contact named in `brief.to`. OpenClaw words it; Steady sends it
as a text message and/or an email (`brief.via`). If steadyd was not running at that time,
the brief goes out when it next runs that day. A day with no monitoring sends nothing.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time

from ..store.db import connect
from ..summary import as_text, period_start, summarize
from .tasks import TaskError, TaskManager

log = logging.getLogger("steady.actions")
META_KEY = "last_brief_date"


def due(now: dt.datetime, at: str, last_sent: str | None) -> bool:
    """True from `at` (HH:MM) until midnight, unless today's brief already went out."""
    hh, mm = (int(x) for x in at.split(":"))
    return now.time() >= dt.time(hh, mm) and last_sent != now.date().isoformat()


class BriefScheduler:
    def __init__(self, steady, tasks: TaskManager) -> None:
        self.steady = steady
        self.tasks = tasks
        cfg = steady.cfg
        self.enabled = bool(cfg.get("brief.enabled", True))
        self.at = str(cfg.get("brief.time", "20:00"))
        self.to = str(cfg.get("brief.to", "caregiver"))
        self.via = list(cfg.get("brief.via") or ["imessage"])
        self.skip_if_no_data = bool(cfg.get("brief.skip_if_no_data", True))

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = connect(self.steady.cfg.db_path, readonly=True)
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    async def last_sent(self) -> str | None:
        await self.steady.writer.flush()
        rows = await asyncio.to_thread(self._query, "SELECT value FROM meta WHERE key=?", (META_KEY,))
        return rows[0]["value"] if rows else None

    def _mark(self, date: str) -> None:
        self.steady.writer.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (META_KEY, date))

    async def context(self) -> dict:
        """The whole day's tracked data, as facts the brief may use."""
        s = await summarize(self.steady, "today")
        since = period_start("today")
        eps = await asyncio.to_thread(
            self._query, "SELECT kind, severity, started_at, duration_s FROM episodes WHERE started_at >= ? "
                         "ORDER BY started_at", (since,))
        logs = await asyncio.to_thread(
            self._query, "SELECT t, kind, data_json FROM logs WHERE t >= ? ORDER BY t", (since,))
        clock = lambda t: time.strftime("%-I:%M %p", time.localtime(t))  # noqa: E731
        return {
            "date": dt.date.today().strftime("%A %-d %B %Y"),
            "summary": s, "summary_text": as_text(s),
            "episodes": [{"what": e["kind"].replace("_", " "), "at": clock(e["started_at"]),
                          "lasted_seconds": round(e["duration_s"] or 0), "severity": e["severity"]} for e in eps[:30]],
            "logged_today": [{"at": clock(r["t"]), "kind": r["kind"], **json.loads(r["data_json"] or "{}")} for r in logs[:30]],
        }

    async def send_now(self, force: bool = False) -> dict:
        """Create today's brief task. Returns what happened (also used by `steady brief`)."""
        ctx = await self.context()
        today = dt.date.today().isoformat()
        if self.skip_if_no_data and not force and not ctx["summary"]["minutes_monitored"] and not ctx["logged_today"]:
            self._mark(today)
            log.info("brief: nothing was tracked today, so no brief is sent")
            return {"sent": False, "reason": "nothing was tracked today"}
        try:
            task = await self.tasks.create("daily_brief", "Send today's brief.", to=self.to, origin="schedule",
                                           context=ctx, channels=self.via)
        except TaskError as e:
            log.warning("brief: not sent: %s", e)
            return {"sent": False, "reason": str(e)}
        self._mark(today)
        return {"sent": True, "task_id": task["id"], "to": task["recipient"]["name"], "via": task["channels"]}

    async def run(self) -> None:
        if not self.enabled:
            log.info("brief: disabled in config")
            return
        log.info("brief: daily at %s to %s by %s", self.at, self.to, "/".join(self.via))
        while True:
            try:
                if due(dt.datetime.now(), self.at, await self.last_sent()):
                    await self.send_now()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  never let the scheduler die
                log.exception("brief: check failed")
            await asyncio.sleep(30)
