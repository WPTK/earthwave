"""SQLite history: quakes (with `seen_at`) and a per-day record of tsunami alerts, written by one task.

Alerts are stored for display only (counts on the history screen). Nothing ever loads them back into the Store, so
a restart cannot resurrect an alert, live or expired: the Store learns about alerts from the feeds alone.
"""

import asyncio
import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import fields
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path

import platformdirs

from earthwave.clock import Clock
from earthwave.models import Earthquake, TsunamiAlert

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS quakes (
    id TEXT PRIMARY KEY,
    time TEXT NOT NULL,
    data TEXT NOT NULL,
    seen_at TEXT
);
CREATE INDEX IF NOT EXISTS quakes_time ON quakes (time);
CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    level TEXT NOT NULL,
    headline TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_at ON alerts (at);
"""
SCHEMA_VERSION = 1  # PRAGMA user_version; bump with a migration when a table changes

_DATETIME_FIELDS = {f.name for f in fields(Earthquake) if "datetime" in str(f.type)}


def default_path() -> Path:
    return Path(platformdirs.user_data_dir("earthwave", appauthor=False, roaming=False)) / "history.sqlite"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def encode_quake(quake: Earthquake) -> str:
    data = {f.name: getattr(quake, f.name) for f in fields(Earthquake)}
    for name in _DATETIME_FIELDS:
        if data[name] is not None:
            data[name] = _iso(data[name])
    return json.dumps(data, separators=(",", ":"))


def decode_quake(text: str) -> Earthquake:
    """Tolerant of other versions: unknown keys are ignored and missing optional ones take the model's defaults.
    Raises KeyError or ValueError when a required field is missing or malformed."""
    data = json.loads(text)
    known = {f.name for f in fields(Earthquake)}
    kwargs = {k: v for k, v in data.items() if k in known}
    for name in _DATETIME_FIELDS & kwargs.keys():
        if kwargs[name] is not None:
            kwargs[name] = datetime.fromisoformat(kwargs[name])
    if "also_ids" in kwargs:
        kwargs["also_ids"] = tuple(kwargs["also_ids"])
    return Earthquake(**kwargs)


def day_bounds(day: date, zone: tzinfo | None = None) -> tuple[datetime, datetime]:
    """[start, end) of a calendar day as UTC instants, midnight to midnight in `zone` (None: this machine's zone)."""
    if zone is None:
        start = datetime.combine(day, time.min).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time.min).astimezone(UTC)
    else:
        start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return start, end


class History:
    """One writer task owns every write; reads open their own short connection (WAL lets them run alongside)."""

    def __init__(self, path: Path, retain_days: int, clock: Clock):
        self.path = path
        self._retain = timedelta(days=retain_days)
        self._clock = clock
        self._queue: asyncio.Queue[Callable[[sqlite3.Connection], None] | None] = asyncio.Queue()
        self._writer: asyncio.Task | None = None
        self._db: sqlite3.Connection | None = None

    # --- lifecycle ----------------------------------------------------------------------------------------------

    def open(self) -> None:
        """Create the file and schema and trim old rows. Raises sqlite3.Error or OSError."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._trim(db)
            db.commit()
        except Exception:
            db.close()
            raise
        self._db = db

    def start(self) -> None:
        if self._writer is None:
            self._writer = asyncio.get_running_loop().create_task(self._write_loop())

    def close_now(self) -> None:
        """Close the connection without writing (the startup load failed and the history is being dropped)."""
        if self._db is not None:
            self._db.close()
            self._db = None

    async def close(self) -> None:
        """Finish queued writes, then close the connection."""
        if self._writer is not None:
            self._queue.put_nowait(None)
            await self._writer
            self._writer = None
        self.close_now()

    async def _write_loop(self) -> None:
        while (job := await self._queue.get()) is not None:
            try:
                await asyncio.to_thread(self._apply, job)
            except Exception:  # a failed write loses that batch, never the app; the feeds will send it again
                log.exception("history write failed")

    def _apply(self, job: Callable[[sqlite3.Connection], None]) -> None:
        assert self._db is not None
        try:
            job(self._db)
            self._trim(self._db)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def _trim(self, db: sqlite3.Connection) -> None:
        cutoff = _iso(self._clock.now() - self._retain)
        db.execute("DELETE FROM quakes WHERE time < ?", (cutoff,))
        db.execute("DELETE FROM alerts WHERE at < ?", (cutoff,))

    # --- writes -------------------------------------------------------------------------------------------------

    def record(
        self,
        quakes: Iterable[Earthquake],
        unseen_ids: set[str],
        alerts: Iterable[TsunamiAlert],
        fetched_at: datetime,
        replaced: Iterable[str] = (),
    ) -> None:
        """Queue a merged batch. Quakes in `unseen_ids` (announced live, or just made major) are saved unseen,
        even if already stored; the rest start seen. Encoding happens in the writer job, off the event loop."""
        quakes, alerts, replaced = tuple(quakes), tuple(alerts), tuple(replaced)
        if not quakes and not alerts:
            return
        stamp = _iso(self._clock.now())

        def write(db: sqlite3.Connection) -> None:
            db.executemany("DELETE FROM quakes WHERE id = ?", [(i,) for i in replaced])  # ids USGS replaced
            db.executemany(
                "INSERT INTO quakes (id, time, data, seen_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET time = excluded.time, data = excluded.data, "
                "seen_at = CASE WHEN excluded.seen_at IS NULL THEN NULL ELSE seen_at END "
                "WHERE data != excluded.data OR excluded.seen_at IS NULL",
                [(q.id, _iso(q.time), encode_quake(q), None if q.id in unseen_ids else stamp) for q in quakes],
            )
            db.executemany(
                "INSERT INTO alerts (id, at, level, headline) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET level = excluded.level, headline = excluded.headline",
                [(a.id, _iso(a.issued or a.updated or fetched_at), a.level, a.headline) for a in alerts],
            )

        self._queue.put_nowait(write)

    def mark_seen(self, ids: set[str]) -> None:
        """Persist acknowledgements. Idempotent: only rows still unseen change."""
        if not ids:
            return
        stamp = _iso(self._clock.now())

        def write(db: sqlite3.Connection) -> None:
            db.executemany("UPDATE quakes SET seen_at = ? WHERE id = ? AND seen_at IS NULL", [(stamp, i) for i in ids])

        self._queue.put_nowait(write)

    # --- reads (raise sqlite3.Error, KeyError or ValueError; callers show the failure) --------------------------

    def _read(self, sql: str, params: tuple) -> list[tuple]:
        db = sqlite3.connect(self.path, timeout=5.0)
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def day(self, start: datetime, end: datetime) -> tuple[list[Earthquake], Counter[str]]:
        """Quakes with start <= time < end, newest first, and the day's alerts counted by level. Safe off-thread."""
        bounds = (_iso(start), _iso(end))
        rows = self._read("SELECT data FROM quakes WHERE time >= ? AND time < ? ORDER BY time DESC", bounds)
        alerts = self._read("SELECT level FROM alerts WHERE at >= ? AND at < ?", bounds)
        return [decode_quake(r[0]) for r in rows], Counter(r[0] for r in alerts)

    def load_since(self, since: datetime) -> tuple[list[Earthquake], set[str]]:
        """Quakes at or after `since` (to refill the Store) and the ids among them never acknowledged."""
        rows = self._read("SELECT data, seen_at IS NULL FROM quakes WHERE time >= ?", (_iso(since),))
        quakes = [(decode_quake(data), unseen) for data, unseen in rows]
        return [q for q, _ in quakes], {q.id for q, unseen in quakes if unseen}
