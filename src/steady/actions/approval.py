"""Who has to say yes (SPEC 7.4). Decided by Steady, never by a model.

There are exactly two ways a message leaves Steady:
  voice      the wearer asks out loud for a message or an email: needs their yes (tier 1)
  schedule   the end-of-day brief to the contact the wearer set up in config (tier 0)
Messages have no emergency path. After a possible fall, Steady phones the caregiver instead (steady/call).
"""
from __future__ import annotations

VOICE_TYPES = ("message_contact", "email_contact")
TYPES = VOICE_TYPES + ("daily_brief",)
CHANNEL = {"message_contact": ["imessage"], "email_contact": ["email"]}
TIER_NAMES = {0: "preconfigured", 1: "confirm first"}


def tier_for(task_type: str, origin: str) -> int:
    if task_type in VOICE_TYPES and origin == "voice":
        return 1
    if task_type == "daily_brief" and origin == "schedule":
        return 0
    raise ValueError(f"{task_type!r} cannot be created from {origin!r}")
