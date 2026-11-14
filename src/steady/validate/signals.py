"""Wrist-motion models for validation: Parkinson's signs and everyday look-alikes.

Each model is a function of the seconds since its phase began and returns what the band's
sensors would read: ax ay az in g (gravity included), gx gy gz in deg/s. They are played
into the pretend band (SimTransport.play_motion), so they pass through the real protocol:
16-bit quantisation, sensor range clipping, framing, timestamps.

The tremor model follows the clinical picture rather than a pure sine:
  - pronation-supination: one dominant rotation axis, weaker coupling into the other two
  - a slowly wandering frequency (+-0.2 Hz) and a waxing and waning amplitude
  - a second harmonic, and a small tangential acceleration at the same rhythm
  - on top of ordinary resting posture: breathing sway and sensor/physiological noise
"""
from __future__ import annotations

import math
from typing import Callable

import numpy as np

Motion = Callable[[float], dict]
_rng = np.random.default_rng(7)
TWO_PI = 2 * math.pi


def _noise(scale: float) -> float:
    return float(_rng.normal(0.0, scale))


def rest_posture(s: float, gyro_noise: float = 0.5, sway: float = 1.2) -> dict:
    """Hand resting on the lap: gravity on z, slow breathing sway, sensor noise."""
    return dict(
        ax=0.02 * math.sin(TWO_PI * 0.25 * s) + _noise(0.002),
        ay=0.01 * math.sin(TWO_PI * 0.18 * s + 1.0) + _noise(0.002),
        az=1.0 + _noise(0.002),
        gx=sway * math.sin(TWO_PI * 0.25 * s) + _noise(gyro_noise),
        gy=0.6 * sway * math.sin(TWO_PI * 0.18 * s + 0.7) + _noise(gyro_noise),
        gz=_noise(gyro_noise),
    )


def _add(base: dict, **extra: float) -> dict:
    for k, v in extra.items():
        base[k] = base.get(k, 0.0) + v
    return base


def tremor(freq: float, rms_dps: float, wander_hz: float = 0.2, wax: float = 0.25,
           envelope: Callable[[float], float] | None = None) -> Motion:
    """A pronation-supination tremor of about `rms_dps` deg/s RMS, on a resting posture."""
    # RMS over three coupled axes (1, 0.35, 0.15) plus a 15% harmonic -> scale the main axis
    peak = rms_dps * math.sqrt(2) / math.sqrt(1 + 0.35 ** 2 + 0.15 ** 2 + 0.15 ** 2)

    def f(s: float) -> dict:
        phase = TWO_PI * freq * s + (wander_hz / 0.11) * math.sin(TWO_PI * 0.11 * s)
        amp = peak * (1 - wax + wax * math.sin(TWO_PI * 0.15 * s))
        if envelope is not None:
            amp *= envelope(s)
        return _add(
            rest_posture(s),
            gx=amp * (math.sin(phase) + 0.15 * math.sin(2 * phase + 0.4)),
            gy=0.35 * amp * math.sin(phase + 1.1),
            gz=0.15 * amp * math.sin(phase + 2.0),
            ax=0.0006 * amp * math.sin(phase + 0.5),
            ay=0.0004 * amp * math.cos(phase),
        )
    return f


def tremor_with_fidget(freq: float, rms_dps: float, fidget_dps: float = 6.0) -> Motion:
    """A rest tremor while the person also shifts their hand slowly, as real patients do."""
    base = tremor(freq, rms_dps)
    return lambda s: _add(base(s), gx=fidget_dps * math.sin(TWO_PI * 1.0 * s), gz=0.5 * fidget_dps * math.sin(TWO_PI * 0.6 * s))


def quiet_rest() -> Motion:
    return rest_posture


def typing() -> Motion:
    """Small, irregular wrist jitters: broad-band, no single rhythm."""
    def f(s: float) -> dict:
        burst = 1.0 if math.sin(TWO_PI * 0.4 * s) > -0.3 else 0.2
        return _add(rest_posture(s), gx=_noise(6.0 * burst), gy=_noise(5.0 * burst), gz=_noise(4.0 * burst),
                    ax=_noise(0.01 * burst), az=_noise(0.015 * burst))
    return f


def gesturing() -> Motion:
    """Reaching and talking with the hands: large, slow, voluntary."""
    def f(s: float) -> dict:
        tilt = 0.5 * math.sin(TWO_PI * 0.4 * s)
        return dict(ax=math.sin(tilt) + _noise(0.01), ay=0.2 * math.sin(TWO_PI * 0.8 * s), az=math.cos(tilt),
                    gx=120 * math.sin(TWO_PI * 0.8 * s) + _noise(2), gy=50 * math.sin(TWO_PI * 1.7 * s + 1) + _noise(2),
                    gz=30 * math.sin(TWO_PI * 0.5 * s) + _noise(2))
    return f


