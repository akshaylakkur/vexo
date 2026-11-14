"""The caregiver call without a phone: the real call server, tunnel, voice and model, with
this program playing Twilio and the caregiver.

    python -m steady.validate.call_local [--no-tunnel]

1. Starts the call server and its cloudflared tunnel, and checks the public address answers.
2. Opens a pretend Twilio media stream to it. Steady must speak first (real Deepgram voice,
   real Ollama model). What it said is transcribed and printed.
3. The "caregiver" answers out loud, "Yes, I'll go and check right now" (a synthesized voice,
   sent as phone audio). Steady must record the confirmation, say goodbye and hang up.
4. A second call where the caregiver presses 1 instead.
No phone call is placed and Twilio is not contacted.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import sys
import time

import httpx
import websockets

from .. import config as config_mod
from ..call.caller import PROMPT
from ..call.server import CallServer, CallSession
from ..call.twilio import TwilioCreds
from ..config import secret

FRAME = 160                    # 20 ms of 8 kHz mu-law
WHAT = ("At 14:05 the wristband detected a possible fall: a drop, a hard impact, then stillness. "
        "Steady asked through the headphones twice and got no answer.")


async def synth(text: str) -> bytes:
    """A caregiver's voice as phone audio (mu-law, 8 kHz), from Deepgram."""
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post("https://api.deepgram.com/v1/speak",
                            params={"model": "aura-2-orion-en", "encoding": "mulaw", "sample_rate": 8000, "container": "none"},
                            headers={"Authorization": "Token " + secret("DEEPGRAM_API_KEY")}, json={"text": text})
        r.raise_for_status()
        return r.content


async def transcribe(mulaw: bytes) -> str:
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post("https://api.deepgram.com/v1/listen",
                            params={"model": "nova-3", "encoding": "mulaw", "sample_rate": 8000, "smart_format": "true",
                                    "mip_opt_out": "true"},
                            headers={"Authorization": "Token " + secret("DEEPGRAM_API_KEY"), "Content-Type": "audio/basic"},
                            content=mulaw)
        r.raise_for_status()
        return r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]


