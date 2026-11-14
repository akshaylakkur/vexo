"""Detector flag bits stored in windows.flags. A window with any bit set is "positive"."""

REST_TREMOR = 1 << 0
ACTION_TREMOR = 1 << 1
DYSKINESIA = 1 << 2
FREEZE = 1 << 3
FALL = 1 << 4
HARD_IMPACT = 1 << 5        # logged for analysis; not a symptom by itself

NAMES = {REST_TREMOR: "rest_tremor", ACTION_TREMOR: "action_tremor", DYSKINESIA: "dyskinesia",
         FREEZE: "freeze", FALL: "fall_suspected", HARD_IMPACT: "hard_impact"}


def names(flags: int) -> list[str]:
    return [name for bit, name in NAMES.items() if flags & bit]
