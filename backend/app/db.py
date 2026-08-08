"""SQLite storage: reference data, the price cache, and the fetch log.

SQLite is deliberate. This app stores a few tens of thousands of static
reference rows plus a short-lived price cache — a Postgres container would be
pure operational cost for no benefit.

The ``fetch_log`` table is not incidental. A flight data source fails far more
often by returning ``200 OK`` with an empty body than by erroring, so every
outbound call records the number of rows it actually produced. ``/api/health``
reads this table, which is why it can tell "working" apart from "answering".
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS countries (
    code        TEXT PRIMARY KEY,
    name_en     TEXT NOT NULL,
    name_zh     TEXT,
    currency    TEXT
);

CREATE TABLE IF NOT EXISTS cities (
    code            TEXT PRIMARY KEY,
    country_code    TEXT NOT NULL,
    name_en         TEXT NOT NULL,
    name_zh         TEXT,
    lat             REAL,
    lon             REAL,
    time_zone       TEXT,
    flightable      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cities_country ON cities(country_code);

-- `iata_type` earns its column: Travelpayouts marks 386 railway stations, bus
-- terminals and harbours as `flightable: true` (LMJ "Tokyo Bus Station" sits in
-- city TYO next to NRT and HND). Filtering on flightable alone puts a bus stop
-- in the 東京 airport picker.
CREATE TABLE IF NOT EXISTS airports (
    code            TEXT PRIMARY KEY,
    city_code       TEXT,
    country_code    TEXT NOT NULL,
    name_en         TEXT NOT NULL,
    name_zh         TEXT,
    lat             REAL,
    lon             REAL,
    time_zone       TEXT,
    iata_type       TEXT,
    flightable      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_airports_city ON airports(city_code);
CREATE INDEX IF NOT EXISTS idx_airports_country ON airports(country_code);

-- One row per (route, departure day). Refreshed wholesale per route-month.
CREATE TABLE IF NOT EXISTS price_cache (
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    depart_date     TEXT NOT NULL,
    currency        TEXT NOT NULL,
    price           REAL,
    transfers       INTEGER,
    airline         TEXT,
    flight_number   TEXT,
    found_at        TEXT,
    fetched_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    source          TEXT NOT NULL,
    PRIMARY KEY (origin, destination, depart_date, currency)
);

-- Records that a route-month was fetched even when it came back with nothing,
-- so "no cheap flights" can be told apart from "nobody has ever searched this".
CREATE TABLE IF NOT EXISTS route_fetch (
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    month           TEXT NOT NULL,
    currency        TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    fetched_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    PRIMARY KEY (origin, destination, month, currency)
);

-- 站台設定(目前只有查價金鑰)。金鑰原本刻意只放在瀏覽器,因為公開站台上的
-- 伺服器端金鑰等於誰都讀得走;站台加上密碼保護之後那個理由消失了,而「在網頁上
-- 存好卻只有那台瀏覽器算數」對使用者來說就只是壞掉。
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    params      TEXT,
    status_code INTEGER,
    row_count   INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    error       TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_source ON fetch_log(source, created_at DESC);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or settings.db_path
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path | None = None) -> None:
    with closing_conn(path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def closing_conn(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def log_fetch(
    conn: sqlite3.Connection,
    *,
    source: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
    status_code: int | None = None,
    row_count: int = 0,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO fetch_log
            (source, endpoint, params, status_code, row_count, duration_ms, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            endpoint,
            json.dumps(params, ensure_ascii=False) if params else None,
            status_code,
            row_count,
            duration_ms,
            error,
            utcnow().isoformat(),
        ),
    )


def source_health(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per-source: when it last answered, and whether it answered with anything.

    ``last_nonempty_at`` is the column that matters. A source whose
    ``last_success_at`` keeps advancing while ``last_nonempty_at`` stays frozen
    is broken, however green its status codes look.
    """
    rows = conn.execute(
        """
        SELECT source,
               MAX(created_at)                                        AS last_call_at,
               MAX(CASE WHEN error IS NULL THEN created_at END)       AS last_success_at,
               MAX(CASE WHEN row_count > 0 THEN created_at END)       AS last_nonempty_at,
               COUNT(*)                                               AS calls,
               SUM(CASE WHEN row_count = 0 THEN 1 ELSE 0 END)         AS empty_calls,
               SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END)     AS failed_calls
        FROM fetch_log
        WHERE created_at >= datetime('now', '-24 hours')
        GROUP BY source
        ORDER BY source
        """
    ).fetchall()
    return [dict(row) for row in rows]
