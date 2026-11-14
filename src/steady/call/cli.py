"""`python -m steady call`: place one test call, to check Twilio, the tunnel and the call's voice."""
from __future__ import annotations

import sqlite3
import time

from ..config import Config


async def test_call(cfg: Config, to: str) -> int:
    from ..app import Steady

    steady = Steady(cfg)
    await steady.writer.start()
    caller = steady.caller
    try:
        await caller.start(need_contacts=not to.startswith("+"))
        if not caller.ready:
            print("Caregiver calls are not set up:", "; ".join(caller.problems(not to.startswith("+"))) or "see the log above")
            return 1
        print(f"Call server reachable at {caller.server.public_url}. Placing a test call to {to}...")
        ok = await caller.alert("This is a test.", time.time(), test=True, only=to)
        await steady.writer.flush()
        conn = sqlite3.connect(cfg.db_path)
        status, note = conn.execute("SELECT status, result_note FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        print(f"\nresult: {status} | {note}")
        return 0 if ok else 1
    finally:
        await caller.stop()
        await steady.writer.close()
