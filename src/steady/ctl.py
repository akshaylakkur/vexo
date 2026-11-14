"""steadyctl: how OpenClaw (and you) talk to a running steadyd.

    steadyctl task get 12
    steadyctl task send 12 --text "..." [--subject "..."]
    steadyctl task done 12 --note "..."        steadyctl task fail 12 --note "..."
    steadyctl tasks
    steadyctl summary [--period last_hour|today|week]
    steadyctl contacts

Reads STEADY_API_TOKEN from the environment or the project's .env. Prints JSON (summary:
text). Exit code 0 on success, 2 when Steady refuses (the reason is printed), 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

from .config import load, secret


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="steadyctl", description="Talk to a running steadyd.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("task").add_subparsers(dest="action", required=True)
    g = t.add_parser("get")
    g.add_argument("id", type=int)
    s = t.add_parser("send")
    s.add_argument("id", type=int)
    s.add_argument("--text", required=True)
    s.add_argument("--subject")
    for name in ("done", "fail"):
        p = t.add_parser(name)
        p.add_argument("id", type=int)
        p.add_argument("--note", default="")
    sub.add_parser("tasks")
    sm = sub.add_parser("summary")
    sm.add_argument("--period", default="today", choices=["last_hour", "today", "week"])
    sub.add_parser("contacts")
    args = ap.parse_args(argv)

    cfg = load()
    token = secret("STEADY_API_TOKEN", required=False)
    if not token:
        print("STEADY_API_TOKEN is not set (run: python -m steady openclaw setup)", file=sys.stderr)
        return 1
    base = f"http://{cfg.get('api.host', '127.0.0.1')}:{cfg.get('api.port', 8765)}/api"
    client = httpx.Client(base_url=base, headers={"Authorization": "Bearer " + token}, timeout=60)

    if args.cmd == "task":
        if args.action == "get":
            req = ("GET", f"/tasks/{args.id}", None)
        elif args.action == "send":
            req = ("POST", f"/tasks/{args.id}/send", {"text": args.text, "subject": args.subject})
        else:
            req = ("POST", f"/tasks/{args.id}/status", {"ok": args.action == "done", "note": args.note})
    elif args.cmd == "tasks":
        req = ("GET", "/tasks", None)
    elif args.cmd == "summary":
        req = ("GET", f"/summary?period={args.period}", None)
    else:
        req = ("GET", "/contacts", None)

    try:
        r = client.request(req[0], req[1], json=req[2])
    except httpx.HTTPError as e:
        print(f"Steady is not reachable ({type(e).__name__}). Stop here: do not start, restart or change "
              "anything. The wearer has to start Steady themselves.", file=sys.stderr)
        return 1
    try:
        data = r.json()
    except ValueError:
        data = {"detail": r.text}
    if r.status_code != 200:
        print(f"Refused: {data.get('detail', data)}", file=sys.stderr)
        return 2 if r.status_code in (409, 429) else 1
    print(data["text"] if args.cmd == "summary" else json.dumps(data, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
