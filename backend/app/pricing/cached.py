"""層一:Travelpayouts(Aviasales)快取價。

這一層的存在理由是**成本結構**:一次呼叫回傳一條航線一整個月的每日最低價,
所以「日期」這個維度的組合爆炸是免費的,只有「機場對」才花呼叫數。台北→日本
兩城開口大約是 30–40 次呼叫,不是幾千次。

必須誠實面對的三件事,都寫進了程式碼:

1. 這是**快取價**,不是可訂價。每個價格都帶著 `found_at` 與 `fetched_at`,
   UI 有義務把時效顯示出來。
2. 冷門航線可能一列都沒有 —— 因為沒人搜過,不是因為沒有便宜票。
   `route_fetch` 表記錄「這條航線這個月抓過、拿到 N 列」,所以查無資料能被
   說成「不知道」而不是「不便宜」。
3. 是不是真的單程價是實測出來的,不是假設的。見 `docs/spike-datasource.md`。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Sequence

import httpx

from app.combos import Combo, FlightLeg
from app.config import settings
from app.db import log_fetch, utcnow

SOURCE = "travelpayouts-prices"
BASE_URL = "https://api.travelpayouts.com"

LegStatus = Literal["ok", "no_data", "not_fetched"]


class MissingToken(RuntimeError):
    """Raised when a price call is attempted without TRAVELPAYOUTS_TOKEN.

    Deliberately loud. A pricing layer that silently returns nothing looks
    exactly like a destination with no cheap flights.
    """


@dataclass(frozen=True)
class PricePoint:
    origin: str
    destination: str
    depart_date: date
    price: float
    currency: str
    transfers: int | None
    airline: str | None
    flight_number: str | None
    found_at: str | None
    fetched_at: datetime
    # 訂票網站,不是航空公司。month-matrix 只給得出這個,所以那個端點的
    # `airline` 一定是 None —— 把 gate 塞進 airline 會讓「Kupi.com」變成一家
    # 航空公司,而航空公司篩選會照單全收。
    gate: str | None = None

    @property
    def age_hours(self) -> float:
        return (utcnow() - self.fetched_at).total_seconds() / 3600


@dataclass(frozen=True)
class LegPricing:
    leg: FlightLeg
    status: LegStatus
    point: PricePoint | None = None

    @property
    def price(self) -> float | None:
        return self.point.price if self.point else None


@dataclass(frozen=True)
class ComboPricing:
    combo: Combo
    legs: tuple[LegPricing, ...]

    @property
    def is_complete(self) -> bool:
        return all(leg.status == "ok" for leg in self.legs)

    @property
    def total(self) -> float | None:
        """None when any leg is unpriced — never a partial sum.

        Summing only the legs we happen to have would make an incomplete
        itinerary look like the cheapest one on the page.
        """
        if not self.is_complete:
            return None
        return sum(leg.price or 0.0 for leg in self.legs)

    @property
    def oldest_fetch(self) -> datetime | None:
        stamps = [leg.point.fetched_at for leg in self.legs if leg.point]
        return min(stamps) if stamps else None

    @property
    def missing_legs(self) -> tuple[FlightLeg, ...]:
        return tuple(leg.leg for leg in self.legs if leg.status != "ok")


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def months_covering(dates: Iterable[date]) -> list[str]:
    """The distinct YYYY-MM buckets a set of departure dates falls into."""
    return sorted({d.strftime("%Y-%m") for d in dates})


@dataclass(frozen=True)
class FetchOutcome:
    """What a warming pass actually achieved, successes and failures alike."""

    counts: dict[tuple[str, str, str], int]
    failures: dict[tuple[str, str, str], str]

    @property
    def rows(self) -> int:
        return sum(self.counts.values())

    @property
    def empty_routes(self) -> list[str]:
        return sorted({f"{o}→{d}" for (o, d, _), n in self.counts.items() if n == 0})

    @property
    def failed_routes(self) -> list[str]:
        return sorted({f"{o}→{d}" for (o, d, _) in self.failures})

    @property
    def unauthorized(self) -> bool:
        """Nothing came back and everything was rejected — the key is the problem.

        Reporting a mistyped token as "these 20 routes didn't come back" sends
        the user hunting for a network fault that isn't there. The converse
        matters just as much: if even one route succeeded the key is clearly
        valid, so a 401 on the rest is something else and must not be blamed
        on the token.
        """
        return (
            bool(self.failures)
            and not self.counts
            and all(
                "401" in message or "403" in message
                for message in self.failures.values()
            )
        )


def ensure_routes(
    conn: sqlite3.Connection,
    pairs: Sequence[tuple[str, str]],
    months: Sequence[str],
    *,
    currency: str | None = None,
    client: httpx.Client | None = None,
    force: bool = False,
    token: str | None = None,
) -> FetchOutcome:
    """Fetch every (route, month) that isn't already cached and fresh.

    Commits after **each** route-month, and keeps going when one fails. Both
    parts matter for a pass that makes 20–40 sequential upstream calls:

    * A single commit at the end means one 429 on the last route throws away
      all the earlier writes, so the retry re-fetches everything and spends
      the quota twice — and the ``fetch_log`` row recording the failure is
      rolled back with it, blinding the health endpoint to the very failure
      mode it exists to catch.
    * Aborting the loop means one bad route denies prices for every other
      route in the search.

    Row counts come back including the zeros, because a caller has to tell
    "we looked and there was nothing" from "we never looked".

    ``token`` lets a caller supply the key per request. The site is public and
    unauthenticated, so storing a key server-side would leave it readable by
    anyone who finds the settings page; instead the browser keeps it and sends
    it with the search. The configured key stays as a fallback.
    """
    currency = currency or settings.default_currency
    token = (token or settings.travelpayouts_token).strip()
    if not token:
        raise MissingToken(
            "還沒有 Travelpayouts token,無法查價。"
            "到 travelpayouts.com 註冊 affiliate 帳號取得 token,"
            "填進頁面右上角的「設定」即可(只存在你的瀏覽器裡)。"
        )

    owns_client = client is None
    client = client or httpx.Client(timeout=settings.request_timeout_s)
    counts: dict[tuple[str, str, str], int] = {}
    failures: dict[tuple[str, str, str], str] = {}
    try:
        for origin, destination in pairs:
            for month in months:
                key = (origin, destination, month)
                if not force and _is_fresh(conn, origin, destination, month, currency):
                    counts[key] = _cached_row_count(conn, origin, destination, month, currency)
                    continue
                try:
                    counts[key] = _fetch_route_month(
                        conn, client, origin, destination, month, currency, token
                    )
                except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
                    failures[key] = f"{type(exc).__name__}: {exc}"
                finally:
                    # Whatever happened — a stored month or a logged error —
                    # it is durable before the next route is attempted.
                    conn.commit()
    finally:
        if owns_client:
            client.close()
    return FetchOutcome(counts=counts, failures=failures)


def _is_fresh(
    conn: sqlite3.Connection, origin: str, destination: str, month: str, currency: str
) -> bool:
    row = conn.execute(
        """
        SELECT expires_at FROM route_fetch
        WHERE origin = ? AND destination = ? AND month = ? AND currency = ?
        """,
        (origin, destination, month, currency),
    ).fetchone()
    if not row:
        return False
    return datetime.fromisoformat(row["expires_at"]) > utcnow()


def _cached_row_count(
    conn: sqlite3.Connection, origin: str, destination: str, month: str, currency: str
) -> int:
    row = conn.execute(
        """
        SELECT row_count FROM route_fetch
        WHERE origin = ? AND destination = ? AND month = ? AND currency = ?
        """,
        (origin, destination, month, currency),
    ).fetchone()
    return int(row["row_count"]) if row else 0


def _fetch_route_month(
    conn: sqlite3.Connection,
    client: httpx.Client,
    origin: str,
    destination: str,
    month: str,
    currency: str,
    token: str,
) -> int:
    endpoint = settings.price_month_endpoint
    url, params = _build_request(endpoint, origin, destination, month, currency)
    started = time.perf_counter()
    try:
        response = client.get(
            url,
            params=params,
            headers={
                "X-Access-Token": token,
                "Accept-Encoding": "gzip, deflate",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — logging the failure is the job
        log_fetch(
            conn,
            source=SOURCE,
            endpoint=endpoint,
            params={"origin": origin, "destination": destination, "month": month},
            status_code=getattr(getattr(exc, "response", None), "status_code", None),
            row_count=0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
        # Commit the log row before unwinding. Without this the record of the
        # failure is rolled back along with the transaction, and /api/health
        # reports a source that has never had a problem.
        conn.commit()
        raise

    points = parse_month_payload(endpoint, payload, origin, destination, currency)

    # 再問一次 v3。它涵蓋的天數比較少,但**每一列都帶航空公司** —— 而
    # month-matrix 一列都沒有。合併時同一天取最便宜的,所以「這個價是誰飛的」
    # 只有在 v3 那筆真的勝出時才會標上去,不會張冠李戴。
    try:
        points = _merge_cheapest(
            points, fetch_v3_oneway(client, origin, destination, month, currency, token)
        )
    except Exception as exc:  # noqa: BLE001 — 純加值,失敗不該擋掉主要來源
        # 但**要留下記錄**。第一版這裡是 `pass`,結果 v3 一列都沒進來而畫面上
        # 完全看不出為什麼 —— 那正是這個專案到處在防的靜默失敗。
        log_fetch(
            conn,
            source=SOURCE,
            endpoint="prices_for_dates",
            params={"origin": origin, "destination": destination, "month": month},
            status_code=getattr(getattr(exc, "response", None), "status_code", None),
            row_count=0,
            duration_ms=0,
            error=f"{type(exc).__name__}: {exc}",
        )

    _store(conn, origin, destination, month, currency, points)
    log_fetch(
        conn,
        source=SOURCE,
        endpoint=endpoint,
        params={"origin": origin, "destination": destination, "month": month},
        status_code=response.status_code,
        row_count=len(points),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return len(points)


V3_URL = f"{BASE_URL}/aviasales/v3/prices_for_dates"


def fetch_v3_oneway(
    client: httpx.Client, origin: str, destination: str, month: str,
    currency: str, token: str,
) -> list[PricePoint]:
    """v3 的單程價 —— 它**帶航空公司代碼**,而 month-matrix 沒有。

    這是「畫面上那個價不是長榮的價」唯一有解的地方。實測 `one_way=true` 時
    每一列都有 `airline`(TPE→NRT 2026-09:17 列 17 天,17 列都有)。

    ⚠️ 它**沒有**航空公司篩選參數 —— `airline` / `airlines` / `airline_iata` /
    `carrier` 四種寫法回傳位元組完全相同,參數被忽略。而且它一天只給最便宜的
    那一筆,所以那 17 天的航空公司清一色是廉航(TR/MM/GK/ZE/LJ/SL/D7/IT)。
    也就是說:能做到的是**把價格標上是誰飛的**,不是「只算長榮的價」。

    涵蓋範圍比 month-matrix 窄(17 天 vs 29 天),所以這是**補充**不是取代。
    """
    response = client.get(
        V3_URL,
        params={
            "origin": origin,
            "destination": destination,
            "departure_at": month,
            "one_way": "true",
            "limit": 1000,
            "sorting": "price",
            "currency": currency.lower(),
            "token": token,
        },
    )
    response.raise_for_status()
    fetched_at = utcnow()
    points: list[PricePoint] = []
    for entry in response.json().get("data") or []:
        depart = _parse_date(entry.get("departure_at"))
        price = entry.get("price")
        if depart is None or price is None:
            continue
        points.append(
            PricePoint(
                origin=origin,
                destination=destination,
                depart_date=depart,
                price=float(price),
                currency=currency,
                transfers=entry.get("transfers"),
                airline=entry.get("airline"),
                gate=None,
                flight_number=str(entry.get("flight_number") or "") or None,
                found_at=None,
                fetched_at=fetched_at,
            )
        )
    return points


def _merge_cheapest(*groups: Sequence[PricePoint]) -> list[PricePoint]:
    """同一天只留最便宜的那一筆;**同價時留知道航空公司的那一筆**。

    同價的優先權不是細節,是這個合併唯一的用處。實測 TPE→NRT 2026-09 兩邊
    共同的 17 天**價格一模一樣**(4290/4503/4199…全部相同)—— 它們本來就是
    同一批票價,v3 只是天數少一點、外加一個 `airline` 欄位。所以嚴格用 `<` 比,
    v3 永遠不會勝出,合併就白做了。

    不能反過來「拿 month-matrix 的價貼上 v3 的航空公司」:價格不同的日子,
    那兩筆是不同的班機,貼上去就是把價格算到一家沒有飛那個價的公司頭上。
    只有同價才能換 —— 同價代表就是同一張票。
    """
    cheapest: dict[date, PricePoint] = {}
    for group in groups:
        for point in group:
            existing = cheapest.get(point.depart_date)
            if existing is None or point.price < existing.price:
                cheapest[point.depart_date] = point
            elif (
                point.price == existing.price
                and point.airline
                and not existing.airline
            ):
                cheapest[point.depart_date] = point
    return sorted(cheapest.values(), key=lambda p: p.depart_date)


def _build_request(
    endpoint: str, origin: str, destination: str, month: str, currency: str
) -> tuple[str, dict[str, Any]]:
    """Both month endpoints are supported; the spike picks which one we trust.

    They disagree on shape and possibly on whether a call without a return date
    is genuinely one-way priced, and the published docs don't settle it — so
    the choice is configuration backed by measurement, not a guess baked into
    the code.
    """
    first_of_month = f"{month}-01"
    if endpoint == "month-matrix":
        return (
            f"{BASE_URL}/v2/prices/month-matrix",
            {
                "origin": origin,
                "destination": destination,
                "month": first_of_month,
                "currency": currency.lower(),
                "show_to_affiliates": "true",
            },
        )
    if endpoint == "calendar":
        return (
            f"{BASE_URL}/v1/prices/calendar",
            {
                "origin": origin,
                "destination": destination,
                "depart_date": month,
                "calendar_type": "departure_date",
                "currency": currency.lower(),
            },
        )
    raise ValueError(f"unknown price month endpoint {endpoint!r}")


def parse_month_payload(
    endpoint: str,
    payload: dict[str, Any],
    origin: str,
    destination: str,
    currency: str,
) -> list[PricePoint]:
    """Normalise either endpoint's shape into one-way price points.

    Rows carrying a return date are dropped. A round-trip fare summed across
    legs would double-count the journey home, which is precisely the mistake
    that would make every 拼票 total wrong while still looking plausible.
    """
    # Validate the endpoint name before looking at the payload. Bailing out on
    # an empty response first would turn a mistyped config value into "this
    # route has no cheap flights" instead of a crash.
    if endpoint not in ("month-matrix", "calendar"):
        raise ValueError(f"unknown price month endpoint {endpoint!r}")

    fetched_at = utcnow()
    data = payload.get("data")
    if not data:
        return []

    points: list[PricePoint] = []
    if endpoint == "month-matrix":
        for entry in data:
            if entry.get("return_date"):
                continue
            depart = _parse_date(entry.get("depart_date"))
            price = entry.get("value", entry.get("price"))
            if depart is None or price is None:
                continue
            points.append(
                PricePoint(
                    # Key on what we asked for, not what came back. Aviasales
                    # answers a request for KIX with `destination: "OSA"` and a
                    # request for NRT with `"TYO"` — the echo is a city label,
                    # but the rows really are airport-specific (TPE→NRT and
                    # TPE→HND share not one price for the same month). Storing
                    # the echoed city means every lookup for an airport code
                    # misses, and the whole site reads 「此航段查無資料」 while
                    # sitting on the data.
                    origin=origin,
                    destination=destination,
                    depart_date=depart,
                    price=float(price),
                    currency=currency,
                    transfers=entry.get("number_of_changes"),
                    # month-matrix has no carrier field at all. `gate` is the
                    # booking site — measured values include "Kupi.com",
                    # "Aviakassa", "City.Travel". Reading it as the airline is
                    # how a fare gets attributed to a company that doesn't fly.
                    airline=entry.get("airline"),
                    gate=entry.get("gate"),
                    flight_number=None,
                    found_at=entry.get("found_at"),
                    fetched_at=fetched_at,
                )
            )
    elif endpoint == "calendar":
        for key, entry in data.items():
            if entry.get("return_at"):
                continue
            depart = _parse_date(entry.get("departure_at") or key)
            price = entry.get("price") or entry.get("value")
            if depart is None or price is None:
                continue
            points.append(
                PricePoint(
                    # Same reasoning as month-matrix above: key on the request.
                    origin=origin,
                    destination=destination,
                    depart_date=depart,
                    price=float(price),
                    currency=currency,
                    transfers=entry.get("transfers") or entry.get("number_of_changes"),
                    airline=entry.get("airline"),
                    gate=entry.get("gate"),
                    flight_number=str(entry.get("flight_number") or "") or None,
                    found_at=entry.get("found_at"),
                    fetched_at=fetched_at,
                )
            )
    else:
        raise ValueError(f"unknown price month endpoint {endpoint!r}")

    # Same route, same day can appear more than once; keep the cheapest.
    cheapest: dict[date, PricePoint] = {}
    for point in points:
        existing = cheapest.get(point.depart_date)
        if existing is None or point.price < existing.price:
            cheapest[point.depart_date] = point
    return sorted(cheapest.values(), key=lambda p: p.depart_date)


def _parse_date(raw: Any) -> date | None:
    if not raw:
        return None
    text = str(raw)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _store(
    conn: sqlite3.Connection,
    origin: str,
    destination: str,
    month: str,
    currency: str,
    points: Sequence[PricePoint],
) -> None:
    now = utcnow()
    expires = now + timedelta(hours=settings.price_cache_ttl_hours)
    conn.execute(
        """
        DELETE FROM price_cache
        WHERE origin = ? AND destination = ? AND currency = ?
          AND substr(depart_date, 1, 7) = ?
        """,
        (origin, destination, currency, month),
    )
    conn.executemany(
        """
        INSERT INTO price_cache
            (origin, destination, depart_date, currency, price, transfers, airline,
             gate, flight_number, found_at, fetched_at, expires_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(origin, destination, depart_date, currency) DO UPDATE SET
            price = excluded.price,
            transfers = excluded.transfers,
            airline = excluded.airline,
            gate = excluded.gate,
            flight_number = excluded.flight_number,
            found_at = excluded.found_at,
            fetched_at = excluded.fetched_at,
            expires_at = excluded.expires_at
        """,
        [
            (
                point.origin,
                point.destination,
                point.depart_date.isoformat(),
                currency,
                point.price,
                point.transfers,
                point.airline,
                point.gate,
                point.flight_number,
                point.found_at,
                point.fetched_at.isoformat(),
                expires.isoformat(),
                SOURCE,
            )
            for point in points
        ],
    )
    conn.execute(
        """
        INSERT INTO route_fetch (origin, destination, month, currency, row_count,
                                 fetched_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(origin, destination, month, currency) DO UPDATE SET
            row_count = excluded.row_count,
            fetched_at = excluded.fetched_at,
            expires_at = excluded.expires_at
        """,
        (
            origin,
            destination,
            month,
            currency,
            len(points),
            now.isoformat(),
            expires.isoformat(),
        ),
    )


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

# 「直達」在這裡跟「航空公司」是完全不同的東西:轉機次數我們**真的有資料**
# (`price_cache.transfers`,來自 month-matrix 的 `number_of_changes`),所以它可以
# 誠實地改變站內顯示的數字,不是只能跟著連結出去。
#
# `transfers IS NULL` 一律排除:那代表我們不知道那一筆轉幾次,而「不知道」不能
# 算成「直達」—— 那正是這個站到處在防的那種話。
NONSTOP_SQL = " AND transfers = 0"


def load_lookup(
    conn: sqlite3.Connection,
    pairs: Sequence[tuple[str, str]],
    currency: str | None = None,
    *,
    nonstop: bool = False,
) -> dict[tuple[str, str, date], PricePoint]:
    """Pull every cached price for these routes into memory in one query.

    A search prices thousands of combinations against a few hundred rows, so
    the rows go in a dict once rather than being queried per leg.
    """
    currency = currency or settings.default_currency
    if not pairs:
        return {}
    clauses = " OR ".join("(origin = ? AND destination = ?)" for _ in pairs)
    args: list[Any] = [currency]
    for origin, destination in pairs:
        args.extend([origin, destination])
    rows = conn.execute(
        f"SELECT * FROM price_cache WHERE currency = ? AND ({clauses})"
        + (NONSTOP_SQL if nonstop else ""),
        args,
    ).fetchall()

    lookup: dict[tuple[str, str, date], PricePoint] = {}
    for row in rows:
        point = _point_from_row(row)
        if point is not None:
            lookup[(point.origin, point.destination, point.depart_date)] = point
    return lookup


def _point_from_row(row: sqlite3.Row) -> PricePoint | None:
    depart = _parse_date(row["depart_date"])
    if depart is None:
        return None
    return PricePoint(
        origin=row["origin"],
        destination=row["destination"],
        depart_date=depart,
        price=row["price"],
        currency=row["currency"],
        transfers=row["transfers"],
        airline=row["airline"],
        gate=row["gate"],
        flight_number=row["flight_number"],
        found_at=row["found_at"],
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
    )


def nearest_priced_dates(
    conn: sqlite3.Connection,
    origin: str,
    destination: str,
    target: date,
    *,
    currency: str | None = None,
    limit: int = 3,
    window_days: int | None = 14,
    month: str | None = None,
    nonstop: bool = False,
) -> list[PricePoint]:
    """這條航線上離 `target` 最近、而且有價的日子。

    「12-13 查無資料」單獨看會被讀成「那天飛不回來」。實際上通常是這條航線只有
    少數幾天被人搜過 —— 實測 ITM→TPE 在 2026-12 只有 3 天有價,而同城的
    KIX→TPE 有 28 天。抓價本來就是整月一起抓的,所以那幾天就在手上,把它拿出來
    就能把死路變成下一步。

    `window_days=None` 取消距離限制,`month` 則把答案綁在某一個月裡。兩個一起用
    才是安全的:快取是跨搜尋共用的,上一次查 10 月留下的資料還躺在裡面,不設月份
    就會拿 10 月的票去回答 12 月的問題 —— 而「離得比較遠」跟「那是另一個月的票」
    是兩件事,前者挪三天就好,後者要整趟旅行重排。
    """
    currency = currency or settings.default_currency
    sql = """
        SELECT * FROM price_cache
        WHERE origin = ? AND destination = ? AND currency = ?
    """
    args: list[Any] = [origin, destination, currency]
    if window_days is not None:
        sql += " AND ABS(julianday(depart_date) - julianday(?)) <= ?"
        args += [target.isoformat(), window_days]
    if month is not None:
        sql += " AND substr(depart_date, 1, 7) = ?"
        args.append(month)
    if nonstop:
        sql += NONSTOP_SQL
    sql += " ORDER BY ABS(julianday(depart_date) - julianday(?)) ASC LIMIT ?"
    args += [target.isoformat(), limit]

    rows = conn.execute(sql, args).fetchall()
    return [point for point in map(_point_from_row, rows) if point is not None]


GapReason = Literal[
    "not_fetched", "route_empty", "connections_only", "nearby", "far_only"
]


@dataclass(frozen=True)
class Gap:
    """為什麼這一段沒有價格 —— 分成使用者要採取不同行動的幾種情況。

    它們在畫面上長得一樣(一個空格),但下一步完全不同:沒查過要按重查、
    整個月都沒有要換航線或機場、只有轉機班要放寬「只要直達」、只有遠處有價要改
    日期。全部併成「查無資料」等於把使用者能做的事一起藏起來,而那正是他回報
    「還是沒拿到價格」的原因。
    """

    origin: str
    destination: str
    target: date
    reason: GapReason
    month: str
    month_rows: int
    nearby: tuple[PricePoint, ...] = ()
    same_city: tuple[PricePoint, ...] = ()


def explain_gap(
    conn: sqlite3.Connection,
    origin: str,
    destination: str,
    target: date,
    *,
    currency: str | None = None,
    fetched: dict[tuple[str, str, str], int] | None = None,
    sibling_origins: Sequence[str] = (),
    sibling_destinations: Sequence[str] = (),
    nonstop: bool = False,
) -> Gap:
    """把一個空價格變成一句能行動的話。

    `sibling_*` 是同城的其他機場。同城替代是這裡最有價值的一條出路,因為它連
    日期都不用改:實測 2026-12 的大阪,ITM→TPE 只有 3 天有價,KIX→TPE 有 28 天。
    那些資料早就在同一次搜尋裡抓回來了(城市本來就展開成多個機場),不必多花
    一次呼叫。
    """
    currency = currency or settings.default_currency
    month = target.strftime("%Y-%m")

    if fetched is None:
        row = conn.execute(
            """
            SELECT row_count FROM route_fetch
            WHERE origin = ? AND destination = ? AND month = ? AND currency = ?
            """,
            (origin, destination, month, currency),
        ).fetchone()
        month_rows = int(row["row_count"]) if row else -1
    else:
        month_rows = fetched.get((origin, destination, month), -1)

    same_city = _same_city_prices(
        conn, origin, destination, target,
        currency=currency,
        sibling_origins=sibling_origins,
        sibling_destinations=sibling_destinations,
        nonstop=nonstop,
    )

    if month_rows < 0:
        return Gap(origin, destination, target, "not_fetched", month, 0,
                   same_city=same_city)

    nearby = tuple(nearest_priced_dates(
        conn, origin, destination, target, currency=currency, nonstop=nonstop
    ))
    if nearby:
        return Gap(origin, destination, target, "nearby", month, month_rows,
                   nearby=nearby, same_city=same_city)

    # 近處沒有,不代表這個月都沒有。放寬距離再問一次,但**綁在同一個月裡** ——
    # 快取跨搜尋共用,不綁月份就會拿上次查 10 月留下的票去回答 12 月的問題,
    # 然後配上一句寫著「2026-12」的說明,那是直接說錯話。
    far = tuple(nearest_priced_dates(
        conn, origin, destination, target,
        currency=currency, window_days=None, month=month, nonstop=nonstop,
    ))
    if far:
        return Gap(origin, destination, target, "far_only", month, month_rows,
                   nearby=far, same_city=same_city)

    # 只要直達的時候,「一列都沒有」有兩種完全不同的意思,而且說錯的那種會把
    # 使用者推向錯的結論:這條航線這個月**有**票,只是全部要轉機。說成
    # 「沒人搜過」會讓他去按重查,而重查一百次也不會多出一班直飛。
    if nonstop:
        any_month = nearest_priced_dates(
            conn, origin, destination, target,
            currency=currency, window_days=None, month=month, limit=1,
        )
        if any_month:
            return Gap(origin, destination, target, "connections_only",
                       month, month_rows, same_city=same_city)

    return Gap(origin, destination, target, "route_empty", month, month_rows,
               same_city=same_city)


def _same_city_prices(
    conn: sqlite3.Connection,
    origin: str,
    destination: str,
    target: date,
    *,
    currency: str,
    sibling_origins: Sequence[str],
    sibling_destinations: Sequence[str],
    nonstop: bool = False,
) -> tuple[PricePoint, ...]:
    """同一天、同城不同機場、有價的替代航段。"""
    pairs = [(code, destination) for code in sibling_origins if code != origin]
    pairs += [(origin, code) for code in sibling_destinations if code != destination]
    if not pairs:
        return ()

    clauses = " OR ".join("(origin = ? AND destination = ?)" for _ in pairs)
    args: list[Any] = [currency, target.isoformat()]
    for pair in pairs:
        args.extend(pair)
    rows = conn.execute(
        f"""
        SELECT * FROM price_cache
        WHERE currency = ? AND depart_date = ? AND ({clauses})
        {NONSTOP_SQL if nonstop else ""}
        ORDER BY price ASC
        """,
        args,
    ).fetchall()
    return tuple(p for p in map(_point_from_row, rows) if p is not None)


def fetched_routes(
    conn: sqlite3.Connection, currency: str | None = None
) -> dict[tuple[str, str, str], int]:
    """Which (route, month) pairs we have actually asked about, and what came back."""
    currency = currency or settings.default_currency
    rows = conn.execute(
        "SELECT origin, destination, month, row_count FROM route_fetch WHERE currency = ?",
        (currency,),
    ).fetchall()
    return {(r["origin"], r["destination"], r["month"]): r["row_count"] for r in rows}


def price_combo(
    combo: Combo,
    lookup: dict[tuple[str, str, date], PricePoint],
    fetched: dict[tuple[str, str, str], int] | None = None,
) -> ComboPricing:
    """Price one combination, distinguishing "no flight" from "never looked".

    The distinction is the point. `no_data` means we asked Aviasales about that
    route-month and it had nothing cached; `not_fetched` means we never asked.
    Collapsing both into a blank cell is how a tool ends up implying a route is
    expensive when it simply has no data.
    """
    fetched = fetched if fetched is not None else {}
    legs: list[LegPricing] = []
    for leg in combo.legs:
        point = lookup.get((leg.origin, leg.destination, leg.depart_date))
        if point is not None:
            legs.append(LegPricing(leg=leg, status="ok", point=point))
            continue
        month = leg.depart_date.strftime("%Y-%m")
        asked = (leg.origin, leg.destination, month) in fetched
        legs.append(LegPricing(leg=leg, status="no_data" if asked else "not_fetched"))
    return ComboPricing(combo=combo, legs=tuple(legs))


def rank(pricings: Iterable[ComboPricing]) -> tuple[list[ComboPricing], list[ComboPricing]]:
    """Split into (ranked, unpriceable) rather than filtering.

    Unpriceable combinations are returned so the UI can show them as
    「此航段查無資料」. Dropping them would make a gap in the data
    indistinguishable from an expensive route.
    """
    priced: list[ComboPricing] = []
    unpriced: list[ComboPricing] = []
    for pricing in pricings:
        (priced if pricing.is_complete else unpriced).append(pricing)
    priced.sort(key=lambda p: (p.total if p.total is not None else float("inf")))
    return priced, unpriced
