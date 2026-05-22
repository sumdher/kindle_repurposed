"""
SQLite persistence for time-series data.

DB path: /srv/kindle/data/kindle.db — mount this as a Docker volume so data
survives container restarts and is shared across service containers.

All writes are serialised through a module-level lock; reads open their own
connection so pages can query without blocking the collector jobs.
WAL mode allows concurrent reads + one writer without locking errors.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/srv/kindle/data/kindle.db")

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _conn(path: str = DB_PATH) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    """Create tables if absent; prune rows older than 8 days. Call once on startup."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _write_lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS ups_samples (
                ts              INTEGER NOT NULL PRIMARY KEY,
                load_pct        INTEGER NOT NULL,
                watts           REAL    NOT NULL,
                input_voltage   REAL    NOT NULL,
                battery_charge  INTEGER NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ups_ts ON ups_samples(ts)")
        # Keep only last 8 days; the UI only ever shows 7d
        cutoff = _epoch_ago(hours=8 * 24)
        c.execute("DELETE FROM ups_samples WHERE ts < ?", (cutoff,))


# ---------------------------------------------------------------------------
# UPS helpers
# ---------------------------------------------------------------------------

def _epoch_ago(hours: int) -> int:
    return int(datetime.now(timezone.utc).timestamp()) - hours * 3600


def ups_insert(
    ts: datetime,
    load_pct: int,
    watts: float,
    input_voltage: float,
    battery_charge: int,
) -> None:
    epoch = int(ts.timestamp())
    with _write_lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO ups_samples VALUES (?,?,?,?,?)",
            (epoch, load_pct, round(watts, 2), round(input_voltage, 2), battery_charge),
        )


def ups_last_ts() -> datetime | None:
    """Timestamp of the most recent sample, or None if the table is empty."""
    with _conn() as c:
        row = c.execute("SELECT MAX(ts) FROM ups_samples").fetchone()
    if row and row[0] is not None:
        return datetime.fromtimestamp(row[0], tz=timezone.utc)
    return None


def ups_query(hours: int, max_points: int = 300) -> list[dict]:
    """
    Return samples for the last `hours` hours, thinned to at most `max_points`.
    Thinning keeps the graph readable without bloating SVG output.
    """
    cutoff = _epoch_ago(hours)
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, load_pct, watts, input_voltage, battery_charge "
            "FROM ups_samples WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ).fetchall()

    if not rows:
        return []

    # Thin evenly if we have more points than we want to render
    step = max(1, len(rows) // max_points)
    thinned = rows[::step]
    # Always include the very last point
    if thinned[-1] != rows[-1]:
        thinned = list(thinned) + [rows[-1]]

    return [
        {
            "ts":             datetime.fromtimestamp(r["ts"], tz=timezone.utc),
            "load_pct":       r["load_pct"],
            "watts":          r["watts"],
            "input_voltage":  r["input_voltage"],
            "battery_charge": r["battery_charge"],
        }
        for r in thinned
    ]


def ups_averages(hours: int) -> tuple[float | None, float | None, int]:
    """Return (avg_load_pct, avg_watts, sample_count) for the last `hours`."""
    cutoff = _epoch_ago(hours)
    with _conn() as c:
        row = c.execute(
            "SELECT AVG(load_pct), AVG(watts), COUNT(*) "
            "FROM ups_samples WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
    if row and row[2]:
        return row[0], row[1], row[2]
    return None, None, 0
