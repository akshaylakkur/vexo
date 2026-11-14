"""Steady command line.

    python -m steady run [--sim | --transport serial|ble|sim] [--no-api] [--demo]
    python -m steady doctor [--band]
    python -m steady say "text" [--stream] [--interrupt-after 1.5]
    python -m steady talk [--sim] [--say "..."]... [--episode rest_tremor] [--silence 6]
    python -m steady validate [--live] [--only keys] [--dashboard]
    python -m steady call [--to caregiver|backup_caregiver|+15551234567]
    python -m steady init-db
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import config as config_mod
from .store import db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="steady")
    ap.add_argument("--config", help="path to steady.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run the always-on screening service")
    p_run.add_argument("--transport", choices=["serial", "ble", "sim"])
    p_run.add_argument("--sim", action="store_true", help="shortcut for --transport sim")
    p_run.add_argument("--no-api", action="store_true")
    p_run.add_argument("--demo", action="store_true", help="mode: demo")
    p_run.add_argument("--db", help="database path (default from config)")

    p_doc = sub.add_parser("doctor", help="check keys, models, devices and the band")
    p_doc.add_argument("--band", action="store_true", help="also connect to the band")

    sub.add_parser("init-db", help="create the database")

    p_call = sub.add_parser("call", help="place a test call to the caregiver (Twilio + Pipecat + Deepgram)")
    p_call.add_argument("--to", default="caregiver", help="caregiver, backup_caregiver, or a number like +15551234567")

    p_val = sub.add_parser("validate", help="run the labelled validation protocol and score the detectors")
    p_val.add_argument("--live", action="store_true", help="on the real band, performed by the wearer")
    p_val.add_argument("--only", help="comma-separated phase keys")
    p_val.add_argument("--dashboard", action="store_true", help="also serve the live dashboard")

    p_say = sub.add_parser("say", help="speak text in the headphones (Deepgram text-to-speech)")
    p_say.add_argument("text", nargs="+")
    p_say.add_argument("--stream", action="store_true", help="feed the text word by word, as a model would")
    p_say.add_argument("--interrupt-after", type=float, help="seconds after the sound starts, then stop (tests interruption)")

    p_talk = sub.add_parser("talk", help="have one conversation now (band mic -> headphones)")
    p_talk.add_argument("--sim", action="store_true", help="pretend band")
    p_talk.add_argument("--say", action="append", help="typed line instead of the microphone (repeatable); "
                        "'<tap>' taps the band, '<silence>' says nothing")
    p_talk.add_argument("--episode", choices=["rest_tremor", "action_tremor", "fall_suspected"],
                        help="open as if the band had just detected this")
    p_talk.add_argument("--sample-speech", action="store_true", help="pretend band plays the kit's recorded voice")
    p_talk.add_argument("--silence", type=float, help="seconds of silence before asking again")
    p_talk.add_argument("--max-seconds", type=float, help="wrap up after this long")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "uvicorn.access", "bleak"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    overrides = {}
    if getattr(args, "sim", False):
        overrides["band.transport"] = "sim"
    if getattr(args, "transport", None):
        overrides["band.transport"] = args.transport
    if getattr(args, "demo", False):
        overrides["mode"] = "demo"
    if getattr(args, "db", None):
        overrides["storage.db_path"] = args.db
    if overrides.get("band.transport") == "sim" and "storage.db_path" not in overrides:
        overrides["storage.db_path"] = "data/sim.db"        # pretend-band runs never touch the real database
    load_args = {"overrides": overrides}
    if args.config:
        from pathlib import Path
        load_args["path"] = Path(args.config)
    cfg = config_mod.load(**load_args)

    if args.cmd == "run":
        from .app import Steady
        asyncio.run(Steady(cfg).run(with_api=not args.no_api))
        return 0
    if args.cmd == "doctor":
        from . import doctor
        return doctor.run(cfg, check_band=args.band)
    if args.cmd == "say":
        from .voice import cli as voice_cli
        return asyncio.run(voice_cli.say(cfg, " ".join(args.text), stream=args.stream,
                                         interrupt_after=args.interrupt_after))
    if args.cmd == "talk":
        from .voice import cli as voice_cli
        return asyncio.run(voice_cli.talk(cfg, says=args.say, trigger="episode" if args.episode else "double_tap",
                                          kind=args.episode or "rest_tremor", sample_speech=args.sample_speech,
                                          silence_s=args.silence, max_s=args.max_seconds))
    if args.cmd == "validate":
        from .validate import runner
        live = args.live
        over = dict(overrides)
        over["storage.db_path"] = "data/validation/live.db" if live else "data/validation/sim.db"
        if not live:
            over["band.transport"] = "sim"
        vcfg = config_mod.load(**{**load_args, "overrides": over})
        return asyncio.run(runner.run(vcfg, live=live, only=args.only.split(",") if args.only else None,
                                      dashboard=args.dashboard))
    if args.cmd == "call":
        from .call import cli as call_cli
        return asyncio.run(call_cli.test_call(cfg, args.to))
    if args.cmd == "init-db":
        db.init(cfg.db_path)
        print(f"database ready: {cfg.db_path}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
