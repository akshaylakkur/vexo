import asyncio
import sqlite3

import numpy as np
import pytest

from signals import rows, sine
from steady.band.ingest import RingBuffer
from steady.bus import Bus
from steady.screening import flags as F
from steady.screening.context import ContextClassifier, ContextParams
from steady.screening.detectors.fall import FallDetector
from steady.screening.detectors.tremor import TremorDetector
from steady.screening.episodes import EpisodeTracker
from steady.screening.features import compute
from steady.screening.preprocess import build_window
from steady.store.db import Writer


def detect(data, ctx=None):
    w = build_window(data, data[-1, 0], 4.0, 2.0)
    f, _ = compute(w)
    ctx = ctx or ContextClassifier(ContextParams()).classify(f, data[-1, 0], None)
    return TremorDetector()(f, ctx), ctx, f


def white(amp):
    rng = np.random.default_rng(3)
    return lambda t: rng.normal(0, amp, (len(t), 3))


# ---- tremor --------------------------------------------------------------------------------

@pytest.mark.parametrize("freq,amp", [(4.0, 20), (5.0, 8), (6.5, 60), (7.0, 12)])
def test_rest_tremor_detected(freq, amp):
    flag, ctx, _ = detect(rows(7, gyro=sine(freq, amp * 1.414)))
    assert ctx == "rest" and flag == F.REST_TREMOR


def test_fast_postural_tremor_is_action_tremor():
    flag, ctx, _ = detect(rows(7, gyro=sine(9.0, 25)))
    assert ctx == "rest" and flag == F.ACTION_TREMOR


@pytest.mark.parametrize("name,gyro", [
    ("still", None),
    ("slow voluntary", sine(1.0, 80)),
    ("below the range", sine(2.5, 80)),
    ("too weak", sine(5.0, 2.0)),
    ("broad-band jitter", white(8.0)),
    ("vibration", sine(20.0, 20)),
    ("violent scrubbing", sine(4.5, 260)),
])
def test_look_alikes_are_not_tremor(name, gyro):
    flag, _, f = detect(rows(7, gyro=gyro))
    assert flag == 0, (name, f)


def test_no_tremor_judgement_while_walking():
    flag, _, _ = detect(rows(7, gyro=sine(5.0, 30)), ctx="walking")
    assert flag == 0


# ---- fall ----------------------------------------------------------------------------------

def fill(ring, segments, fs=208.0, t0=1000.0):
    """segments: [(seconds, (ax, ay, az))] played back to back. Returns the end time."""
    rng = np.random.default_rng(1)
    t, n = t0, 0
    for seconds, acc in segments:
        for _ in range(int(seconds * fs)):
            a = np.array(acc) + rng.normal(0, 0.003, 3)
            ring.append(t, n, a[0], a[1], a[2], 0, 0, 0)
            t += 1 / fs
            n += 1
    return t


def run_fall(segments):
    ring = RingBuffer(60, 208)
    det = FallDetector()
    end = fill(ring, segments)
    events = []
    for now in np.arange(1000.0, end + 0.01, 2.0):      # ticks every 2 s, as the screener does
        events += det.update(ring, float(now))
    events += det.update(ring, end)
    return events


def test_fall_free_fall_impact_then_still_on_the_side():
    ev = run_fall([(5, (0, 0, 1)), (0.3, (0, 0, 0.1)), (0.05, (3.5, 2.5, 2.6)), (14, (1, 0, 0.05))])
    assert [e.kind for e in ev] == ["fall_suspected"]
    m = ev[0].metrics
    assert m["impact_g"] > 4 and m["orientation_change_deg"] > 60 and m["still"]


def test_impact_without_free_fall_is_nothing():
    assert run_fall([(5, (0, 0, 1)), (0.05, (0, 0, 4.5)), (14, (0, 0, 1))]) == []


def test_free_fall_without_impact_is_nothing():
    assert run_fall([(5, (0, 0, 1)), (0.3, (0, 0, 0.1)), (0.05, (0, 0, 1.6)), (14, (0, 0, 1))]) == []


def test_impact_then_carrying_on_is_only_a_hard_impact():
    rng = np.random.default_rng(5)
    ring = RingBuffer(60, 208)
    det = FallDetector()
    end = fill(ring, [(5, (0, 0, 1)), (0.3, (0, 0, 0.1)), (0.05, (0, 0, 4.5))])
    t, n = end, 100000
    for _ in range(int(14 * 208)):                       # same orientation, arm keeps moving
        a = np.array([0, 0, 1.0]) + rng.normal(0, 0.2, 3)
        ring.append(t, n, *a, 0, 0, 0)
        t += 1 / 208
        n += 1
    assert [e.kind for e in det.update(ring, t)] == ["hard_impact"]


def test_one_fall_gives_one_event():
    ev = run_fall([(5, (0, 0, 1)), (0.3, (0, 0, 0.1)), (0.05, (3.5, 2.5, 2.6)), (25, (1, 0, 0.05))])
    assert len(ev) == 1


# ---- episodes ------------------------------------------------------------------------------

def test_episode_hysteresis(tmp_path):
    pattern = "0 1 0 0 0 1 1 0 1 1 1 0 1 0 0 1 1 0 0 0 0 0".split()     # 1 = rest tremor window
    amps = [6.0] * 9 + [25.0] * 13

    async def go():
        w = Writer(tmp_path / "e.db", interval=0.02)
        await w.start()
        tr = EpisodeTracker(w, Bus())
        log = []
        for i, (p, amp) in enumerate(zip(pattern, amps)):
            row = {"valid": 1, "flags": F.REST_TREMOR if p == "1" else 0, "t_start": 100 + 2 * i,
                   "t_end": 104 + 2 * i, "tremor_amp": amp, "dom_freq": 5.0}
            for what, ep in await tr.update(row):
                log.append((i, what, ep["severity"]))
        await w.close()
        return log

    log = asyncio.run(go())
    # one isolated positive starts nothing; 3 of 5 starts it; one or two quiet windows do not
    # end it; a stronger tremor escalates it; 3 quiet windows in a row end it
    assert log == [(8, "started", "notice"), (9, "escalated", "concern"), (19, "ended", "concern")]
    row = sqlite3.connect(tmp_path / "e.db").execute(
        "SELECT kind, status, started_at, ended_at, severity FROM episodes").fetchall()
    assert row == [("rest_tremor", "closed", 110.0, 136.0, "concern")]
