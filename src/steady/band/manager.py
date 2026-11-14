"""Owns the band: connects, configures, supervises the link, feeds the ring buffer.

Only this module talks to the band. Everything else reads `BandManager.status` or the bus.
SPEC 3.1 (settings) and 3.3 (supervision).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field

from .. import kitpath  # noqa: F401
from vexo_open import Band, VexoError  # noqa: E402

from ..bus import Bus
from ..config import Config
from ..screening.context import Activity
from ..store.db import Writer
from .ingest import RingBuffer

log = logging.getLogger("steady.band")
BACKOFF = (5, 10, 20, 30)


@dataclass
class BandStatus:
    connected: bool = False
    reconnecting: bool = False
    name: str | None = None
    firmware: str | None = None
    transport: str | None = None
    session_id: int | None = None
    battery: int | None = None
    charging: bool | None = None
    rssi: int | None = None
    last_error: str | None = None
    connected_since: float | None = None
    samples: int = 0
    lost_samples: int = 0
    events: dict = field(default_factory=dict)

    def public(self) -> dict:
        return asdict(self)


class BandManager:
    def __init__(self, cfg: Config, bus: Bus, writer: Writer, ring: RingBuffer) -> None:
        self.cfg = cfg
        self.bus = bus
        self.writer = writer
        self.ring = ring
        self.status = BandStatus(transport=cfg.transport)
        self.activity: Activity | None = None
        self.band: Band | None = None
        self.calibrated = False
        self._closed = asyncio.Event()
        self._stopping = False
        self._gap_id: int | None = None
        self.link_epoch = 0          # bumps on every (re)connect; windows spanning it are invalid
        self.link_epoch_time = 0.0
        self.sim = None                      # the pretend band, when transport is "sim" (to direct it)
        self.sim_options: dict = {}

    # ---- supervision -------------------------------------------------------------------
    async def run(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                await self._connect_and_serve()
                attempt = 0
            except VexoError as e:
                self.status.last_error = f"{e.code}: {e}"
                log.warning("band: %s", self.status.last_error)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep supervising whatever happens
                self.status.last_error = repr(e)
                log.exception("band: unexpected error")
            self._set_connected(False)
            if self._stopping:
                break
            await self._open_gap("disconnected")
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            attempt += 1
            hint = " Double-tap the band." if self.cfg.transport == "ble" else ""
            log.info("band: retrying in %ds.%s", delay, hint)
            await asyncio.sleep(delay)

    async def _connect_and_serve(self) -> None:
        n = self.cfg.band_number
        transport: object = self.cfg.transport
        if transport == "sim":
            from vexo_open.transports.sim import SimTransport
            transport = self.sim = SimTransport(n, **self.sim_options)
        log.info("band: connecting to %d over %s", n, self.cfg.transport)
        band = await Band.connect(n, transport=transport, auto_reconnect=True,
                                  reconnect_seconds=float(self.cfg.get("band.reconnect_seconds", 120)))
        self.band = band
        self._closed.clear()
        try:
            self._register(band)
            await self._configure(band)
            self.status.name = band.name
            self.status.firmware = band.info.firmware
            self.status.session_id = await self.writer.insert_returning_id("band_sessions", {
                "started_at": time.time(), "band_no": band.number,
                "firmware": band.info.firmware, "transport": self.cfg.transport})
            self._on_link_up()
            log.info("band: %s connected (fw %s)", band.name, band.info.firmware)
            await self._closed.wait()
        finally:
            self.band = None
            if self.status.session_id:
                self.writer.execute("UPDATE band_sessions SET ended_at=?, end_reason=? WHERE id=?",
                                    (time.time(), self.status.last_error or "closed", self.status.session_id))
            try:
                if band.connected or band.reconnecting:
                    await band.disconnect()
            except VexoError:
                pass

    async def _configure(self, band: Band) -> None:
        rate = 100 if self.cfg.get("sampling.mode") == "eco" else 208
        await band.set_motion(accel_range=int(self.cfg.get("sampling.accel_range_g", 8)),
                              gyro_range=int(self.cfg.get("sampling.gyro_range_dps", 1000)))
        streams = dict(motion=rate, sensors="both", events=True, activity=1, battery=60)
        if self.cfg.transport == "ble":
            streams["link"] = 5
        await band.configure(**streams)
        await self.apply_screening_led()

    async def apply_screening_led(self) -> None:
        if not self.band:
            return
        colour = self.cfg.get("led.screening_colour") if self.calibrated else self.cfg.get("led.uncalibrated_colour")
        try:
            await self.band.set_led(colour, brightness=float(self.cfg.get("led.brightness", 0.08)),
                                    pattern="breathe", period=int(self.cfg.get("led.period_ms", 4000)))
        except VexoError as e:  # e.g. denied while the mic is on
            log.info("band: LED not set (%s)", e.code)

    # ---- microphone (only during a conversation) ----------------------------------------
    async def mic_on(self, on_packet) -> None:
        """Start the audio stream: on_packet(opus_bytes, seq) for each 20 ms packet.
        The band's light turns solid blue and LED commands are refused until mic_off."""
        if not self.band:
            raise VexoError("not-connected", "The band is not connected, so its microphone cannot be used.")
        await self.mic_off()
        self._audio_handler = lambda a: on_packet(a.opus, a.seq)
        self.band.on("audio", self._audio_handler)
        await self.band.configure(audio=True)
        self.mic = True
        self._publish_status()

    async def mic_off(self) -> None:
        handler, self._audio_handler = getattr(self, "_audio_handler", None), None
        if handler is None:
            return
        self.mic = False
        if self.band:
            try:
                self.band.off("audio", handler)
                await self.band.configure(audio=False)      # the stored LED setting comes back by itself
            except VexoError as e:
                log.warning("band: could not switch the microphone off cleanly (%s)", e.code)
        self._publish_status()

    async def flash(self, colour: str, seconds: float = 0.6) -> None:
        """A short attention flash, then back to the screening light. Not while the mic is on."""
        if not self.band or getattr(self, "mic", False):
            return
        try:
            await self.band.set_led(colour, brightness=0.6, pattern="blink", period=200)
            await asyncio.sleep(seconds)
        except VexoError:
            pass
        await self.apply_screening_led()

    async def stop(self) -> None:
        self._stopping = True
        self._closed.set()

    # ---- events ------------------------------------------------------------------------
    def _register(self, band: Band) -> None:
        ring = self.ring

        def on_motion(m):
            ring.append(m.time, m.n, m.ax, m.ay, m.az, m.gx, m.gy, m.gz)

        def on_event(e):
            self.status.events[e.kind] = self.status.events.get(e.kind, 0) + 1
            self.writer.insert("band_events", {"session_id": self.status.session_id, "t": e.time,
                                               "kind": e.kind, "arg": getattr(e, "arg", None)})
            self.bus.publish("band.event", {"kind": e.kind, "time": e.time, "arg": getattr(e, "arg", None)})

        def on_activity(e):
            self.activity = Activity(time=e.time, state=e.state, cadence=float(e.cadence), steps=int(e.steps))

        def on_battery(e):
            self.status.battery, self.status.charging = e.percent, bool(e.charging)
            self._publish_status()

        def on_link(e):
            self.status.rssi = e.rssi

        def on_notice(e):
            log.warning("band notice: %s %s", e.kind, getattr(e, "arg", ""))
            self.writer.insert("band_events", {"session_id": self.status.session_id, "t": e.time,
                                               "kind": "notice:" + e.kind, "arg": getattr(e, "arg", None)})

        def on_reconnecting(e):
            log.warning("band: link lost (%s), reconnecting for %ss", e.reason, e.seconds)
            self._set_connected(False, reconnecting=True)
            asyncio.get_running_loop().create_task(self._open_gap(f"reconnecting:{e.reason}"))

        def on_reconnected(e):
            log.info("band: reconnected after %.1fs (refused: %s)", e.seconds, e.failed)
            self._on_link_up()

        def on_disconnect(e):
            self.status.last_error = getattr(e, "reason", "lost")
            log.warning("band: disconnected (%s)", self.status.last_error)
            self._closed.set()

        band.on("motion", on_motion)
        band.on("event", on_event)
        band.on("activity", on_activity)
        band.on("battery", on_battery)
        band.on("notice", on_notice)
        band.on("reconnecting", on_reconnecting)
        band.on("reconnected", on_reconnected)
        band.on("disconnect", on_disconnect)
        if self.cfg.transport == "ble":
            band.on("link", on_link)

    # ---- state helpers -----------------------------------------------------------------
    def _on_link_up(self) -> None:
        self.ring.reset_numbering()
        self.link_epoch += 1
        self.link_epoch_time = time.time()
        self.status.last_error = None
        self._set_connected(True)
        if self._gap_id is not None:
            self.writer.execute("UPDATE connectivity_gaps SET ended_at=? WHERE id=?", (time.time(), self._gap_id))
            self._gap_id = None

    async def _open_gap(self, reason: str) -> None:
        if self._gap_id is None:
            self._gap_id = -1  # reserve while the insert runs
            gap_id = await self.writer.insert_returning_id(
                "connectivity_gaps", {"started_at": time.time(), "reason": reason})
            if self.status.connected:   # the link came back while we were inserting
                self.writer.execute("UPDATE connectivity_gaps SET ended_at=? WHERE id=?", (time.time(), gap_id))
                self._gap_id = None
            else:
                self._gap_id = gap_id

    def _set_connected(self, connected: bool, reconnecting: bool = False) -> None:
        self.status.connected = connected
        self.status.reconnecting = reconnecting
        self.status.connected_since = time.time() if connected else None
        self._publish_status()

    def _publish_status(self) -> None:
        self.status.samples = self.ring.count
        self.status.lost_samples = self.ring.lost
        self.bus.publish("band.status", self.status.public())

    def describe(self) -> str:
        return json.dumps(self.status.public())
