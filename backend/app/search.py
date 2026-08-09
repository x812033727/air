"""把「使用者填的表單」變成「排好序、標好來源與風險的組合清單」。

這個模組是三層之間的接線:refdata 展開機場 → combos 產生組合 →
cached 計價 → 這裡排序、算省多少、標風險、掛深連結。

它刻意不碰 HTTP,也不碰 FastAPI 的型別 —— 這樣整條主線可以用一個假的 lookup
字典完整測完,不需要起服務、不需要網路。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

from app import gaps, refdata
from app.combos import (
    Combo,
    Link,
    SearchRequest,
    Waypoint,
    count_combos,
    expand_shapes,
    generate_all,
    route_pairs,
)
from app.config import settings
from app.pricing import cached, deeplinks
from app.pricing.cached import ComboPricing


# How many rows actually reach the browser. Ranking covers everything; this
# only bounds what gets serialised, and the full counts ride along with it.
MAX_RESULTS = 50
UNPRICEABLE_SAMPLE = 10
# Baselines are a reference line, not a list to shop from — the cheapest few
# answer "what would a plain round trip have cost?" and more just adds noise.
BASELINE_LIMIT = 4


class UnknownPlace(ValueError):
    """A code the user picked doesn't resolve to any bookable airport."""


@dataclass(frozen=True)
class StopSelection:
    codes: tuple[str, ...]
    nights_min: int = 2
    nights_max: int = 4
    label: str | None = None


def build_request(
    conn: sqlite3.Connection,
    *,
    home_codes: Sequence[str],
    stops: Sequence[StopSelection],
    depart_earliest: date,
    depart_latest: date,
    one_way: bool = False,
    try_both_orders: bool = True,
    internal_links: Sequence[str] | None = None,
    passengers: int = 1,
    cabin: str = "economy",
    currency: str | None = None,
    airlines: Sequence[str] | None = None,
) -> SearchRequest:
    """Resolve the user's city/airport picks into a concrete search request."""
    home = _waypoint(conn, home_codes, label=None, nights=(0, 0))
    resolved_stops = tuple(
        _waypoint(conn, stop.codes, label=stop.label, nights=(stop.nights_min, stop.nights_max))
        for stop in stops
    )
    links = (
        tuple(Link(value) for value in internal_links) if internal_links is not None else None
    )
    return SearchRequest(
        home=home,
        stops=resolved_stops,
        depart_earliest=depart_earliest,
        depart_latest=depart_latest,
        internal_links=links,
        try_both_orders=try_both_orders,
        one_way=one_way,
        passengers=passengers,
        cabin=cabin,
        currency=currency or settings.default_currency,
        airlines=deeplinks.normalise_airlines(airlines),
    )


def _waypoint(
    conn: sqlite3.Connection,
    codes: Sequence[str],
    *,
    label: str | None,
    nights: tuple[int, int],
) -> Waypoint:
    airports = refdata.expand_airports(conn, codes)
    if not airports:
        raise UnknownPlace(f"找不到可飛的機場:{', '.join(codes)}")
    # A selection spanning several cities (e.g. 「日本」) is labelled by the
    # first city so the itinerary still reads like a place, not a code list.
    display = label or airports[0].city_name
    return Waypoint(
        label=display,
        airports=tuple(a.code for a in airports),
        country_code=airports[0].country_code,
        nights_min=nights[0],
        nights_max=nights[1],
    )


def estimate_cost(request: SearchRequest) -> dict[str, Any]:
    """How much API quota this search will spend, before spending it.

    Shown to the user because the honest answer to "why is my search
    limited?" is arithmetic, not a shrug.
    """
    pairs = route_pairs(request)
    months = cached.months_covering(
        [request.depart_earliest, request.depart_latest, _latest_return(request)]
    )
    combos = sum(count_combos(shape, request) for shape in expand_shapes(request))
    return {
        "route_pairs": len(pairs),
        "months": months,
        "api_calls": len(pairs) * len(months),
        "combinations": combos,
    }


def _latest_return(request: SearchRequest) -> date:
    from datetime import timedelta

    longest_stay = sum(stop.nights_max for stop in request.stops)
    return request.depart_latest + timedelta(days=longest_stay)


