"""Local API and live dashboard (SPEC 9). Bound to 127.0.0.1 only.

M1 serves read-only endpoints and the live WebSocket. Write endpoints (speak, tasks) arrive
with M6 and require the STEADY_API_TOKEN bearer token. The WebSocket and the dashboard only
accept same-origin connections, so another web page in the browser cannot read live data.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import TYPE_CHECKING

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..kitpath import ROOT
from ..store.db import connect

if TYPE_CHECKING:
    from ..app import Steady

WEB = ROOT / "web"


def create_app(steady: "Steady") -> FastAPI:
    app = FastAPI(title="Steady", docs_url=None, redoc_url=None)
    cfg = steady.cfg
    port = int(cfg.get("api.port", 8765))
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def query(sql: str, params: tuple = ()) -> list[dict]:
        conn = connect(cfg.db_path, readonly=True)
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    @app.get("/api/status")
    async def status() -> JSONResponse:
        s = steady.screener
        return JSONResponse({
            "time": time.time(),
            "band": steady.band.status.public(),
            "screening": {"ticks": s.ticks, "logged": s.logged, "positives": s.positives, "compute_ms": round(s.compute_ms, 1),
                          "last": s.last, "calibrated": steady.band.calibrated},
            "db": {"path": str(cfg.db_path.relative_to(ROOT)), "written": steady.writer.written},
            "mode": cfg.get("mode"),
        })

    @app.get("/api/windows")
    async def windows(since: float = Query(0.0), limit: int = Query(300, le=5000)) -> JSONResponse:
        rows = await asyncio.to_thread(query, "SELECT * FROM windows WHERE t_end > ? ORDER BY t_end DESC LIMIT ?",
                                       (since, limit))
        return JSONResponse(rows[::-1])

    @app.get("/api/episodes")
    async def episodes(since: float = Query(0.0), limit: int = Query(100, le=2000)) -> JSONResponse:
        rows = await asyncio.to_thread(
            query, "SELECT * FROM episodes WHERE started_at > ? ORDER BY started_at DESC LIMIT ?", (since, limit))
        for r in rows:
            r["metrics"] = json.loads(r.pop("metrics_json") or "{}")
        return JSONResponse(rows)

    @app.get("/api/rollups")
    async def rollups(hours: int = Query(24, le=24 * 31)) -> JSONResponse:
        since = time.time() - hours * 3600
        rows = await asyncio.to_thread(query, "SELECT * FROM hourly_rollups WHERE hour_start >= ? ORDER BY hour_start",
                                       (since - 3600,))
        return JSONResponse(rows)

    @app.get("/api/events")
    async def events(limit: int = Query(50, le=1000)) -> JSONResponse:
        rows = await asyncio.to_thread(query, "SELECT t, kind, arg FROM band_events ORDER BY t DESC LIMIT ?", (limit,))
        return JSONResponse(rows)

    @app.websocket("/ws/live")
    async def live(ws: WebSocket) -> None:
        if ws.headers.get("origin") not in allowed_origins:
            await ws.close(code=1008)
            return
        await ws.accept()
        q = steady.bus.subscribe("window", "band.status", "band.event", "shutdown",
                                 "episode.started", "episode.escalated", "episode.ended",
                                 "conversation.state", "conversation.turn")
        try:
            await ws.send_text(json.dumps({"topic": "band.status", "data": steady.band.status.public()}))
            while True:
                topic, payload = await q.get()
                if topic == "shutdown":
                    await ws.close(code=1001)
                    break
                await ws.send_text(json.dumps({"topic": topic, "data": payload}))
        except (WebSocketDisconnect, RuntimeError):
            pass
        except asyncio.CancelledError:      # server shutting down: close politely, no traceback
            with contextlib.suppress(Exception):
                await ws.close(code=1001)
        finally:
            steady.bus.unsubscribe(q)

    if (WEB / "index.html").exists():
        app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return app


def dashboard_built() -> bool:
    return (WEB / "dist" / "main.js").exists()
