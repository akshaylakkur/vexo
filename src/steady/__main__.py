"""Steady command line.

    python -m steady run [--sim | --transport serial|ble|sim] [--no-api] [--demo]
    python -m steady doctor [--band]
    python -m steady say "text" [--stream] [--interrupt-after 1.5]
    python -m steady talk [--sim] [--say "..."]... [--episode rest_tremor] [--silence 6]
    python -m steady validate [--live] [--only keys]
    python -m steady contacts            (check the allowlist, the sender, iMessage and email)
    python -m steady brief [--force]     (send today's brief now)
    python -m steady openclaw setup|gateway|status [--profile P] [--port N] [--model M]
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
    p_call.add_argument("--fall", action="store_true", help="rehearse the real call after a fall, instead of the 'only a test' call")

    p_val = sub.add_parser("validate", help="run the labelled validation protocol and score the detectors")
    p_val.add_argument("--live", action="store_true", help="on the real band, performed by the wearer")
    p_val.add_argument("--only", help="comma-separated phase keys")

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

    p_oc = sub.add_parser("openclaw", help="connect Steady and OpenClaw (setup, test gateway, status)")
    p_oc.add_argument("action", choices=["setup", "gateway", "status"])
    p_oc.add_argument("--profile", default="", help="OpenClaw profile; empty = your main OpenClaw")
    p_oc.add_argument("--port", type=int)
    p_oc.add_argument("--model", help="e.g. ollama/glm-5.3")
    p_oc.add_argument("--lockdown", choices=["on", "off"], help="limit the OpenClaw agent to steadyctl only "
                      "(default: on for a dedicated --profile, off for your main OpenClaw)")

    p_brief = sub.add_parser("brief", help="send today's brief now (steadyd must be running)")
    p_brief.add_argument("--force", action="store_true", help="even if nothing was tracked today")
    sub.add_parser("contacts", help="check config/contacts.yaml: who may receive, who you send as, iMessage, email")

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
    if args.cmd == "brief":
        import httpx
        from .config import secret
        url = f"http://127.0.0.1:{cfg.get('api.port', 8765)}/api/brief"
        try:
            r = httpx.post(url, params={"force": str(args.force).lower()}, timeout=60,
                           headers={"Authorization": "Bearer " + (secret("STEADY_API_TOKEN", required=False) or "")})
        except httpx.HTTPError:
            print("Steady is not running. Start it with: python -m steady run")
            return 1
        print(r.json())
        return 0 if r.status_code == 200 else 1
    if args.cmd == "contacts":
        from .actions import check
        return asyncio.run(check.run(cfg))
    if args.cmd == "openclaw":
        from . import openclaw_setup as ocs
        if args.action == "setup":
            return ocs.setup(args.profile, args.port, args.model,
                             strict=None if args.lockdown is None else args.lockdown == "on")
        if args.action == "gateway":
            return ocs.gateway(args.profile, args.port)
        return ocs.status(args.profile)
    if args.cmd == "validate":
        from .validate import runner
        live = args.live
        over = dict(overrides)
        over["storage.db_path"] = "data/validation/live.db" if live else "data/validation/sim.db"
        if not live:
            over["band.transport"] = "sim"
        vcfg = config_mod.load(**{**load_args, "overrides": over})
        return asyncio.run(runner.run(vcfg, live=live, only=args.only.split(",") if args.only else None))
    if args.cmd == "call":
        from .call import cli as call_cli
        return asyncio.run(call_cli.test_call(cfg, args.to, fall=args.fall))
    if args.cmd == "init-db":
        db.init(cfg.db_path)
        print(f"database ready: {cfg.db_path}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
