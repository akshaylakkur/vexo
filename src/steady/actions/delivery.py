"""Delivering a message. Only Steady sends; models only write the words.

  imessage  through Messages on this Mac (AppleScript), so it goes out from the wearer's own
            iMessage account
  email     SMTP with the wearer's own account; From is always sender.email
  dry_run   delivery.mode: dry_run writes data/outbox.jsonl and sends nothing

Every send re-checks the allowlist: the exact address must be written in
config/contacts.yaml. Text is passed as an argument, never through a shell.
"""
from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from ..config import Config, secret
from ..kitpath import ROOT
from .contacts import Contact, Contacts, mask

log = logging.getLogger("steady.actions")
OUTBOX = ROOT / "data" / "outbox.jsonl"
MAX_CHARS = {"imessage": 1200, "email": 6000}

IMESSAGE_SCRIPT = '''on run argv
  set targetPhone to item 1 of argv
  set messageText to item 2 of argv
  tell application "Messages"
    set theService to 1st account whose service type = iMessage
    send messageText to participant targetPhone of theService
  end tell
end run'''

IMESSAGE_CHECK = '''tell application "Messages"
  set n to count of (accounts whose service type = iMessage and enabled is true)
  return n as string
end tell'''


@dataclass
class Delivered:
    ok: bool
    channel: str
    detail: str = ""


class Delivery:
    def __init__(self, cfg: Config, contacts=None) -> None:
        self.cfg = cfg
        self.mode = str(cfg.get("delivery.mode", "live"))
        self.timeout_s = float(cfg.get("delivery.timeout_s", 45))
        self._contacts = contacts

    @property
    def contacts(self) -> Contacts:
        return self._contacts if self._contacts is not None else Contacts.load()

    async def send(self, contact: Contact, channel: str, text: str, subject: str | None = None,
                   task_id: int | None = None) -> Delivered:
        text = text.strip()[:MAX_CHARS.get(channel, 1200)]
        address = contact.address(channel)
        book = self.contacts
        if not text:
            return Delivered(False, channel, "empty message")
        if not address or not book.allows(channel, address):
            return Delivered(False, channel, f"{contact.name} has no approved {channel} address in config/contacts.yaml")
        if self.mode == "dry_run":
            return self._outbox(contact, channel, address, text, subject, task_id)
        if channel == "imessage":
            if not book.sender.phone:
                return Delivered(False, channel, "sender.phone is not set in config/contacts.yaml")
            return await self._imessage(address, text)
        if channel == "email":
            if not book.sender.email:
                return Delivered(False, channel, "sender.email is not set in config/contacts.yaml")
            return await asyncio.to_thread(self._email, book, address, contact.name, text, subject or "A message from Steady")
        return Delivered(False, channel, f"unknown channel {channel!r}")

    def _outbox(self, contact, channel, address, text, subject, task_id) -> Delivered:
        OUTBOX.parent.mkdir(parents=True, exist_ok=True)
        with OUTBOX.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "task_id": task_id, "role": contact.role, "name": contact.name,
                                "channel": channel, "to": address, "subject": subject, "text": text}) + "\n")
        return Delivered(True, channel, "dry run: written to the outbox, not sent")

    async def _osascript(self, script: str, *args: str) -> tuple[int, str]:
        if sys.platform != "darwin":
            return 126, "iMessage needs a Mac"
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/osascript", "-", *args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(script.encode()), self.timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "Messages did not answer in time (is macOS waiting for you to allow automation?)"
        return proc.returncode or 0, out.decode(errors="replace").strip()[-300:]

    async def _imessage(self, phone: str, text: str) -> Delivered:
        code, out = await self._osascript(IMESSAGE_SCRIPT, phone, text)
        if code != 0:
            log.warning("iMessage to %s failed (exit %d)", mask(phone), code)
            return Delivered(False, "imessage", f"Messages could not send it: {out}")
        return Delivered(True, "imessage", "handed to Messages")

    async def imessage_ready(self) -> tuple[bool, str]:
        """Is an iMessage account signed in and enabled on this Mac? Sends nothing."""
        code, out = await self._osascript(IMESSAGE_CHECK)
        if code != 0:
            return False, out or "Messages could not be asked"
        return (out.strip() not in ("", "0")), f"{out.strip()} iMessage account(s) enabled"

    def _email(self, book: Contacts, to: str, to_name: str, text: str, subject: str) -> Delivered:
        smtp = book.smtp
        password = secret("STEADY_SMTP_PASSWORD", required=False)
        if not smtp.configured or not password:
            return Delivered(False, "email", "email is not set up yet: fill in `smtp` in config/contacts.yaml "
                                             "and add STEADY_SMTP_PASSWORD to .env")
        msg = EmailMessage()
        msg["From"] = formataddr((book.sender.name or "Steady", book.sender.email))
        msg["To"] = formataddr((to_name, to))
        msg["Subject"] = " ".join(subject.split())[:150]
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg.set_content(text)
        try:
            ctx = ssl.create_default_context()
            if smtp.security == "ssl":
                server = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=self.timeout_s, context=ctx)
            else:
                server = smtplib.SMTP(smtp.host, smtp.port, timeout=self.timeout_s)
                server.starttls(context=ctx)
            with server:
                server.login(smtp.username, password)
                server.send_message(msg, from_addr=book.sender.email, to_addrs=[to])
        except smtplib.SMTPAuthenticationError:
            return Delivered(False, "email", "the mail server refused the login (check the username and the app password)")
        except (smtplib.SMTPException, OSError) as e:
            return Delivered(False, "email", f"the mail server could not be used ({type(e).__name__})")
        return Delivered(True, "email", "sent")
