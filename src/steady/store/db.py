"""SQLite access: opening, schema, and the single batched writer.

All writes go through one Writer task (one transaction per flush), so the screening loop
never waits on the disk. Readers (the API) open their own short-lived connections; WAL mode
lets them read while the writer writes.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("steady.store")
SCHEMA = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "2"
MIGRATIONS = [("windows", "peak_conc", "REAL")]


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init(path: Path) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        for table, col, kind in MIGRATIONS:              # columns added after the first release
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        conn.commit()
    finally:
        conn.close()


# Keep the newest N assessments; of the older ones keep only the positive ones (flags != 0).
PRUNE_WINDOWS_SQL = ("DELETE FROM windows WHERE flags = 0 AND id NOT IN "
                     "(SELECT id FROM windows ORDER BY id DESC LIMIT ?)")


def insert_sql(table: str, row: dict[str, Any]) -> tuple[str, tuple]:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    return f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values())


class Writer:
    """Queue of SQL statements, flushed in one transaction every `interval` seconds."""

    def __init__(self, path: Path, interval: float = 1.0) -> None:
        self.path = path
        self.interval = interval
        self._queue: asyncio.Queue = asyncio.Queue()
        self._conn: sqlite3.Connection | None = None
        self._task: asyncio.Task | None = None
        self.written = 0

    async def start(self) -> None:
        init(self.path)
        self._conn = connect(self.path)
        self._task = asyncio.create_task(self._run(), name="db-writer")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        self._queue.put_nowait((sql, tuple(params), None))

    def insert(self, table: str, row: dict[str, Any]) -> None:
        self.execute(*insert_sql(table, row))

    async def insert_returning_id(self, table: str, row: dict[str, Any]) -> int:
        """Insert now (in order with queued writes) and get the new row id."""
        fut = asyncio.get_running_loop().create_future()
        sql, params = insert_sql(table, row)
        self._queue.put_nowait((sql, params, fut))
        return await fut

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.flush()

    async def flush(self) -> None:
        batch = []
        while not self._queue.empty():
            batch.append(self._queue.get_nowait())
        if batch:
            await asyncio.to_thread(self._write, batch)

    def _write(self, batch: list) -> None:
        assert self._conn is not None
        t0 = time.perf_counter()
        results = []
        try:
            with self._conn:
                for sql, params, fut in batch:
                    cur = self._conn.execute(sql, params)
                    if fut is not None:
                        results.append((fut, cur.lastrowid))
        except sqlite3.Error as e:
            log.error("database write failed (%d statements lost): %s", len(batch), e)
            for _, _, fut in batch:
                if fut is not None:
                    fut.get_loop().call_soon_threadsafe(fut.set_exception, e)
            return
        for fut, rowid in results:
            fut.get_loop().call_soon_threadsafe(fut.set_result, rowid)
        self.written += len(batch)
        ms = (time.perf_counter() - t0) * 1000
        if ms > 200:
            log.warning("slow database flush: %d statements in %.0f ms", len(batch), ms)

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.flush()
        if self._conn:
            self._conn.close()
