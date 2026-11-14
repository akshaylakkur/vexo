"""steadyd: wires the band, screening loop, database and local API into one asyncio process.

Startup order: database writer -> local API -> band supervisor -> screener.
Shutdown (Ctrl+C): screener -> rollup flush -> band disconnect -> database flush -> API.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys

import uvicorn

from .api.server import create_app
from .band.ingest import RingBuffer
from .band.manager import BandManager
from .bus import Bus
from .config import Config
from .screening.screener import Screener
from .store.db import Writer
from .voice.service import VoiceService
from .actions.brief import BriefScheduler
from .actions.tasks import TaskManager
from .voice.announce import Announcer

log = logging.getLogger("steady")


class Steady:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.bus = Bus()
        self.writer = Writer(cfg.db_path)
        self.ring = RingBuffer(seconds=60, rate_hz=208)
        self.band = BandManager(cfg, self.bus, self.writer, self.ring)
        self.screener = Screener(cfg, self.bus, self.writer, self.ring, self.band)
        self.voice = VoiceService(self)
        # M6: the two outgoing paths (a spoken request, the end-of-day brief) and spoken outcomes
        self.tasks = TaskManager(cfg, self.bus, self.writer)
        self.brief = BriefScheduler(self, self.tasks)
        self.announcer = Announcer(self)

    async def run(self, with_api: bool = True) -> None:
        await self.writer.start()
        tasks: list[asyncio.Task] = []
        server = None
        caffeinate = self._keep_awake()
        if with_api:
            host, port = self.cfg.get("api.host", "127.0.0.1"), int(self.cfg.get("api.port", 8765))
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise SystemExit("api.host must be a loopback address")
            server = uvicorn.Server(uvicorn.Config(create_app(self), host=host, port=port,
                                                   log_level="warning", lifespan="off",
                                                   timeout_graceful_shutdown=2))
            server.install_signal_handlers = lambda: None  # we handle Ctrl+C ourselves
            tasks.append(asyncio.create_task(server.serve(), name="api"))
            log.info("local API for steadyctl on %s:%d", host, port)
        tasks.append(asyncio.create_task(self.band.run(), name="band"))
        tasks.append(asyncio.create_task(self.screener.run(), name="screener"))
        tasks.append(asyncio.create_task(self.voice.watch_double_tap(), name="voice"))
        await self.tasks.start()
        for name, coro in (("tasks", self.tasks.watch()), ("brief", self.brief.run()),
                           ("announcer", self.announcer.run())):
            tasks.append(asyncio.create_task(coro, name=name))

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        done_any = asyncio.create_task(asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION))
        stop_task = asyncio.create_task(stop.wait())
        await asyncio.wait([done_any, stop_task], return_when=asyncio.FIRST_COMPLETED)
        stop_task.cancel()
        for t in tasks:
            if t.done() and not t.cancelled() and t.exception():
                log.error("task %s failed: %r", t.get_name(), t.exception())

        log.info("stopping...")
        band_task = next(t for t in tasks if t.get_name() == "band")
        for t in tasks:
            if t.get_name() == "screener":
                t.cancel()
        self.screener.close()
        await self.band.stop()                     # lets the supervisor disconnect cleanly
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(band_task), timeout=5)
        if server:
            server.should_exit = True
            api_task = next(t for t in tasks if t.get_name() == "api")
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(api_task), timeout=5)
        for t in tasks + [done_any]:
            if not t.done():
                t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.voice.close()
        await self.writer.close()
        if caffeinate:
            caffeinate.terminate()
        log.info("stopped; %d statements written", self.writer.written)

    def _keep_awake(self):
        """macOS: stop idle sleep while Steady runs (the lid must stay open)."""
        if sys.platform != "darwin" or self.cfg.transport == "sim":
            return None
        try:
            return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            return None
