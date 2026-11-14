"""Per-window features (SPEC 4.4).

Spectra are computed per gyro axis on mean-removed data and summed (never on the
magnitude, which would distort frequencies). Band "power" is reported as band RMS in the
signal's own unit (deg/s or g), which keeps thresholds readable.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
from scipy import signal

from .preprocess import Window, _sos

NFFT = 512


@dataclass
class Features:
    acc_dyn_rms: float
    gyro_rms: float
    vol_rms: float
    total_rms: float
    dom_freq: float
    peak_prom: float
    peak_conc: float            # share of 0.5-15 Hz power within +-1 Hz of the peak: is it one rhythm?
    rest_ratio: float
    action_ratio: float
    tremor_amp: float
    dysk_rms: float
    dysk_flat: float
    freeze_index: float
    vigor: float
    pitch: float
    roll: float
    arm_swing_deg: float | None = None
    step_reg: float | None = None

    def as_row(self) -> dict:
        return {k: (None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(v, 5))
                for k, v in asdict(self).items()}


@dataclass
class Spectrum:
    freqs: np.ndarray
    psd: np.ndarray             # one-sided PSD, summed over axes, unit^2/Hz

    def band_ms(self, lo: float, hi: float) -> float:
        """Mean square of the signal in [lo, hi) Hz."""
        m = (self.freqs >= lo) & (self.freqs < hi)
        df = self.freqs[1] - self.freqs[0]
        return float(self.psd[m].sum() * df)

    def band_rms(self, lo: float, hi: float) -> float:
        return math.sqrt(max(self.band_ms(lo, hi), 0.0))


def spectrum(x: np.ndarray, fs: float) -> Spectrum:
    """x: (N, axes). Hann taper, zero-pad to NFFT, PSD summed over axes."""
    x = x - x.mean(axis=0)
    n = len(x)
    w = np.hanning(n)[:, None]
    nfft = max(NFFT, 1 << (n - 1).bit_length())
    spec = np.fft.rfft(x * w, n=nfft, axis=0)
    psd = (np.abs(spec) ** 2).sum(axis=1) / (fs * (w ** 2).sum())
    psd[1:-1] *= 2.0
    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    # Parseval: sum(psd) * df == sum((x*w)^2) / sum(w^2), the window-weighted mean square,
    # whatever the zero-padding. No extra scaling.
    return Spectrum(freqs, psd)


def _safe_ratio(a: float, b: float) -> float:
    return a / b if b > 1e-12 else 0.0


def gait_features(w: Window) -> tuple[float | None, float | None]:
    """Arm swing (deg, per stride) and step regularity, meaningful only while walking."""
    fs = w.fs
    g = w.gyro - w.gyro.mean(axis=0)
    if len(g) < fs * 3:
        return None, None
    # main swing axis = first principal component of the gyro
    _, _, vt = np.linalg.svd(g, full_matrices=False)
    swing = g @ vt[0]
    swing = signal.sosfiltfilt(_sos("bandpass", (0.3, 3.0), fs, order=2), swing)
    angle = np.cumsum(swing) / fs
    angle = signal.sosfiltfilt(_sos("highpass", 0.3, fs, order=2), angle)
    arm_swing = float(np.percentile(angle, 95) - np.percentile(angle, 5))

    mag = np.linalg.norm(w.dyn, axis=1)
    mag = mag - mag.mean()
    ac = np.correlate(mag, mag, mode="full")[len(mag) - 1:]
    if ac[0] <= 1e-12:
        return arm_swing, None
    ac = ac / ac[0]
    lo, hi = int(0.8 * fs), min(int(1.6 * fs), len(ac) - 1)
    step_reg = float(ac[lo:hi + 1].max()) if hi > lo else None
    return arm_swing, step_reg


def compute(w: Window, walking: bool = False) -> tuple[Features, Spectrum]:
    fs = w.fs
    gyro_c = w.gyro - w.gyro.mean(axis=0)
    gyro_mag = np.linalg.norm(gyro_c, axis=1)
    sg = spectrum(w.gyro, fs)

    total_ms = sg.band_ms(0.5, 15.0)
    m = (sg.freqs >= 1.0) & (sg.freqs <= 15.0)
    band_psd = sg.psd[m]
    peak_i = int(np.argmax(band_psd))
    dom_freq = float(sg.freqs[m][peak_i])
    median = float(np.median(band_psd))
    peak_prom = _safe_ratio(float(band_psd[peak_i]), median)
    peak_conc = _safe_ratio(sg.band_ms(max(0.5, dom_freq - 1.0), dom_freq + 1.0), total_ms)

    tremor_amp = sg.band_rms(3.5, 12.0)     # deg/s RMS in the tremor band (exact at band edges)

    dm = (sg.freqs >= 1.0) & (sg.freqs < 4.0)
    dpsd = np.maximum(sg.psd[dm], 1e-20)
    dysk_flat = float(np.exp(np.mean(np.log(dpsd))) / np.mean(dpsd))

    sa = spectrum(w.dyn, fs)
    freeze_index = _safe_ratio(sa.band_ms(3.0, 8.0), sa.band_ms(0.5, 3.0))

    vigor = float(np.percentile(gyro_mag, 95))      # how briskly the wrist moves (deg/s)

    gx, gy, gz = w.gravity.mean(axis=0)
    pitch = math.degrees(math.atan2(-gx, math.hypot(gy, gz)))
    roll = math.degrees(math.atan2(gy, gz))

    arm_swing, step_reg = gait_features(w) if walking else (None, None)

    feats = Features(
        acc_dyn_rms=float(np.sqrt(np.mean(np.sum(w.dyn ** 2, axis=1)))),
        gyro_rms=float(np.sqrt(np.mean(gyro_mag ** 2))),
        vol_rms=sg.band_rms(0.5, 3.0),
        total_rms=math.sqrt(total_ms),
        dom_freq=dom_freq,
        peak_prom=peak_prom,
        peak_conc=peak_conc,
        rest_ratio=_safe_ratio(sg.band_ms(3.5, 7.5), total_ms),
        action_ratio=_safe_ratio(sg.band_ms(4.0, 12.0), total_ms),
        tremor_amp=tremor_amp,
        dysk_rms=sg.band_rms(1.0, 4.0),
        dysk_flat=dysk_flat,
        freeze_index=freeze_index,
        vigor=vigor,
        pitch=pitch,
        roll=roll,
        arm_swing_deg=arm_swing,
        step_reg=step_reg,
    )
    return feats, sg
