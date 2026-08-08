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
                    origin=entry.get("origin") or origin,
                    destination=entry.get("destination") or destination,
                    depart_date=depart,
                    price=float(price),
                    currency=currency,
                    transfers=entry.get("number_of_changes"),
                    airline=entry.get("gate") or entry.get("airline"),
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
                    origin=entry.get("origin") or origin,
                    destination=entry.get("destination") or destination,
                    depart_date=depart,
                    price=float(price),
                    currency=currency,
                    transfers=entry.get("transfers") or entry.get("number_of_changes"),
                    airline=entry.get("airline"),
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
             flight_number, found_at, fetched_at, expires_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(origin, destination, depart_date, currency) DO UPDATE SET
            price = excluded.price,
            transfers = excluded.transfers,
            airline = excluded.airline,
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

def load_lookup(
    conn: sqlite3.Connection,
    pairs: Sequence[tuple[str, str]],
    currency: str | None = None,
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
        f"SELECT * FROM price_cache WHERE currency = ? AND ({clauses})", args
    ).fetchall()

    lookup: dict[tuple[str, str, date], PricePoint] = {}
    for row in rows:
        depart = _parse_date(row["depart_date"])
        if depart is None:
            continue
        lookup[(row["origin"], row["destination"], depart)] = PricePoint(
            origin=row["origin"],
            destination=row["destination"],
            depart_date=depart,
            price=row["price"],
            currency=row["currency"],
            transfers=row["transfers"],
            airline=row["airline"],
            flight_number=row["flight_number"],
            found_at=row["found_at"],
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
        )
    return lookup


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
