"""The validation protocol: labelled phases, each a known wrist condition.

expect:
  None              nothing may be detected (a negative)
  "rest_tremor" ... that episode must start during the phase (a positive)
`limit=True` marks a case the design knowingly gets "wrong"; it is reported separately and
does not count towards sensitivity or specificity.
`live` is what the wearer is asked to do when the same protocol runs on the real band.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from . import signals as S


@dataclass
class Phase:
    key: str
    title: str
    expect: Optional[str]
    motion: Callable[[float], dict]
    live: Optional[str] = None            # instruction for the wearer; None = not part of the live run
    activity: str = "still"
    limit: bool = False
    note: str = ""
    metronome_hz: float = 0.0             # live run: clicks in the headphones to pace the movement
    seconds: float = 22.0


def phases() -> list[Phase]:
    return [
        Phase("rest", "Quiet rest, hand on the lap", None, S.quiet_rest(),
              live="Rest your hand on your lap and keep it relaxed and still."),
        Phase("pd_moderate", "Parkinson's rest tremor, moderate (4.8 Hz, 20 deg/s)", "rest_tremor", S.tremor(4.8, 20),
              live="Keep your arm resting and shake your hand gently in time with the clicks.", metronome_hz=5.0),
        Phase("typing", "Typing", None, S.typing(),
              live="Type on the keyboard as you normally do."),
        Phase("pd_mild", "Parkinson's rest tremor, mild (5.5 Hz, 6 deg/s)", "rest_tremor", S.tremor(5.5, 6),
              live="Arm resting. A very small, light tremble of the hand in time with the clicks.", metronome_hz=5.5),
        Phase("gesturing", "Gesturing and reaching", None, S.gesturing(),
              live="Talk with your hands: reach, point and wave slowly."),
        Phase("pd_marked", "Parkinson's rest tremor, marked (4.2 Hz, 60 deg/s)", "rest_tremor", S.tremor(4.2, 60),
              live="Arm resting. A strong, clear hand shake in time with the clicks.", metronome_hz=4.2),
        Phase("walking", "Walking with normal arm swing", None, S.walking(), activity="walk",
              live="Walk around with your arms swinging normally."),
        Phase("pd_intermittent", "Rest tremor that comes and goes (5 Hz, 18 deg/s, 11 s on / 4 s off)", "rest_tremor",
              S.tremor(5.0, 18, envelope=S.on_off(11, 4)), seconds=28.0),
        Phase("postural_fast", "Fast postural tremor, essential-tremor range (8.5 Hz, 15 deg/s)", "action_tremor",
              S.tremor(8.5, 15)),
        Phase("stirring", "Stirring a pot (2.5 Hz)", None, S.stirring(),
              live="Mime stirring a pot at a steady pace."),
        Phase("pd_fidget", "Rest tremor with posture shifts (5 Hz, 15 deg/s + slow fidgeting)", "rest_tremor",
              S.tremor_with_fidget(5.0, 15)),
        Phase("vibration", "Vehicle vibration (20 Hz)", None, S.vibration()),
        Phase("physiological", "Normal physiological tremor (9 Hz, 1.5 deg/s)", None, S.tremor(9.0, 1.5, wax=0.1),
              note="everyone has this; far below the amplitude floor"),
        Phase("bumps", "Knocking the wrist on a table (3.5 g, no free-fall)", None, S.bumps(),
              live="Knock your wrist lightly on the table every few seconds."),
        Phase("fall", "A fall: free-fall, 5 g impact, then lying still", "fall_suspected", S.fall(4.0),
              live="When you hear 'now', let your arm drop onto a cushion or your lap, then keep it completely still."),
        Phase("arm_drop", "Arm dropped onto a cushion, soft landing (1.6 g)", None, S.arm_drop_soft(),
              note="low g but no hard impact"),
        # ---- known limits of a single wrist sensor (reported, not scored) ---------------
        Phase("limit_walk_tremor", "Rest tremor while walking", None, S.walking(tremor_rms=20), activity="walk",
              limit=True, note="by design: tremor is not judged while walking, so this is missed"),
        Phase("brushing", "Brushing teeth (4.5 Hz, vigorous)", "rest_tremor", S.brushing_teeth(), limit=True,
              live="Mime brushing your teeth hard and fast.",
              note="a false alarm we expect since the real-wrist tuning: a shaken hand reaches the same "
                   "strength, so strength no longer tells them apart; the check-in is what resolves it"),
        Phase("limit_gentle_rhythm", "A gentle voluntary 5 Hz rhythm (whisking, 30 deg/s)", "rest_tremor",
              S.tremor(5.0, 30, wax=0.05), limit=True,
              note="a false alarm we expect: at the wrist it is indistinguishable from a tremor"),
    ]
