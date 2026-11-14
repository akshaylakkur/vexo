"""Is a tap on the band a real tap? (Found on the real band, 2026-11-14.)

A shaking hand makes the band report taps and double-taps by itself, and wearing the band
makes it report tilts all the time. So:
  - tilts are never used as a gesture
  - a tap or a double-tap only counts while the wrist is calm; while it is shaking, the
    tremor detector is what speaks for the person, not the tap
"""
from __future__ import annotations

CALM_ACC_DYN_G = 0.20        # a deliberate tap on a still wrist stays far below this over 4 s
CALM_TREMOR_DPS = 20.0


def wrist_calm(last: dict | None) -> bool:
    """`last` is the newest assessment (Screener.last). No data yet counts as calm."""
    if not last or not last.get("valid"):
        return True
    if last.get("flags"):
        return False
    return (last.get("acc_dyn_rms") or 0.0) < CALM_ACC_DYN_G and (last.get("tremor_amp") or 0.0) < CALM_TREMOR_DPS