def walking(tremor_rms: float = 0.0) -> Motion:
    """Arm swing at 0.9 Hz with step impacts at 1.8 Hz and their harmonics."""
    extra = tremor(4.8, tremor_rms) if tremor_rms else None

    def f(s: float) -> dict:
        step = math.sin(TWO_PI * 1.8 * s)
        d = dict(ax=0.25 * math.sin(TWO_PI * 0.9 * s) + _noise(0.01),
                 ay=0.1 * step, az=1.0 + 0.3 * max(step, 0.0) ** 4 + _noise(0.01),
                 gx=100 * math.sin(TWO_PI * 0.9 * s) + 10 * math.sin(TWO_PI * 3.6 * s) + _noise(2),
                 gy=25 * math.sin(TWO_PI * 1.8 * s + 0.5) + 6 * math.sin(TWO_PI * 5.4 * s) + _noise(2),
                 gz=15 * math.sin(TWO_PI * 0.9 * s + 1.0) + _noise(2))
        if extra:
            t = extra(s)
            for k in ("gx", "gy", "gz"):
                d[k] += t[k]
        return d
    return f


def brushing_teeth() -> Motion:
    """Vigorous 4.5 Hz scrubbing: in the tremor range, but far too violent to be one."""
    def f(s: float) -> dict:
        p = TWO_PI * 4.5 * s
        return dict(ax=0.5 * math.sin(p) + _noise(0.02), ay=0.2 * math.cos(p), az=0.9 + _noise(0.02),
                    gx=260 * math.sin(p) + 30 * math.sin(TWO_PI * 0.7 * s) + _noise(4),
                    gy=120 * math.sin(p + 0.8) + _noise(4), gz=60 * math.sin(p + 1.5) + _noise(4))
    return f


def stirring() -> Motion:
    """Steady 2.5 Hz stirring: rhythmic, but below the tremor range."""
    def f(s: float) -> dict:
        p = TWO_PI * 2.5 * s
        return _add(rest_posture(s), gx=80 * math.sin(p), gy=60 * math.cos(p), ax=0.05 * math.sin(p))
    return f


def vibration() -> Motion:
    """A vehicle or a machine: 20 Hz, outside what a limb does (tests the anti-alias filter)."""
    def f(s: float) -> dict:
        p = TWO_PI * 20 * s
        return _add(rest_posture(s), gx=15 * math.sin(p), gy=10 * math.cos(p), az=0.05 * math.sin(p))
    return f


def bumps() -> Motion:
    """Knocking the wrist on a table every 3 s: 3.5 g spikes, no free-fall before them."""
    def f(s: float) -> dict:
        d = rest_posture(s)
        if (s % 3.0) < 0.03 and s > 1:
            _add(d, az=3.0, gx=200.0)
        return d
    return f


def arm_drop_soft() -> Motion:
    """Letting the arm drop onto a cushion: a moment of low g, then a soft landing (1.6 g)."""
    def f(s: float) -> dict:
        d = rest_posture(s)
        t = s % 7.0
        if 3.0 <= t < 3.25:
            d.update(ax=0.05, ay=0.05, az=0.15)
        elif 3.25 <= t < 3.30:
            d.update(az=1.6, gx=120.0)
        return d
    return f


def fall(at: float = 4.0) -> Motion:
    """A fall: 0.35 s of free-fall, a 5 g impact with a violent turn, then lying still on the side."""
    def f(s: float) -> dict:
        if s < at:
            return _add(rest_posture(s), gx=20 * math.sin(TWO_PI * 0.7 * s))     # standing, moving a little
        if s < at + 0.35:
            return dict(ax=_noise(0.03), ay=_noise(0.03), az=0.08, gx=250.0, gy=-150.0, gz=80.0)
        if s < at + 0.40:
            return dict(ax=3.5, ay=2.5, az=2.6, gx=-600.0, gy=400.0, gz=200.0)
        if s < at + 0.9:
            k = math.exp(-(s - at - 0.4) * 8)
            return dict(ax=1.0 + 0.8 * k * math.sin(40 * s), ay=0.3 * k, az=0.1, gx=120 * k * math.sin(30 * s), gy=0, gz=0)
        return dict(ax=1.0 + _noise(0.003), ay=_noise(0.003), az=0.05 + _noise(0.003),      # on the side, still
                    gx=_noise(0.5), gy=_noise(0.5), gz=_noise(0.5))
    return f


def on_off(on_s: float = 11.0, off_s: float = 4.0) -> Callable[[float], float]:
    """An envelope for a tremor that comes and goes."""
    return lambda s: 1.0 if (s % (on_s + off_s)) < on_s else 0.0
