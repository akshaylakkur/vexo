"""Who Steady may write to, and who it writes as (config/contacts.yaml, never committed).

This file is the allowlist: a phone number or an email address that is not written here
can never receive anything, whatever a model or a task asks for. Messages go out from the
wearer's own iMessage account on this Mac and from the wearer's own email address, both
named under `sender`. Models only ever see names and roles, never an address.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..kitpath import ROOT

log = logging.getLogger("steady.actions")
CONTACTS_PATH = ROOT / "config" / "contacts.yaml"
PHONE = re.compile(r"^\+[1-9]\d{6,14}$")                    # E.164: +14155550123
EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def clean_phone(raw) -> str | None:
    if not raw:
        return None
    p = re.sub(r"[\s().\-]", "", str(raw))
    return p if PHONE.match(p) else None


def clean_email(raw) -> str | None:
    if not raw:
        return None
    e = str(raw).strip()
    return e if EMAIL.match(e) and "\n" not in e else None


def mask(address: str | None) -> str:
    if not address:
        return "-"
    if "@" in address:
        user, _, domain = address.partition("@")
        return f"{user[:1]}***@{domain}"
    return f"{address[:2]}***{address[-2:]}"


@dataclass(frozen=True)
class Contact:
    role: str
    name: str
    phone: str | None = None
    email: str | None = None

    def address(self, channel: str) -> str | None:
        return {"imessage": self.phone, "email": self.email}.get(channel)

    def public(self) -> dict:
        """What a model may see: no addresses."""
        return {"role": self.role, "name": self.name,
                "can_receive": [c for c in ("imessage", "email") if self.address(c)]}


@dataclass(frozen=True)
class Sender:
    name: str = ""
    phone: str | None = None
    email: str | None = None


@dataclass
class Smtp:
    host: str = ""
    port: int = 587
    username: str = ""
    security: str = "starttls"          # starttls | ssl

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username)


@dataclass
class Contacts:
    items: list[Contact] = field(default_factory=list)
    sender: Sender = field(default_factory=Sender)
    smtp: Smtp = field(default_factory=Smtp)
    problems: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = CONTACTS_PATH) -> "Contacts":
        if not path.exists():
            return cls(problems=[f"{path.name} does not exist: nobody can receive anything"])
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            return cls(problems=[f"{path.name} is not valid YAML ({type(e).__name__})"])
        problems: list[str] = []
        s = raw.get("sender") or {}
        sender = Sender(name=str(s.get("name") or ""), phone=clean_phone(s.get("phone")), email=clean_email(s.get("email")))
        if s.get("phone") and not sender.phone:
            problems.append("sender.phone is not a full number like +14155550123")
        if s.get("email") and not sender.email:
            problems.append("sender.email is not an email address")
        m = raw.get("smtp") or {}
        smtp = Smtp(host=str(m.get("host") or ""), port=int(m.get("port") or 587),
                    username=str(m.get("username") or ""), security=str(m.get("security") or "starttls").lower())
        items = []
        for role, c in (raw.get("contacts") or {}).items():
            if not isinstance(c, dict):
                continue
            phone, email = clean_phone(c.get("phone")), clean_email(c.get("email"))
            if c.get("phone") and not phone:
                problems.append(f"{role}: phone is not a full number like +14155550123 (ignored)")
            if c.get("email") and not email:
                problems.append(f"{role}: email is not an email address (ignored)")
            if not phone and not email:
                problems.append(f"{role}: no usable phone or email, so this contact cannot receive anything")
                continue
            items.append(Contact(role=str(role).lower(), name=str(c.get("name") or role), phone=phone, email=email))
        return cls(items, sender, smtp, problems)

    def find(self, who: str | None) -> Contact | None:
        """By role ("caregiver"), by name ("Priya"), or by a word of the name."""
        if not who:
            return None
        w = who.strip().lower().removeprefix("my ").strip()
        for c in self.items:
            if w == c.role or w == c.name.lower():
                return c
        for c in self.items:
            if w in c.name.lower().split() or c.name.lower().startswith(w):
                return c
        aliases = {"carer": "caregiver", "neurologist": "doctor", "gp": "doctor", "physician": "doctor"}
        role = aliases.get(w)
        return next((c for c in self.items if c.role == role), None) if role else None

    def allows(self, channel: str, address: str) -> bool:
        """The allowlist check: is this exact address written in contacts.yaml for this channel?"""
        return any(c.address(channel) == address for c in self.items)

    def public(self) -> list[dict]:
        return [c.public() for c in self.items]