async def one_call(server: CallServer, how: str, voice: bytes) -> dict:
    session = CallSession(token=server.new_token(), to_name="Priya", to_number="+10000000000", wearer="Akshay",
                          prompt=PROMPT.format(name="Priya", wearer="Akshay", what=WHAT,
                                               then="that they should ask someone else to check on Akshay"),
                          latest=lambda: {"minutes_since_event": 1.5, "band_connected": True,
                                          "wrist_now": "the arm is still", "battery_percent": 80},
                          keyterms=["Akshay", "Priya"])
    server.add(session)
    spoken = bytearray()
    last_audio = [time.time()]
    first_audio = [None]
    t0 = time.time()
    ws = await websockets.connect(f"ws://127.0.0.1:{server.port}/ws", max_size=None)
    send = lambda m: ws.send(json.dumps(m))  # noqa: E731
    await send({"event": "connected", "protocol": "Call", "version": "1.0.0"})
    await send({"event": "start", "streamSid": "MZlocal", "start": {
        "streamSid": "MZlocal", "callSid": "CAlocal", "accountSid": server.creds.account_sid,
        "customParameters": {"token": session.token}, "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}}})

    async def listen() -> None:
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("event") == "media":
                    spoken.extend(base64.b64decode(msg["media"]["payload"]))
                    last_audio[0] = time.time()
                    if first_audio[0] is None:
                        first_audio[0] = time.time() - t0
        except websockets.ConnectionClosed:
            pass

    async def silence_until(done) -> None:              # a phone line is never empty: send silence
        quiet = base64.b64encode(b"\xff" * FRAME).decode()
        while not done() and not closed[0]:
            try:
                await send({"event": "media", "streamSid": "MZlocal", "media": {"payload": quiet, "track": "inbound"}})
            except websockets.ConnectionClosed:      # Steady hung up
                closed[0] = True
                return
            await asyncio.sleep(0.02)

    closed = [False]
    listener = asyncio.create_task(listen())
    try:
        # Steady speaks first; wait until it has said its piece (audio, then 1.2 s of quiet)
        await silence_until(lambda: (first_audio[0] is not None and time.time() - last_audio[0] > 1.2) or time.time() - t0 > 30)
        opening_bytes = len(spoken)
        t_answer = time.time()
        if how == "press 1":
            await send({"event": "dtmf", "streamSid": "MZlocal", "dtmf": {"track": "inbound_track", "digit": "1"}})
        else:
            for i in range(0, len(voice), FRAME):
                if closed[0]:
                    break
                chunk = voice[i:i + FRAME].ljust(FRAME, b"\xff")
                await send({"event": "media", "streamSid": "MZlocal",
                            "media": {"payload": base64.b64encode(chunk).decode(), "track": "inbound"}})
                await asyncio.sleep(0.02)
        await silence_until(lambda: session.acknowledged.is_set() or time.time() - t_answer > 25)
        t_ack = time.time() - t_answer if session.acknowledged.is_set() else None
        # let it thank them, say goodbye and hang up
        await silence_until(lambda: session.finished.is_set() or time.time() - t_answer > 45)
        for _ in range(20):                               # the server marks the call finished just after closing
            if session.finished.is_set():
                break
            await asyncio.sleep(0.1)
        hung_up = closed[0] or session.finished.is_set()
    finally:
        with_closed = False
        try:
            await ws.close()
            with_closed = True
        except websockets.ConnectionClosed:
            with_closed = True
        finally:
            listener.cancel()
            server.remove(session)
    opening = await transcribe(bytes(spoken[:opening_bytes])) if opening_bytes else ""
    closing = await transcribe(bytes(spoken[opening_bytes:])) if len(spoken) > opening_bytes + 800 else ""
    return {"how": how, "first_audio_s": first_audio[0], "opening": opening, "acknowledged": session.acknowledged.is_set(),
            "recorded_as": session.how, "ack_after_s": t_ack, "heard": list(session.heard), "closing": closing,
            "hung_up_by_steady": hung_up, "socket_closed": with_closed}


async def main(use_tunnel: bool) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from pipecat.serializers.twilio import TwilioFrameSerializer

    hangups: list[bool] = []

    async def no_real_hangup(self) -> None:          # there is no real call to hang up at Twilio
        hangups.append(True)
    TwilioFrameSerializer._hang_up_call = no_real_hangup

    over = {"call.port": 8767}
    if not use_tunnel:
        over["call.public_url"] = "http://127.0.0.1:8767"
    cfg = config_mod.load(overrides=over)
    creds = TwilioCreds.load() or TwilioCreds("ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "unused", "+10000000000")
    server = CallServer(cfg, creds)
    checks: list[tuple[str, bool]] = []
    try:
        await server.start()
        if use_tunnel:
            async with httpx.AsyncClient(timeout=10) as http:
                ok = False
                for _ in range(10):
                    try:
                        ok = (await http.get(server.public_url + "/health")).text == "ok"
                    except httpx.HTTPError:
                        ok = False
                    if ok:
                        break
                    await asyncio.sleep(2)
            print(f"tunnel: {server.public_url} -> reachable: {ok}")
            checks.append(("public tunnel reaches the call server", ok))
        voice = await synth("Yes, I'll go and check on him right now.")
        for how in ("says yes", "press 1"):
            r = await one_call(server, how, voice)
            print(f"\n=== caregiver {how}")
            print(f"Steady spoke first after {r['first_audio_s']:.1f} s:\n  > {r['opening']}")
            if r["heard"]:
                print(f"Steady heard the caregiver say: {r['heard']}")
            print(f"confirmed: {r['acknowledged']} ({r['recorded_as']})"
                  + (f" after {r['ack_after_s']:.1f} s" if r["ack_after_s"] is not None else ""))
            print(f"then Steady said:\n  > {r['closing']}")
            print(f"Steady hung up by itself: {r['hung_up_by_steady']}")
            said = r["opening"].lower()
            checks += [(f"[{how}] Steady speaks first, within 5 s", r["first_audio_s"] is not None and r["first_audio_s"] < 5),
                       (f"[{how}] it says who it is, what happened, and asks them to check",
                        "steady" in said and "akshay" in said and "fall" in said and ("press" in said or "yes" in said)),
                       (f"[{how}] the confirmation is recorded", r["acknowledged"]
                        and (r["recorded_as"].startswith("said yes") if how == "says yes" else r["recorded_as"] == "pressed 1")),
                       (f"[{how}] it thanks them and hangs up by itself", bool(r["closing"]) and r["hung_up_by_steady"])]
    finally:
        await server.stop()
    print("\n--- verdict ---")
    for name, ok in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(f"hang-ups requested from Twilio (stubbed here): {len(hangups)}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tunnel", action="store_true")
    sys.exit(asyncio.run(main(not ap.parse_args().no_tunnel)))
