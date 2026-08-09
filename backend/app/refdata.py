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
    # 航空公司代碼對名稱。價格資料只給代碼(TW、MM、IT),沒有這張表就只能
    # 在畫面上印兩個字母。
    "airlines": f"{BASE_URL}/airlines.json",
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
                "airlines": _write_airlines,
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


def _write_airlines(conn: sqlite3.Connection, payload: Iterable[dict[str, Any]]) -> int:
    rows = []
    for entry in payload:
        code = entry.get("code")
        if not code:
            continue
        # 上游實測只附英文,所以中文名跟國家、城市一樣自己維護;沒收錄的
        # 就留英文原名,不硬翻 —— 一個亂翻的公司名比英文原名更難認。
        name_en = _english(entry) or code
        rows.append((code, zh_names.airline_name(code, name_en), name_en))
    conn.executemany(
        """
        INSERT INTO airlines (code, name, name_en) VALUES (?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            name = excluded.name,
            name_en = excluded.name_en
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
        country["translated"] = country["code"].upper() in zh_names.COUNTRY_ZH
    # 常用的排最前面,再來是**有中文名的**,最後才是只有英文名的。
    # 只照 name_zh 排會出事:沒收錄中文名的國家,name_zh 存的是英文字串,
    # 於是 147 個英文國名(Abkhazia…Åland Islands)會夾在中文國名中間,
    # 把挪威、瑞典、巴西、墨西哥擠到那 147 個之後 —— 實測就是這樣。
    countries.sort(
        key=lambda c: (c["popularity"], not c["translated"], c["name_zh"])
    )
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
    # Places a traveller from Taiwan would actually pick, in the order they'd
    # think of them; everything else falls in behind, multi-airport cities
    # first because that is where airport substitution saves money.
    cities.sort(
        key=lambda c: (
            zh_names.city_rank(c.code),
            -len(c.airports),
            c.name,
        )
    )
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


def search_cities(
    conn: sqlite3.Connection, query: str, *, limit: int = 20
) -> list[City]:
    """找地方,回傳城市(連它的機場)。`query` 留空就給熱門清單。

    這支的存在理由是**選單原本是死路**:舊版的挑法是「選國家 → 從前 12 個城市裡點」,
    而日本有 72 個可飛城市 —— 岡山、函館、石垣、靜岡全部點不到,而且畫面上寫著
    「日本 (77)」,看起來只是排在後面,實際上根本沒有那個按鈕。美國更誇張,
    525 個城市裡只列得出 12 個。

    比對五種寫法,因為使用者手上有哪一種完全看心情:中文名(福岡)、英文名
    (Fukuoka)、城市代碼(FUK)、機場代碼(HND)、國家(日本)。參考資料只有 253 國裡的 90 國、
    3,522 個可飛城市裡的 138 個有中文名,所以**英文與代碼一定要能搜** ——
    不然沒有中文名的地方一樣是死路,只是死得比較隱晦。
    """
    q = query.strip()
    like = f"%{q}%"
    upper = q.upper()

    select = """
        SELECT a.code       AS airport_code,
               a.name_zh    AS airport_name,
               a.city_code  AS city_code,
               a.country_code,
               COALESCE(ct.name_zh, a.name_zh) AS city_name
        FROM airports a
        LEFT JOIN cities ct    ON ct.code = a.city_code
        LEFT JOIN countries co ON co.code = a.country_code
        WHERE a.flightable = 1 AND a.iata_type = 'airport'
    """

    if q:
        rows = conn.execute(
            select + """
              AND (
                    a.code = ?
                 OR a.city_code = ?
                 OR a.name_zh LIKE ?
                 OR a.name_en LIKE ?
                 OR COALESCE(ct.name_zh, '') LIKE ?
                 OR COALESCE(ct.name_en, '') LIKE ?
                 -- 打國家名也要能用。「日本」比「福岡」更容易是使用者腦中的第一個詞,
                 -- 尤其是還沒決定去哪一城的時候。
                 OR co.code = ?
                 OR COALESCE(co.name_zh, '') LIKE ?
                 OR COALESCE(co.name_en, '') LIKE ?
              )
            ORDER BY city_name, a.code
            """,
            (upper, upper, like, like, like, like, upper, like, like),
        ).fetchall()
    else:
        # 還沒打字:給一份**點得到的**清單。純搜尋框對「不知道要打什麼」的人
        # 一樣是死路 —— 只是死在一個空白輸入框前面,而不是死在一份截斷的清單裡。
        # 用 CITY_ZH 的排列順序當熱門度,那份表本來就是照台灣旅客會想到的順序寫的。
        placeholders = ",".join("?" * len(zh_names.CITY_ZH))
        rows = conn.execute(
            select + f" AND a.city_code IN ({placeholders}) ORDER BY city_name, a.code",
            tuple(zh_names.CITY_ZH),
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

    def rank(city: City) -> tuple:
        codes = {a.code for a in city.airports} | {city.code}
        return (
            0 if upper in codes else 1,          # 打代碼的人要的就是那一個
            zh_names.city_rank(city.code),        # 台灣旅客真的會去的地方在前
            -len(city.airports),                  # 多機場的城市是省錢的來源
            city.name,
        )

    cities.sort(key=rank)
    return cities[:limit]


def country_names(conn: sqlite3.Connection, codes: Iterable[str]) -> dict[str, str]:
    """國家代碼 → 顯示名稱。搜尋結果要標國家,否則「Santiago」有五個。"""
    wanted = sorted({c for c in codes if c})
    if not wanted:
        return {}
    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"SELECT code, COALESCE(name_zh, name_en) AS name FROM countries "
        f"WHERE code IN ({placeholders})",
        wanted,
    ).fetchall()
    return {row["code"]: row["name"] for row in rows}


def siblings_by_airport(
    conn: sqlite3.Connection, codes: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """每個機場所在城市的所有可飛機場,包含自己。

    同城替代是「那天沒有價」最好的一條出路,因為連日期都不用改 —— 大阪的
    ITM→TPE 在 2026-12 只有 3 天有價,同城的 KIX→TPE 有 28 天。查一次全部拿完,
    因為呼叫端手上是一整批航段,一段一問就是 N 次查詢換一個常數大小的答案。
    """
    wanted = sorted({code.strip().upper() for code in codes if code and code.strip()})
    if not wanted:
        return {}
    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"""
        SELECT mine.code AS code, peer.code AS peer
        FROM airports mine
        JOIN airports peer ON peer.city_code = mine.city_code
        WHERE mine.code IN ({placeholders})
          AND peer.flightable = 1 AND peer.iata_type = 'airport'
          AND mine.city_code IS NOT NULL
        ORDER BY peer.code
        """,
        wanted,
    ).fetchall()

    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["code"], []).append(row["peer"])
    return {code: tuple(peers) for code, peers in grouped.items()}


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
