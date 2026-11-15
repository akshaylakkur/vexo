"""The call server: the only part of Steady that the internet can reach.

    POST /twiml/{token}    Twilio, when the call is answered: stream its audio to /ws
    POST /status/{token}   Twilio: initiated, ringing, answered, completed / no-answer / busy / failed
    WS   /ws               the call's audio; the stream's first message carries the token
    GET  /health           for the tunnel check

It listens on 127.0.0.1 only, on its own port, never the dashboard's. A `cloudflared` quick
tunnel gives it a public https address at startup (or set call.public_url). Each call has a
random token that lives only as long as the call; anything with an unknown token is refused.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qsl

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket

from .twilio import FINAL_STATUSES, TwilioCreds, twiml_stream

log = logging.getLogger("steady.call")
TUNNEL_URL = re.compile(r"https://[-a-z0-9]+\.trycloudflare\.com")


@dataclass
class CallSession:
    """What one call knows and reports back. The caller waits on `acknowledged` / `finished`."""
    token: str
    to_name: str
    to_number: str
    wearer: str
    prompt: str                                   # the system prompt, with the facts of the alert
    latest: Callable[[], dict]                    # the band's live status, for get_latest_status
    keyterms: list[str] = field(default_factory=list)
    acknowledged: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    call_sid: str | None = None
    status: str = "created"                       # Twilio's last CallStatus
    how: str = ""                                 # how they confirmed, or why not
    heard: list[str] = field(default_factory=list)
    said: list[str] = field(default_factory=list)

    def acknowledge(self, how: str) -> None:
        if not self.acknowledged.is_set():
            self.how = how
            self.acknowledged.set()
            log.info("call to %s: confirmed (%s)", self.to_name, how)


class Tunnel:
    """A cloudflared quick tunnel to a local port. Its address changes every run."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.proc: asyncio.subprocess.Process | None = None
        self._drain: asyncio.Task | None = None

    async def start(self, timeout: float = 30.0) -> str:
        self.proc = await asyncio.create_subprocess_exec(
            "cloudflared", "tunnel", "--url", f"http://127.0.0.1:{self.port}", "--no-autoupdate",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)

        async def find() -> str:
            url = None
            while line := await self.proc.stderr.readline():
                text = line.decode(errors="replace")
                if url is None and (m := TUNNEL_URL.search(text)):
                    url = m.group(0)
                if url and "Registered tunnel connection" in text:   # the address is printed before it works
                    return url
            raise RuntimeError("cloudflared exited before the tunnel was registered")
        url = await asyncio.wait_for(find(), timeout)
        self._drain = asyncio.create_task(self._discard())          # a full pipe would stall cloudflared
        return url

    async def _discard(self) -> None:
        while await self.proc.stderr.readline():
            pass

    async def stop(self) -> None:
        if self._drain:
            self._drain.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), 5)


class CallServer:
    def __init__(self, cfg, creds: TwilioCreds) -> None:
        self.cfg = cfg
        self.creds = creds
        self.port = int(cfg.get("call.port", 8766))
        self.public_url: str | None = (cfg.get("call.public_url") or "").rstrip("/") or None
        self.sessions: dict[str, CallSession] = {}
        self.tunnel: Tunnel | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    # ---- sessions ----------------------------------------------------------------------
    def new_token(self) -> str:
        return secrets.token_urlsafe(24)

    def add(self, session: CallSession) -> None:
        self.sessions[session.token] = session

    def remove(self, session: CallSession) -> None:
        self.sessions.pop(session.token, None)

    def urls(self, token: str) -> tuple[str, str]:
        return f"{self.public_url}/twiml/{token}", f"{self.public_url}/status/{token}"

    # ---- the web app -------------------------------------------------------------------
    def app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/health")
        async def health() -> Response:
            return Response("ok", media_type="text/plain")

        @app.post("/twiml/{token}")
        async def twiml(token: str) -> Response:
            if token not in self.sessions:
                return Response(status_code=403)
            ws_url = re.sub(r"^http", "ws", self.public_url or "") + "/ws"
            return Response(twiml_stream(ws_url, token), media_type="application/xml")

        @app.post("/status/{token}")
        async def status(token: str, request: Request) -> Response:
            session = self.sessions.get(token)
            if session is None:
                return Response(status_code=403)
            form = dict(parse_qsl((await request.body()).decode()))      # Twilio posts url-encoded forms
            session.status = form.get("CallStatus", session.status)
            log.info("call to %s: %s", session.to_name, session.status)
            if session.status in FINAL_STATUSES:
                session.finished.set()
            return Response(status_code=204)

        @app.websocket("/ws")
        async def media(websocket: WebSocket) -> None:
            from pipecat.runner.utils import parse_telephony_websocket
            await websocket.accept()
            try:
                _, call_data = await asyncio.wait_for(parse_telephony_websocket(websocket), 10)
            except Exception as e:  # noqa: BLE001
                log.warning("call socket: not a Twilio media stream (%s)", type(e).__name__)
                await websocket.close(code=1008)
                return
            session = self.sessions.get(str((call_data.get("body") or {}).get("token", "")))
            if session is None:
                log.warning("call socket: unknown token; closed")
                await websocket.close(code=1008)
                return
            log.info("call to %s: connected, speaking", session.to_name)
            from . import bot                     # loads Pipecat only when a call connects
            try:
                await bot.run(websocket, call_data, session, self.cfg, self.creds)
            except (Exception, SystemExit):  # noqa: BLE001  a broken call must not take the server down
                log.exception("call to %s failed", session.to_name)
            finally:
                session.finished.set()

        return app

    # ---- lifecycle ---------------------------------------------------------------------
    async def start(self) -> None:
        self._server = uvicorn.Server(uvicorn.Config(self.app(), host="127.0.0.1", port=self.port, log_level="warning",
                                                     lifespan="off", timeout_graceful_shutdown=2))
        self._server.install_signal_handlers = lambda: None
        self._task = asyncio.create_task(self._server.serve(), name="call-server")
        while not self._server.started:
            if self._task.done():
                raise RuntimeError(f"the call server could not start on port {self.port}")
            await asyncio.sleep(0.05)
        if self.public_url is None:
            self.tunnel = Tunnel(self.port)
            self.public_url = await self.tunnel.start()
        await self._reachable()
        log.info("call server: %s -> 127.0.0.1:%d", self.public_url, self.port)

    async def _reachable(self, timeout: float = 45.0) -> None:
        """Check the public address answers. A new quick tunnel's name takes a few seconds to
        appear in DNS, and asking too early can leave a "no such name" in this Mac's DNS cache,
        so wait first. Twilio resolves the name itself, so a failure here only warns."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if self.tunnel:
            await asyncio.sleep(5.0)
        async with httpx.AsyncClient(timeout=5) as http:
            while loop.time() < deadline:
                with contextlib.suppress(httpx.HTTPError):
                    if (await http.get(self.public_url + "/health")).status_code == 200:
                        return
                await asyncio.sleep(2.0)
        log.warning("call server: %s did not answer from this Mac within %.0f s (a stale DNS cache?); "
                    "calls stay on", self.public_url, timeout)

    async def stop(self) -> None:
        if self.tunnel:
            await self.tunnel.stop()
        if self._server:
            self._server.should_exit = True
        if self._task:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, 5)
