"""`python -m steady call`: place one test call, to check Twilio, the tunnel and the call's voice."""
from __future__ import annotations

import sqlite3
import time

from ..config import Config


FALL = ("At {at} the wristband detected a possible fall: a drop, a hard impact, then stillness. "
        "Steady asked through the headphones twice and got no answer.")


async def test_call(cfg: Config, to: str, fall: bool = False) -> int:
    """fall=True: the call a real fall would cause, word for word (a rehearsal), not the "only a test" call."""
    from ..app import Steady

    steady = Steady(cfg)
    await steady.writer.start()
    caller = steady.caller
    caller.explicit = True                    # you asked for this call: it is placed even in dry-run mode
    try:
        await caller.start(need_contacts=not to.startswith("+"))
        if not caller.ready:
            print("Caregiver calls are not set up:", "; ".join(caller.problems(not to.startswith("+"))) or "see the log above")
            return 1
        print(f"Call server reachable at {caller.server.public_url}. Placing a "
              f"{'fall rehearsal' if fall else 'test'} call to {to}...")
        if fall:
            ok = await caller.alert(FALL.format(at=time.strftime("%H:%M")), time.time() - 60, only=to)
        else:
            ok = await caller.alert("This is a test.", time.time(), test=True, only=to)
        if caller._tasks:                          # someone confirmed: let the call finish before the line closes
            import asyncio
            await asyncio.gather(*caller._tasks, return_exceptions=True)
        await steady.writer.flush()
        conn = sqlite3.connect(cfg.db_path)
        status, note = conn.execute("SELECT status, result_note FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        print(f"\nresult: {status} | {note}")
        return 0 if ok else 1
    finally:
        await caller.stop()
        await steady.writer.close()
