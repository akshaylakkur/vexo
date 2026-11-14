import numpy as np
import pytest

from signals import rows, sine
from steady.band.ingest import RingBuffer
from steady.screening.context import Activity, ContextClassifier, ContextParams
from steady.screening.features import compute
from steady.screening.preprocess import FS, build_window, coverage

WIN, PAD = 4.0, 2.0


def window_of(data):
    return build_window(data, data[-1, 0], WIN, PAD)


# ---- ring buffer ---------------------------------------------------------------------------

def test_ring_buffer_slices_by_time_and_counts_lost_samples():
    ring = RingBuffer(seconds=1, rate_hz=100)          # small capacity: forces wrap-around
    for i in range(300):
        if i == 150:
            continue                                     # one lost sample
        ring.append(1000 + i / 100, i, 0, 0, 1, 0, 0, 0)
    assert ring.lost == 1
    part = ring.since(1002.0, 1002.5)
    assert len(part) == 51 and part[0, 0] == pytest.approx(1002.0) and np.all(np.diff(part[:, 0]) > 0)


def test_ring_buffer_numbering_restarts_after_reconnect():
    ring = RingBuffer(seconds=1, rate_hz=100)
    ring.append(1, 50, 0, 0, 1, 0, 0, 0)
    ring.reset_numbering()
    ring.append(2, 0, 0, 0, 1, 0, 0, 0)
    assert ring.lost == 0


# ---- preprocessing -------------------------------------------------------------------------

def test_coverage_counts_holes():
    t = np.concatenate([np.arange(0, 1, 0.005), np.arange(2, 4, 0.005)])  # 1 s hole in 4 s
    assert coverage(t, 0.0, 4.0) == pytest.approx(0.75, abs=0.01)


def test_window_is_52hz_and_finds_gravity():
    w = window_of(rows(WIN + PAD + 0.5))
    assert w.fs == FS and len(w.acc) == pytest.approx(WIN * FS, abs=2)
    assert w.gravity[:, 2].mean() == pytest.approx(1.0, abs=0.01)
    assert w.valid_frac > 0.99 and w.lost_samples == 0


def test_too_little_data_gives_no_window():
    assert window_of(rows(1.0)) is None


# ---- features ------------------------------------------------------------------------------

def test_band_at_rest_is_quiet():
    f, _ = compute(window_of(rows(7)))
    assert f.gyro_rms < 1.0 and f.acc_dyn_rms < 0.005 and f.tremor_amp < 0.5


@pytest.mark.parametrize("freq", [4.0, 5.0, 6.5])
def test_rest_tremor_is_measured(freq):
    f, _ = compute(window_of(rows(7, gyro=sine(freq, 20.0))))
    assert f.dom_freq == pytest.approx(freq, abs=0.3)
    assert f.rest_ratio > 0.8
    assert f.peak_prom > 20
    assert f.tremor_amp == pytest.approx(20 / np.sqrt(2), rel=0.1)   # RMS of a 20 deg/s sine
    assert f.vol_rms < 3


def test_fast_tremor_is_outside_rest_band():
    f, _ = compute(window_of(rows(7, gyro=sine(9.0, 20.0))))
    assert f.dom_freq == pytest.approx(9.0, abs=0.3)
    assert f.rest_ratio < 0.2 and f.action_ratio > 0.8


def test_voluntary_movement_is_not_tremor_like():
    f, _ = compute(window_of(rows(7, gyro=sine(1.0, 80.0))))
    assert f.dom_freq == pytest.approx(1.0, abs=0.3)
    assert f.vol_rms > 40 and f.rest_ratio < 0.05


def test_out_of_band_vibration_does_not_alias_into_tremor_band():
    # 50 Hz vibration would alias to ~2 Hz after naive decimation to 52 Hz
    f, _ = compute(window_of(rows(7, gyro=sine(50.0, 30.0))))
    assert f.tremor_amp < 1.0 and f.vol_rms < 1.0


# ---- context -------------------------------------------------------------------------------

def test_context_rest_active_walking_and_not_worn():
    p = ContextParams(not_worn_after_s=10)
    c = ContextClassifier(p)
    still, _ = compute(window_of(rows(7)))
    moving, _ = compute(window_of(rows(7, gyro=sine(1.0, 80.0))))
    walk = Activity(time=100.0, state="walk", cadence=110, steps=10)
    assert c.classify(moving, 100.0, None) == "active"
    assert c.classify(moving, 100.5, walk) == "walking"
    assert c.classify(moving, 110.0, walk) == "active"            # stale activity is ignored
    assert c.classify(still, 200.0, None) == "rest"
    assert c.classify(still, 205.0, None) == "rest"
    assert c.classify(still, 211.0, None) == "not_worn"           # motionless for 10 s
    assert c.classify(moving, 212.0, None) == "active"