def warm(
    conn: sqlite3.Connection, request: SearchRequest, *, token: str | None = None
) -> dict[str, Any]:
    """Fetch the route-months this search needs, and report what came back.

    Kept separate from :func:`run` because it is the slow half: 20–40 sequential
    upstream calls. Folding it into the search endpoint would mean a request
    that blocks for ten-plus seconds with nothing to show; split, the page can
    warm with a progress indicator and then read instantly from cache.
    """
    pairs = sorted(route_pairs(request))
    months = _months_for(request)
    warnings: list[str] = []
    outcome = cached.FetchOutcome(counts={}, failures={})
    try:
        outcome = cached.ensure_routes(
            conn, pairs, months, currency=request.currency, token=token
        )
    except cached.MissingToken as exc:
        warnings.append(str(exc))

    if outcome.unauthorized:
        warnings.append(
            "Travelpayouts 拒絕了這組 token(全部航線都回 401/403)。"
            "請確認金鑰有沒有貼錯,以及帳號是否已加入 Aviasales 聯盟方案。"
        )
    elif outcome.failures:
        # Name them. "搜尋沒有成功" with no detail is the same dead end as a
        # blank price cell — the user can't tell a throttled route from a
        # route with no cheap flights.
        warnings.append(
            f"以下航線這次沒抓成功(其餘航線的價格仍然可用,稍後重試即可):"
            f"{', '.join(outcome.failed_routes)}"
        )

    return {
        "routes_fetched": len(outcome.counts),
        "rows": outcome.rows,
        "empty_routes": outcome.empty_routes,
        "failed_routes": outcome.failed_routes,
        "months": months,
        "warnings": warnings,
    }


def _months_for(request: SearchRequest) -> list[str]:
    """Every month a leg of this search could depart in."""
    return cached.months_covering(
        [request.depart_earliest, request.depart_latest, _latest_return(request)]
    )


def run(
    conn: sqlite3.Connection,
    request: SearchRequest,
    *,
    fetch: bool = False,
    limit: int = MAX_RESULTS,
    token: str | None = None,
    marker: str = "",
) -> dict[str, Any]:
    """Rank every combination, but serialise only the ones worth showing.

    Ranking is cheap — it adds floats. Serialising is not: each row carries
    nine deep links, three of which encode a protobuf. A 14-day window over two
    stops produces thousands of combinations, so emitting them all would build
    tens of thousands of URLs into a payload no browser will render. Counts for
    the remainder are reported instead, which is different from dropping them
    silently.
    """
    combos = generate_all(request)
    pairs = sorted(route_pairs(request))
    all_dates = [leg.depart_date for combo in combos for leg in combo.legs]
    months = cached.months_covering(all_dates)

    warnings: list[str] = []
    if fetch:
        try:
            cached.ensure_routes(
                conn, pairs, months, currency=request.currency, token=token
            )
        except cached.MissingToken as exc:
            warnings.append(str(exc))
    elif not (token or settings.has_cached_prices):
        warnings.append(
            "還沒有 Travelpayouts token,所以沒有價格可以排名。"
            "以下只列出行程組合,票價請用每列的連結查。"
        )

    lookup = cached.load_lookup(conn, pairs, request.currency)
    fetched = cached.fetched_routes(conn, request.currency)
    # 同城機場查一次就好。一個空價格問一次,幾百個空價格就是幾百次查詢,
    # 換回來的還是同一張常數大小的對照表。
    siblings = refdata.siblings_by_airport(
        conn, {code for pair in pairs for code in pair}
    )
    pricings = [cached.price_combo(combo, lookup, fetched) for combo in combos]
    priced, unpriced = cached.rank(pricings)

    ranked = [p for p in priced if not p.combo.is_baseline]
    baselines = [p for p in priced if p.combo.is_baseline]
    cheapest_baseline = baselines[0].total if baselines else None

    empty_routes = [
        f"{origin}→{destination}"
        for (origin, destination, month), count in fetched.items()
        if count == 0 and (origin, destination) in set(pairs) and month in months
    ]
    if empty_routes:
        warnings.append(
            f"以下航線在快取中查無任何價格(不代表沒有便宜票,只代表沒有資料):"
            f"{', '.join(sorted(set(empty_routes)))}"
        )

    return {
        "currency": request.currency,
        "cost": estimate_cost(request),
        "counts": {
            "combinations": len(combos),
            "priced": len(ranked),
            "baselines": len(baselines),
            "unpriceable": len(unpriced),
            "shown": min(len(ranked), limit),
        },
        "results": [
            _serialise(p, request, cheapest_baseline, marker, conn, fetched, siblings)
            for p in ranked[:limit]
        ],
        "baselines": [
            _serialise(p, request, None, marker, conn, fetched, siblings)
            for p in baselines[:BASELINE_LIMIT]
        ],
        # A sample, plus the full count above. Enough to show what a gap looks
        # like without shipping thousands of rows nobody will read.
        "unpriceable": [
            _serialise(p, request, None, marker, conn, fetched, siblings)
            for p in unpriced[:UNPRICEABLE_SAMPLE]
        ],
        "warnings": warnings,
    }


