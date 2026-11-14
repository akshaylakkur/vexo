"""Hardware check: band microphone -> Deepgram live speech-to-text.

Loopback mode (default): a known sentence is spoken by Deepgram TTS through the laptop
speakers while the band's microphone streams to Deepgram STT; the transcript is compared.
Keep the band near the laptop. --listen instead records you for N seconds.

    .venv/bin/python scripts/hw/check_stt.py              # loopback via MacBook speakers
    .venv/bin/python scripts/hw/check_stt.py --listen 8   # speak to the band yourself
"""
import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vexo-open-kit" / "python"))
sys.path.insert(0, str(ROOT / "vexo-open-kit" / "examples" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import speech  # noqa: E402  (kit helper: Deepgram live STT)
from check_audio import find_output, speak  # noqa: E402
from vexo_open.sync import Band  # noqa: E402

BAND = 3
PHRASE = "The quick brown fox jumps over the lazy dog near the riverbank."


def words(s):
    return re.findall(r"[a-z]+", s.lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=float, default=0, help="seconds to record you instead of loopback")
    ap.add_argument("--speaker", default="MacBook Pro Speakers")
    ap.add_argument("--transport", default="serial")
    args = ap.parse_args()

    band = Band.connect(BAND, transport=args.transport)
    heard = []
    first_partial = {}
    ears = speech.create(endpointing=300).start(
        lambda text, final: (first_partial.setdefault("t", time.perf_counter()), final and heard.append(text)))
    band.on("audio", lambda a: ears.feed(a.opus, a.seq))
    try:
        band.configure(audio=True)  # light goes solid blue: mic on
        band.wait(0.5)
        if args.listen:
            print(f"Speak to the band now ({args.listen:.0f} s)...")
            band.wait(args.listen)
        else:
            idx, name = find_output(args.speaker)
            print(f"Playing a known sentence on {name}; the band's mic should hear it.")
            asyncio.run(speak(PHRASE, idx, "aura-2-thalia-en"))
            band.wait(1.5)
        band.configure(audio=False)
    finally:
        text = ears.stop()
        band.disconnect()

    print(f"Transcript: {text!r}")
    if args.listen:
        ok = bool(text.strip())
    else:
        want, got = set(words(PHRASE)), set(words(text))
        overlap = len(want & got) / len(want)
        print(f"Word overlap with the spoken sentence: {overlap:.0%}")
        ok = overlap >= 0.6
    print(f"[{'PASS' if ok else 'FAIL'}] band mic -> Deepgram STT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
