"""Ring buffer for raw motion samples.

The motion handler only calls `append` (cheap, no allocation). The screener asks for a
slice by real time with `since`. Columns: time (unix s), n (sample number), ax ay az (g),
gx gy gz (deg/s).
"""
from __future__ import annotations

import threading

import numpy as np

COLS = 8  # time, n, ax, ay, az, gx, gy, gz


class RingBuffer:
    def __init__(self, seconds: float = 60.0, rate_hz: float = 208.0) -> None:
        self.capacity = int(seconds * rate_hz * 1.15)
        self._data = np.zeros((self.capacity, COLS), dtype=np.float64)
        self._count = 0          # total samples ever appended
        self._lock = threading.Lock()
        self.lost = 0            # samples lost, from jumps in n
        self._last_n: int | None = None

    def reset_numbering(self) -> None:
        """Call after a (re)connect: sample numbers start again at 0."""
        self._last_n = None

    def append(self, time_s: float, n: int | None, ax, ay, az, gx, gy, gz) -> None:
        if n is not None:
            if self._last_n is not None and n > self._last_n + 1:
                self.lost += n - self._last_n - 1
            self._last_n = n
        with self._lock:
            row = self._data[self._count % self.capacity]
            row[0] = time_s
            row[1] = -1 if n is None else n
            row[2], row[3], row[4] = ax, ay, az
            row[5] = 0.0 if gx is None else gx
            row[6] = 0.0 if gy is None else gy
            row[7] = 0.0 if gz is None else gz
            self._count += 1

    @property
    def count(self) -> int:
        return self._count

    def latest_time(self) -> float | None:
        with self._lock:
            if self._count == 0:
                return None
            return float(self._data[(self._count - 1) % self.capacity, 0])

    def since(self, t_from: float, t_to: float) -> np.ndarray:
        """Copy of all samples with t_from <= time <= t_to, in time order."""
        with self._lock:
            n = min(self._count, self.capacity)
            if n == 0:
                return np.empty((0, COLS))
            start = self._count - n
            idx = np.arange(start, self._count) % self.capacity
            data = self._data[idx].copy()
        mask = (data[:, 0] >= t_from) & (data[:, 0] <= t_to)
        return data[mask]
