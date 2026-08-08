"""哪些航空公司在飛這條航線。

**這不是「這個價格是誰飛的」,而且那個區別很重要。** 我們排名用的
`v2/prices/month-matrix` 根本沒有航空公司欄位 —— 它只有 `gate`,而 `gate` 是
訂票網站(City.Travel、Biletix、Kupi.com),不是航空公司。航空公司只在
`v1/prices/calendar` 拿得到,而那個端點**不吃日期**:拿 2026-10 和 2026-12 去問,
回來的內容一個位元組都不差。

所以這裡能誠實給出的是**航線層級的事實**:這條線上有這些航空公司在飛。
把它拿去解釋某一天某個價格是誰的班機,就是編造。介面上必須把這句話講清楚,
否則使用者會以為左邊那個價格就是右邊那家航空公司的。

同理,「只看長榮」這種篩選這裡刻意不做:篩得到航線,篩不到價格,
顯示的數字根本不是那家的 —— 那比沒有篩選更會誤導。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

import httpx

from app.config import settings
from app.db import log_fetch, utcnow
from app.pricing.cached import BASE_URL

SOURCE = "travelpayouts-airlines"
# 一條航線上有誰飛,幾個月才變一次 —— 沒有理由跟價格一樣六小時就過期。
TTL_DAYS = 7


@dataclass(frozen=True)
class Airline:
    code: str
    name: str


def _is_fresh(conn: sqlite3.Connection, origin: str, destination: str) -> bool:
    row = conn.execute(
        """
        SELECT MAX(fetched_at) AS last FROM route_airlines
        WHERE origin = ? AND destination = ?
        """,
        (origin, destination),
    ).fetchone()
    if not row or not row["last"]:
        return False
    return datetime.fromisoformat(row["last"]) > utcnow() - timedelta(days=TTL_DAYS)


def refresh_route(
    conn: sqlite3.Connection,
    origin: str,
    destination: str,
    *,
    token: str,
    client: httpx.Client | None = None,
) -> list[str]:
    """抓一條航線上的航空公司代碼並存起來。回傳這次拿到的代碼。"""
    owns_client = client is None
    client = client or httpx.Client(timeout=settings.request_timeout_s)
    started = time.perf_counter()
    try:
        response = client.get(
            f"{BASE_URL}/v1/prices/calendar",
            params={
                "origin": origin,
                "destination": destination,
                "calendar_type": "departure_date",
                "currency": settings.default_currency.lower(),
            },
            headers={"X-Access-Token": token, "Accept-Encoding": "gzip, deflate"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — 記錄下來,不吞掉
        log_fetch(
            conn,
            source=SOURCE,
            endpoint="calendar",
            params={"origin": origin, "destination": destination},
            status_code=getattr(getattr(exc, "response", None), "status_code", None),
            row_count=0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
        conn.commit()
        raise
    finally:
        if owns_client:
            client.close()

    data = payload.get("data") or {}
    codes = sorted({
        entry.get("airline")
        for entry in data.values()
        if isinstance(entry, dict) and entry.get("airline")
    })

    now = utcnow().isoformat()
    conn.execute(
        "DELETE FROM route_airlines WHERE origin = ? AND destination = ?",
        (origin, destination),
    )
    conn.executemany(
        "INSERT INTO route_airlines (origin, destination, airline, fetched_at) VALUES (?,?,?,?)",
        [(origin, destination, code, now) for code in codes],
    )
    log_fetch(
        conn,
        source=SOURCE,
        endpoint="calendar",
        params={"origin": origin, "destination": destination},
        status_code=response.status_code,
        row_count=len(codes),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    conn.commit()
    return codes


def for_routes(
    conn: sqlite3.Connection,
    pairs: Sequence[tuple[str, str]],
    *,
    token: str | None = None,
) -> dict[str, list[Airline]]:
    """一批航線各自有誰飛,鍵是 "TPE-NRT" 這種字串。

    只抓過期的。抓不到就留空 —— 這是加值資訊,不該讓一條航線的失敗擋住整批。
    """
    result: dict[str, list[Airline]] = {}
    client = httpx.Client(timeout=settings.request_timeout_s) if token else None
    try:
        for origin, destination in pairs:
            if token and not _is_fresh(conn, origin, destination):
                try:
                    refresh_route(conn, origin, destination, token=token, client=client)
                except Exception:  # noqa: BLE001, S110 — 已記錄,單線失敗不影響其他
                    pass
            result[f"{origin}-{destination}"] = stored(conn, origin, destination)
    finally:
        if client is not None:
            client.close()
    return result


def stored(conn: sqlite3.Connection, origin: str, destination: str) -> list[Airline]:
    rows = conn.execute(
        """
        SELECT r.airline AS code, COALESCE(a.name, r.airline) AS name
        FROM route_airlines r
        LEFT JOIN airlines a ON a.code = r.airline
        WHERE r.origin = ? AND r.destination = ?
        ORDER BY name
        """,
        (origin, destination),
    ).fetchall()
    return [Airline(code=row["code"], name=row["name"]) for row in rows]
