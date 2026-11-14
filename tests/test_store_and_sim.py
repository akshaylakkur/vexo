import asyncio
import sqlite3



from steady import config as config_mod
from steady.store.db import Writer


def test_writer_batches_and_returns_ids(tmp_path):
    async def go():
        w = Writer(tmp_path / "t.db", interval=0.05)
        await w.start()
        sid = await w.insert_returning_id("band_sessions", {"started_at": 1.0, "band_no": 3})
        for i in range(10):
            w.insert("band_events", {"session_id": sid, "t": float(i), "kind": "tap"})
        await w.close()
        return sid

    sid = asyncio.run(go())
    conn = sqlite3.connect(tmp_path / "t.db")
    assert sid == 1
    assert conn.execute("SELECT count(*) FROM band_events WHERE session_id=1").fetchone()[0] == 10


def test_end_to_end_on_pretend_band(tmp_path):
    """steadyd's band + screening + logging, for ~9 s on the pretend band."""
    from steady.app import Steady

    cfg = config_mod.load(overrides={"band.transport": "sim", "storage.db_path": str(tmp_path / "s.db")})
    seen = []

    def fake_detector(feats, ctx, window):          # "detects" every time it is asked
        seen.append(ctx)
        return 1

    async def go():
        s = Steady(cfg)
        s.screener.detectors.append(fake_detector)
        await s.writer.start()
        band_task = asyncio.create_task(s.band.run())
        screen_task = asyncio.create_task(s.screener.run())
        await asyncio.sleep(9.5)
        screen_task.cancel()
        s.screener.close()
        await s.band.stop()
        await asyncio.wait_for(band_task, 5)
        await s.writer.close()
        return s

    s = asyncio.run(go())
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.row_factory = sqlite3.Row
    windows = conn.execute("SELECT * FROM windows ORDER BY t_end").fetchall()
    assert len(windows) == s.screener.ticks >= 4               # every assessment is saved, one every 2 s
    assert all(abs(b["t_end"] - a["t_end"] - 2.0) < 0.3 for a, b in zip(windows, windows[1:]))
    valid = [w for w in windows if w["valid"]]
    assert valid, "no valid assessment after the first 6 s of data"
    assert all(w["context"] in ("rest", "active", "walking") for w in valid)
    positive = [w for w in windows if w["flags"]]
    assert len(positive) == s.screener.positives
    assert all(w["valid"] for w in positive)                   # a warm-up window can never be positive
    assert conn.execute("SELECT count(*) FROM band_sessions WHERE ended_at IS NOT NULL").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM band_events").fetchone()[0] >= 1   # the pretend band taps at 4 s and 8 s
    assert conn.execute("SELECT count(*) FROM hourly_rollups").fetchone()[0] >= 1
    assert s.ring.lost == 0


def test_prune_keeps_newest_and_all_positives(tmp_path):
    from steady.store import db

    path = tmp_path / "p.db"
    db.init(path)
    conn = db.connect(path)
    with conn:
        for i in range(1, 101):                                  # positives at ids 10, 20, ... 100
            conn.execute("INSERT INTO windows (t_start, t_end, valid, flags) VALUES (?, ?, 1, ?)",
                         (float(i), float(i) + 4, 1 if i % 10 == 0 else 0))
        conn.execute(db.PRUNE_WINDOWS_SQL, (20,))
    ids = [r["id"] for r in conn.execute("SELECT id FROM windows ORDER BY id")]
    assert ids == [10, 20, 30, 40, 50, 60, 70, 80] + list(range(81, 101))
    kept_old = conn.execute("SELECT id, t_end, flags FROM windows WHERE id <= 80").fetchall()
    assert all(r["flags"] == 1 and r["t_end"] == r["id"] + 4 for r in kept_old)   # with their timestamps
