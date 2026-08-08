"""國家 → 城市 → 機場 的參考資料。

資料來自 Travelpayouts 的三個公開檔案(`/data/countries.json`、`cities.json`、
`airports.json`),**不需要 token** —— 這是實測過的,所以參考資料層在還沒拿到
API 金鑰之前就能完整運作。

這一層不只是給下拉選單用的。`airports.city_code` 就是「機場替代」的定義來源:
東京 = NRT + HND、大阪 = KIX + ITM。使用者選一個城市,就自動把該城所有機場
納入候選 —— 這正是多開口能省錢的第一個來源,所以這個分組本身就是產品邏輯。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from app import zh_names
from app.config import settings
from app.db import log_fetch, utcnow

BASE_URL = "https://api.travelpayouts.com/data"
SOURCE = "travelpayouts-refdata"

FILES = {
    "countries": f"{BASE_URL}/countries.json",
    "cities": f"{BASE_URL}/cities.json",
    "airports": f"{BASE_URL}/airports.json",
}


@dataclass(frozen=True)
class Airport:
    code: str
    name: str
    city_code: str
    city_name: str
    country_code: str


@dataclass(frozen=True)
class City:
    code: str
    name: str
    country_code: str
    airports: tuple[Airport, ...]


def refresh(conn: sqlite3.Connection, *, client: httpx.Client | None = None) -> dict[str, int]:
    """Download all three reference files and replace what we have.

    Returns the row count written per file so the caller — and the health
    endpoint — can see that the refresh did work rather than merely succeed.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=settings.request_timeout_s)
    written: dict[str, int] = {}
    try:
        for name, url in FILES.items():
            payload = _fetch(conn, client, name, url)
            writer = {
                "countries": _write_countries,
                "cities": _write_cities,
                "airports": _write_airports,
            }[name]
            written[name] = writer(conn, payload)
        conn.commit()
    finally:
        if owns_client:
            client.close()
    return written


