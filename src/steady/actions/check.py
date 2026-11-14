"""`python -m steady contacts`: is everything in place to send? Sends nothing, prints no full address."""
from __future__ import annotations

from ..config import Config, secret
from .contacts import Contacts, mask
from .delivery import Delivery


def _line(ok, name: str, detail: str) -> bool:
    print(f"[{ {True: 'PASS', False: 'FAIL', None: 'TODO'}[ok] }] {name}: {detail}")
    return ok is not False


async def run(cfg: Config) -> int:
    book = Contacts.load()
    good = True
    for p in book.problems:
        good &= _line(False, "contacts.yaml", p)
    s = book.sender
    _line(bool(s.name) or None, "sender name", s.name or "not set (messages will say 'the wearer')")
    _line(bool(s.phone) or None, "sender phone (your iMessage number)", mask(s.phone))
    _line(bool(s.email) or None, "sender email (From address)", mask(s.email))
    if not book.items:
        good &= _line(False, "approved recipients", "none: nobody can receive anything")
    for c in book.items:
        _line(True, f"approved: {c.name} ({c.role})", f"text {mask(c.phone)} | email {mask(c.email)}")
    to = book.find(str(cfg.get("brief.to", "caregiver")))
    via = list(cfg.get("brief.via") or ["imessage"])
    if cfg.get("brief.enabled", True):
        usable = [v for v in via if to and to.address(v)]
        _line(bool(usable) or None, "end-of-day brief",
              f"{cfg.get('brief.time')} to {to.name if to else '(no such contact: ' + str(cfg.get('brief.to')) + ')'} "
              f"by {'/'.join(usable) or 'nothing usable'}")
    mode = cfg.get("delivery.mode", "live")
    _line(True if mode == "live" else None, "delivery mode", f"{mode}" + ("" if mode == "live" else ": nothing is really sent"))
    ok, detail = await Delivery(cfg, book).imessage_ready()
    good &= _line(ok, "iMessage on this Mac", detail)
    pw = bool(secret("STEADY_SMTP_PASSWORD", required=False))
    _line((book.smtp.configured and pw) or None, "email (SMTP)",
          f"host {book.smtp.host or 'not set'}, username {'set' if book.smtp.username else 'not set'}, "
          f"password {'set' if pw else 'not set (STEADY_SMTP_PASSWORD in .env)'}")
    print("\nTODO lines are things you still need to fill in." if True else "")
    return 0 if good else 1
