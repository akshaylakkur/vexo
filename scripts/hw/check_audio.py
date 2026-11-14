"""Hardware check: headphones + Deepgram Aura-2 text-to-speech, streamed.

Plays a short tone, then speaks a sentence through the chosen output device while it is
still arriving from Deepgram, and reports the time to first audio. The key is read from the
environment or .env and never printed.

    .venv/bin/python scripts/hw/check_audio.py                    # device whose name contains "Beats"
    .venv/bin/python scripts/hw/check_audio.py --device Speakers  # another output
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import sounddevice as sd
import websockets

ROOT = Path(__file__).resolve().parents[2]
RATE = 24000


def env_key(name):
    value = os.environ.get(name, "").strip()
    env = ROOT / ".env"
    if not value and env.exists():
        for raw in env.read_text().splitlines():
            k, _, v = raw.strip().removeprefix("export ").partition("=")
            if k.strip() == name:
                value = v.strip().strip("'\"")
    if not value:
        sys.exit(f"{name} is not set (environment or .env)")
    return value


def find_output(fragment):
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_output_channels"] > 0 and fragment.lower() in dev["name"].lower():
            return i, dev["name"]
    sys.exit(f"No output device containing '{fragment}'. Devices:\n{sd.query_devices()}")


async def speak(text, device, voice):
    key = env_key("DEEPGRAM_API_KEY")
    url = "wss://api.deepgram.com/v1/speak?" + urlencode(
        {"model": voice, "encoding": "linear16", "sample_rate": RATE})
    stream = sd.OutputStream(samplerate=RATE, channels=1, dtype="int16", device=device)
    stream.start()
    t_send = time.perf_counter()
    first = None
    total = 0
    try:
        async with websockets.connect(url, additional_headers={"Authorization": "Token " + key}) as ws:
            await ws.send(json.dumps({"type": "Speak", "text": text}))
            await ws.send(json.dumps({"type": "Flush"}))
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                if isinstance(msg, bytes):
                    if first is None:
                        first = time.perf_counter() - t_send
                    pcm = np.frombuffer(msg, dtype=np.int16)
                    total += len(pcm)
                    await asyncio.to_thread(stream.write, pcm)
                else:
                    kind = json.loads(msg).get("type")
                    if kind == "Flushed":
                        break
                    if kind in ("Error", "Warning"):
                        print("Deepgram said:", kind)
            await ws.send(json.dumps({"type": "Close"}))
    except websockets.InvalidStatus as e:
        sys.exit(f"Deepgram TTS refused the connection: status {e.response.status_code}")
    finally:
        await asyncio.to_thread(time.sleep, 0.3)
        stream.stop()
        stream.close()
    return first, total / RATE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="Beats")
    ap.add_argument("--voice", default="aura-2-thalia-en")
    ap.add_argument("--text", default="Hi, this is Steady. If you can hear me in your headphones, the voice pathway works.")
    args = ap.parse_args()
    idx, name = find_output(args.device)
    print(f"Output device: {name} (#{idx})")

    t = np.arange(int(0.4 * RATE)) / RATE
    tone = (0.2 * np.sin(2 * np.pi * 660 * t) * 32767).astype(np.int16)
    sd.play(tone, RATE, device=idx, blocking=True)
    print("[PASS] tone played (660 Hz, 0.4 s)")

    print("Note: this text is sent to Deepgram (deepgram.com) to be turned into speech.")
    first, seconds = asyncio.run(speak(args.text, idx, args.voice))
    ok = first is not None and seconds > 1
    print(f"[{'PASS' if ok else 'FAIL'}] Deepgram TTS: first audio after {first * 1000 if first else -1:.0f} ms, "
          f"{seconds:.1f} s of speech played on {name}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
