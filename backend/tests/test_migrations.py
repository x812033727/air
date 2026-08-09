"""既有資料庫升級。

`CREATE TABLE IF NOT EXISTS` 對已經存在的表什麼都不做,所以新欄位對每一個
「跑過至少一次」的部署都是隱形的 —— 也就是全部的部署。這裡測的是那條路徑,
因為測試用的資料庫都是從最新 schema 全新建起來的,永遠走不到它。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db import SCHEMA, _migrate

# gate 欄位加進來之前的 price_cache。逐字保留,因為這正是伺服器上那張表。
OLD_PRICE_CACHE = """
CREATE TABLE price_cache (
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
"""


@pytest.fixture
def old_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(OLD_PRICE_CACHE)
    yield conn
    conn.close()


def _insert(conn, destination, airline):
    conn.execute(
        """INSERT INTO price_cache
           (origin, destination, depart_date, currency, price, transfers, airline,
            flight_number, found_at, fetched_at, expires_at, source)
           VALUES ('TPE',?,'2026-10-05','TWD',6800,0,?,NULL,NULL,'x','y','test')""",
        (destination, airline),
    )


def test_the_gate_column_is_added_to_an_existing_table(old_conn):
    _migrate(old_conn)
    columns = {row["name"] for row in old_conn.execute("PRAGMA table_info(price_cache)")}
    assert "gate" in columns


def test_booking_sites_are_moved_out_of_the_airline_column(old_conn):
    """伺服器上實際存著的值:Kupi.com、Aviakassa、City.Travel —— 全是訂票網站。
    留在 `airline` 裡,航空公司篩選就會把「Kupi.com」端出來當一家航空公司。"""
    _insert(old_conn, "NRT", "Kupi.com")
    _insert(old_conn, "KIX", "All Nippon Airways")
    _migrate(old_conn)

    rows = {r["destination"]: (r["airline"], r["gate"]) for r in
            old_conn.execute("SELECT destination, airline, gate FROM price_cache")}
    assert rows["NRT"] == (None, "Kupi.com")
    # Aviasales 把航空公司自家的訂票網站也叫 gate。名字像航空公司不代表它是。
    assert rows["KIX"] == (None, "All Nippon Airways")


def test_a_real_iata_code_is_left_where_it_is(old_conn):
    """calendar 端點給的是真的航空公司代碼,而且不給 gate。一律搬動會把真資料毀掉。"""
    _insert(old_conn, "HND", "BR")
    _migrate(old_conn)
    row = old_conn.execute("SELECT airline, gate FROM price_cache").fetchone()
    assert (row["airline"], row["gate"]) == ("BR", None)


def test_running_twice_changes_nothing(old_conn):
    """部署是冪等的,遷移也必須是 —— 每次啟動都會呼叫 init_db。"""
    _insert(old_conn, "NRT", "Kupi.com")
    _migrate(old_conn)
    _migrate(old_conn)
    row = old_conn.execute("SELECT airline, gate FROM price_cache").fetchone()
    assert (row["airline"], row["gate"]) == (None, "Kupi.com")


def test_a_fresh_database_is_already_up_to_date():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(price_cache)")}
    assert "gate" in columns
    conn.close()