def _serialise(
    pricing: ComboPricing,
    request: SearchRequest,
    baseline_total: float | None,
    marker: str = "",
    conn: sqlite3.Connection | None = None,
    fetched: dict[tuple[str, str, str], int] | None = None,
    siblings: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    combo = pricing.combo
    total = pricing.total
    savings = (
        baseline_total - total
        if baseline_total is not None and total is not None
        else None
    )
    oldest = pricing.oldest_fetch
    return {
        "key": combo.key,
        "shape": combo.shape_label,
        "is_baseline": combo.is_baseline,
        "nights": list(combo.nights),
        "depart_date": combo.depart_date.isoformat(),
        "return_date": combo.return_date.isoformat(),
        "legs": [
            {
                "origin": leg.leg.origin,
                "destination": leg.leg.destination,
                "from_label": leg.leg.from_label,
                "to_label": leg.leg.to_label,
                "date": leg.leg.depart_date.isoformat(),
                "status": leg.status,
                "price": leg.price,
                "transfers": leg.point.transfers if leg.point else None,
                "airline": leg.point.airline if leg.point else None,
                "gate": leg.point.gate if leg.point else None,
                "fetched_at": leg.point.fetched_at.isoformat() if leg.point else None,
                "age_hours": round(leg.point.age_hours, 1) if leg.point else None,
                # 「這天查無資料」單看會被讀成「那天沒有飛機」。實際上通常是這條
                # 航線只有少數幾天被人搜過,而抓價本來就是整月一起抓的 ——
                # 那幾天就在手上,拿出來就能把死路變成下一步。
                **gaps.serialise(
                    _gap_for(conn, leg.leg, request, fetched, siblings)
                    if conn is not None and leg.status != "ok"
                    else None
                ),
            }
            for leg in pricing.legs
        ],
        "split_total": total,
        "price_source": "cache" if total is not None else None,
        "oldest_fetch": oldest.isoformat() if oldest else None,
        "savings_vs_baseline": savings,
        "risks": risks(combo, request),
        "links": {
            "single_ticket": deeplinks.links_for_single_ticket(
                combo,
                passengers=request.passengers,
                cabin=request.cabin,
                marker=marker,
                airlines=request.airlines,
            ),
            "split": deeplinks.links_for_split_tickets(
                combo,
                passengers=request.passengers,
                cabin=request.cabin,
                marker=marker,
                airlines=request.airlines,
            ),
        },
    }


def _gap_for(
    conn: sqlite3.Connection,
    leg,
    request: SearchRequest,
    fetched: dict[tuple[str, str, str], int] | None,
    siblings: dict[str, tuple[str, ...]] | None,
) -> cached.Gap:
    siblings = siblings or {}
    return cached.explain_gap(
        conn,
        leg.origin,
        leg.destination,
        leg.depart_date,
        currency=request.currency,
        fetched=fetched,
        sibling_origins=siblings.get(leg.origin, ()),
        sibling_destinations=siblings.get(leg.destination, ()),
    )


def risks(combo: Combo, request: SearchRequest) -> list[str]:
    """What can actually go wrong with this itinerary, stated plainly.

    A cheap combination that quietly assumes you can make a two-hour
    self-transfer on separate tickets is not cheap, it is a bet. The
    ranking doesn't hide these; it labels them.
    """
    notes: list[str] = []
    if len(combo.legs) > 1:
        notes.append("各段分開訂票時行李不直掛,轉機要自己重新報到")

    for previous, nxt in zip(combo.legs, combo.legs[1:]):
        # Check the surface hop first. A shinkansen between 東京 and 大阪 on the
        # same day is a tight schedule, not a missed-connection risk — calling
        # it 「當天轉機」 would describe a flight the traveller never takes.
        moved_by_land = previous.to_label != nxt.from_label
        same_day = previous.depart_date == nxt.depart_date
        if moved_by_land and same_day:
            notes.append(
                f"{previous.to_label} → {nxt.from_label} 當天陸路移動後接下一段,幾乎沒有緩衝"
            )
        elif moved_by_land:
            notes.append(f"{previous.to_label} → {nxt.from_label} 之間要自己移動(陸路)")
        elif same_day:
            notes.append(f"{previous.to_label} 當天轉機:分開訂票時前段誤點沒有人負責後段")

    return notes
