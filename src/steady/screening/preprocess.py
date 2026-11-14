"""Turn raw, slightly irregular 208 Hz samples into a clean 52 Hz analysis window.

Steps (SPEC 4.3): uniform grid by real time -> anti-alias low-pass 20 Hz -> decimate x4 ->
gravity (0.3 Hz low-pass) and dynamic acceleration -> crop the padding.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from scipy import signal

RAW_FS = 208.0
DECIMATE = 4
FS = RAW_FS / DECIMATE          # 52 Hz analysis rate
MAX_GAP_S = 0.05                # a hole longer than this counts as missing data


@dataclass
class Window:
    t_start: float
    t_end: float
    fs: float
    acc: np.ndarray             # (N, 3) g, raw (with gravity), at FS
    gyro: np.ndarray            # (N, 3) deg/s, at FS
    gravity: np.ndarray         # (N, 3) g
    dyn: np.ndarray             # (N, 3) g, acc - gravity
    raw: np.ndarray             # raw rows of the window itself (for fall/impact work later)
    valid_frac: float
    n_samples: int
    lost_samples: int


@lru_cache(maxsize=None)
def _sos(kind: str, cutoff, fs: float, order: int = 4):
    return signal.butter(order, cutoff, btype=kind, fs=fs, output="sos")


def coverage(times: np.ndarray, t_start: float, t_end: float) -> float:
    """Share of [t_start, t_end] covered by samples with no hole longer than MAX_GAP_S."""
    span = t_end - t_start
    if len(times) < 2 or span <= 0:
        return 0.0
    edges = np.concatenate(([t_start], times, [t_end]))
    gaps = np.diff(edges)
    missing = gaps[gaps > MAX_GAP_S].sum()
    return float(max(0.0, 1.0 - missing / span))


def build_window(rows: np.ndarray, t_end: float, window_s: float, pad_s: float) -> Window | None:
    """rows: raw samples covering at least [t_end - window_s - pad_s, t_end]."""
    t_start = t_end - window_s
    t_pad = t_start - pad_s
    rows = rows[(rows[:, 0] >= t_pad) & (rows[:, 0] <= t_end)]
    in_win = rows[rows[:, 0] >= t_start]
    n_samples = len(in_win)
    valid_frac = coverage(in_win[:, 0], t_start, t_end) if n_samples else 0.0
    nums = in_win[:, 1]
    nums = nums[nums >= 0]
    lost = int(max(0, (nums[-1] - nums[0] + 1) - len(nums))) if len(nums) > 1 and np.all(np.diff(nums) > 0) else 0
    if len(rows) < int(0.5 * (window_s + pad_s) * RAW_FS):
        return None

    # 1. uniform raw grid
    n_raw = int(round((t_end - t_pad) * RAW_FS))
    grid = t_end - (np.arange(n_raw)[::-1] / RAW_FS)
    order = np.argsort(rows[:, 0], kind="stable")
    rows = rows[order]
    uni = np.column_stack([np.interp(grid, rows[:, 0], rows[:, c]) for c in range(2, 8)])

    # 2-3. anti-alias + decimate
    filt = signal.sosfiltfilt(_sos("lowpass", 20.0, RAW_FS), uni, axis=0)
    dec = filt[::DECIMATE]
    t_dec = grid[::DECIMATE]

    acc, gyro = dec[:, 0:3], dec[:, 3:6]
    # 4. gravity and dynamic acceleration
    gravity = signal.sosfiltfilt(_sos("lowpass", 0.3, FS, order=2), acc, axis=0)
    dyn = acc - gravity

    # 5. crop padding
    keep = t_dec >= t_start
    return Window(
        t_start=t_start, t_end=t_end, fs=FS,
        acc=acc[keep], gyro=gyro[keep], gravity=gravity[keep], dyn=dyn[keep],
        raw=in_win, valid_frac=valid_frac, n_samples=n_samples, lost_samples=lost,
    )