def _fetch(conn: sqlite3.Connection, client: httpx.Client, name: str, url: str) -> list[dict[str, Any]]:
    started = time.perf_counter()
    try:
        response = client.get(url, headers={"Accept-Encoding": "gzip, deflate"})
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — the log entry is the point
        log_fetch(
            conn,
            source=SOURCE,
            endpoint=name,
            status_code=getattr(getattr(exc, "response", None), "status_code", None),
            row_count=0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
        conn.commit()
        raise
    log_fetch(
        conn,
        source=SOURCE,
        endpoint=name,
        status_code=response.status_code,
        row_count=len(payload),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return payload


def _english(entry: dict[str, Any]) -> str:
    translations = entry.get("name_translations") or {}
    return translations.get("en") or entry.get("name") or entry.get("code", "")


def _write_countries(conn: sqlite3.Connection, payload: Iterable[dict[str, Any]]) -> int:
    rows = []
    for entry in payload:
        code = entry.get("code")
        if not code:
            continue
        name_en = _english(entry)
        rows.append((code, name_en, zh_names.country_name(code, name_en), entry.get("currency")))
    conn.executemany(
        """
        INSERT INTO countries (code, name_en, name_zh, currency) VALUES (?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            name_en = excluded.name_en,
            name_zh = excluded.name_zh,
            currency = excluded.currency
        """,
        rows,
    )
    return len(rows)


def _write_cities(conn: sqlite3.Connection, payload: Iterable[dict[str, Any]]) -> int:
    rows = []
    for entry in payload:
        code = entry.get("code")
        country = entry.get("country_code")
        if not code or not country:
            continue
        name_en = _english(entry)
        coords = entry.get("coordinates") or {}
        rows.append(
            (
                code,
                country,
                name_en,
                zh_names.city_name(code, name_en),
                coords.get("lat"),
                coords.get("lon"),
                entry.get("time_zone"),
                1 if entry.get("has_flightable_airport") else 0,
            )
        )
    conn.executemany(
        """
        INSERT INTO cities (code, country_code, name_en, name_zh, lat, lon, time_zone, flightable)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            country_code = excluded.country_code,
            name_en = excluded.name_en,
            name_zh = excluded.name_zh,
            lat = excluded.lat,
            lon = excluded.lon,
            time_zone = excluded.time_zone,
            flightable = excluded.flightable
        """,
        rows,
    )
    return len(rows)


def _write_airports(conn: sqlite3.Connection, payload: Iterable[dict[str, Any]]) -> int:
    rows = []
    for entry in payload:
        code = entry.get("code")
        country = entry.get("country_code")
        if not code or not country:
            continue
        name_en = _english(entry)
        coords = entry.get("coordinates") or {}
        rows.append(
            (
                code,
                entry.get("city_code"),
                country,
                name_en,
                zh_names.airport_name(code, name_en),
                coords.get("lat"),
                coords.get("lon"),
                entry.get("time_zone"),
                entry.get("iata_type"),
                1 if entry.get("flightable") else 0,
            )
        )
    conn.executemany(
        """
        INSERT INTO airports
            (code, city_code, country_code, name_en, name_zh, lat, lon, time_zone,
             iata_type, flightable)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            city_code = excluded.city_code,
            country_code = excluded.country_code,
            name_en = excluded.name_en,
            name_zh = excluded.name_zh,
            lat = excluded.lat,
            lon = excluded.lon,
            time_zone = excluded.time_zone,
            iata_type = excluded.iata_type,
            flightable = excluded.flightable
        """,
        rows,
    )
    return len(rows)


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------

def list_countries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Countries you can actually fly to, with how many airports each offers."""
    rows = conn.execute(
        """
        SELECT c.code, c.name_en, c.name_zh, COUNT(a.code) AS airport_count
        FROM countries c
        JOIN airports a ON a.country_code = c.code AND a.flightable = 1
                              AND a.iata_type = 'airport'
        GROUP BY c.code
        HAVING airport_count > 0
        ORDER BY c.name_zh
        """
    ).fetchall()
    countries = [dict(row) for row in rows]
    for country in countries:
        country["popularity"] = zh_names.popularity(country["code"])
    countries.sort(key=lambda c: (c["popularity"], c["name_zh"]))
    return countries


def cities_in_country(conn: sqlite3.Connection, country_code: str) -> list[City]:
    """The city → airport tree for one country.

    Cities come back ordered by airport count then name, so multi-airport
    cities — the ones where substitution actually saves money — sit at the top.
    """
    rows = conn.execute(
        """
        SELECT a.code       AS airport_code,
               a.name_zh    AS airport_name,
               a.city_code  AS city_code,
               a.country_code,
               COALESCE(ct.name_zh, a.name_zh) AS city_name
        FROM airports a
        LEFT JOIN cities ct ON ct.code = a.city_code
        WHERE a.country_code = ? AND a.flightable = 1 AND a.iata_type = 'airport'
        ORDER BY city_name, a.code
        """,
        (country_code.upper(),),
    ).fetchall()

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["city_code"] or row["airport_code"], []).append(row)

    cities = [
        City(
            code=city_code,
            name=members[0]["city_name"],
            country_code=members[0]["country_code"],
            airports=tuple(
                Airport(
                    code=m["airport_code"],
                    name=m["airport_name"],
                    city_code=city_code,
                    city_name=members[0]["city_name"],
                    country_code=m["country_code"],
                )
                for m in members
            ),
        )
        for city_code, members in grouped.items()
    ]
    cities.sort(key=lambda c: (-len(c.airports), c.name))
    return cities


def expand_airports(conn: sqlite3.Connection, codes: Iterable[str]) -> list[Airport]:
    """Resolve a mixed list of city and airport codes into concrete airports.

    The UI lets you pick 「東京」 (a city) or 「成田」 (a single airport). Both
    arrive here as bare IATA codes and both have to come out as a flat airport
    list, because that is what the combination generator consumes.
    """
    wanted = [code.strip().upper() for code in codes if code and code.strip()]
    if not wanted:
        return []
    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"""
        SELECT a.code       AS airport_code,
               a.name_zh    AS airport_name,
               a.city_code,
               a.country_code,
               COALESCE(ct.name_zh, a.name_zh) AS city_name
        FROM airports a
        LEFT JOIN cities ct ON ct.code = a.city_code
        WHERE a.flightable = 1 AND a.iata_type = 'airport'
          AND (a.code IN ({placeholders}) OR a.city_code IN ({placeholders}))
        ORDER BY a.code
        """,
        (*wanted, *wanted),
    ).fetchall()
    return [
        Airport(
            code=row["airport_code"],
            name=row["airport_name"],
            city_code=row["city_code"] or row["airport_code"],
            city_name=row["city_name"],
            country_code=row["country_code"],
        )
        for row in rows
    ]


def is_stale(conn: sqlite3.Connection, max_age_days: int = 7) -> bool:
    row = conn.execute(
        """
        SELECT MAX(created_at) AS last
        FROM fetch_log
        WHERE source = ? AND row_count > 0
        """,
        (SOURCE,),
    ).fetchone()
    if not row or not row["last"]:
        return True
    from datetime import datetime

    last = datetime.fromisoformat(row["last"])
    return (utcnow() - last).days >= max_age_days
