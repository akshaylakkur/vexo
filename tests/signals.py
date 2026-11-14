"""Synthetic wrist signals for tests: raw rows as the ring buffer holds them."""
from __future__ import annotations

from typing import Callable

import numpy as np

Fn = Callable[[np.ndarray], np.ndarray]


def rows(seconds: float, gyro: Fn | None = None, acc: Fn | None = None, fs: float = 210.0,
         t0: float = 1_700_000_000.0, noise_dps: float = 0.2, noise_g: float = 0.001,
         seed: int = 0) -> np.ndarray:
    """(N, 8) rows: time, n, ax, ay, az, gx, gy, gz. Default: band lying still, z up.

    fs is deliberately not 208: the real band's clock runs a few percent off.
    gyro(t) -> (N, 3) deg/s, acc(t) -> (N, 3) g (gravity included).
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * fs)
    t = np.arange(n) / fs
    a = np.tile([0.0, 0.0, 1.0], (n, 1)) if acc is None else acc(t)
    g = np.zeros((n, 3)) if gyro is None else gyro(t)
    a = a + rng.normal(0, noise_g, (n, 3))
    g = g + rng.normal(0, noise_dps, (n, 3)) + np.array([1.5, -0.8, 0.4])  # small gyro bias, like the real band
    return np.column_stack([t0 + t, np.arange(n), a, g])


def sine(freq: float, amp: float, axis: int = 0) -> Fn:
    def f(t: np.ndarray) -> np.ndarray:
        out = np.zeros((len(t), 3))
        out[:, axis] = amp * np.sin(2 * np.pi * freq * t)
        return out
    return f


def tremor_with_gravity(freq: float, amp_dps: float) -> tuple[Fn, Fn]:
    """A rest tremor: a small rotational oscillation; gravity stays mostly on z."""
    return sine(freq, amp_dps, axis=0), (lambda t: np.tile([0.0, 0.0, 1.0], (len(t), 1)))
