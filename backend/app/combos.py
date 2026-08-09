"""Turn a loose "I want to visit these places around these dates" request into
concrete, priceable itineraries.

Everything in this module is pure — no network, no database, no clock. Every
input arrives in the request object, so the whole thing is deterministically
testable. That matters here more than anywhere else in the codebase: a silent
off-by-one in the date arithmetic doesn't crash, it just quietly ranks the
wrong itinerary first.

Vocabulary
----------
Waypoint  一個停留點(台北 / 東京 / 大阪),帶著它的候選機場清單
Link      兩個停留點之間怎麼移動 —— FLY(要查價)或 SURFACE(自己搭新幹線)
Shape     一種行程骨架,例如「台北→東京 ~陸路~ 大阪→台北」
Combo     Shape 填上具體機場與具體日期之後,一個可以拿去查價的行程
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Iterator, Sequence

# Explicit ceilings. When a request blows past one of these we raise instead of
# truncating: a silently trimmed search looks exactly like "there was nothing
# cheaper", which is the single most misleading thing this tool could do.
MAX_AIRPORTS_PER_WAYPOINT = 6
MAX_DEPART_WINDOW_DAYS = 21
MAX_TRIP_NIGHTS = 30
MAX_STOPS = 4
MAX_COMBOS = 20_000


class Link(str, Enum):
    FLY = "fly"
    SURFACE = "surface"  # 自己走(高鐵/巴士/船):不查價,但會佔掉天數


class SpecTooLarge(ValueError):
    """Raised when a request would exceed a ceiling.

    Carries the specific thing to shrink so the API can tell the user what to
    change, rather than handing back a bare "too many combinations".
    """

    def __init__(self, message: str, *, offender: str, estimate: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.offender = offender
        self.estimate = estimate


@dataclass(frozen=True)
class Waypoint:
    """A place the trip touches, and the airports that count as "there".

    ``airports`` is already expanded from the user's country/city picks by
    :mod:`app.refdata` — by the time a waypoint reaches this module the
    substitution question (東京 = NRT + HND) is settled.
    """

    label: str
    airports: tuple[str, ...]
    country_code: str = ""
    nights_min: int = 0
    nights_max: int = 0

    def __post_init__(self) -> None:
        if not self.airports:
            raise ValueError(f"waypoint {self.label!r} has no airports")
        if self.nights_min < 0 or self.nights_max < self.nights_min:
            raise ValueError(
                f"waypoint {self.label!r} has an impossible stay range "
                f"({self.nights_min}–{self.nights_max})"
            )


@dataclass(frozen=True)
class Shape:
    """One itinerary skeleton: which places, in which order, joined how."""

    waypoints: tuple[Waypoint, ...]
    links: tuple[Link, ...]
    # Baselines are single-city round trips generated alongside the real
    # multi-stop options. They are NOT competitors — you see fewer cities — so
    # the UI shows them as a reference line, not as a ranked row.
    is_baseline: bool = False

    def __post_init__(self) -> None:
        if len(self.links) != len(self.waypoints) - 1:
            raise ValueError("links must sit between consecutive waypoints")
        if Link.FLY not in self.links:
            raise ValueError("a shape with no flights has nothing to price")

    @property
    def label(self) -> str:
        parts = [self.waypoints[0].label]
        for link, wp in zip(self.links, self.waypoints[1:]):
            parts.append("→" if link is Link.FLY else "〜")
            parts.append(wp.label)
        return "".join(parts)


@dataclass(frozen=True)
class FlightLeg:
    origin: str
    destination: str
    depart_date: date
    from_label: str
    to_label: str


@dataclass(frozen=True)
class Combo:
    """A fully concrete itinerary: real airports, real dates, ready to price."""

    legs: tuple[FlightLeg, ...]
    shape_label: str
    is_baseline: bool
    nights: tuple[int, ...] = field(default=())

    @property
    def key(self) -> str:
        raw = "|".join(
            f"{leg.origin}>{leg.destination}@{leg.depart_date.isoformat()}" for leg in self.legs
        )
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    @property
    def depart_date(self) -> date:
        return self.legs[0].depart_date

    @property
    def return_date(self) -> date:
        return self.legs[-1].depart_date


@dataclass(frozen=True)
class SearchRequest:
    """What the user actually filled in on the form."""

    home: Waypoint
    stops: tuple[Waypoint, ...]
    depart_earliest: date
    depart_latest: date
    # Same-country hops default to SURFACE (東京→大阪 搭新幹線); crossing a
    # border defaults to FLY. The user can override per gap in the UI.
    internal_links: tuple[Link, ...] | None = None
    # Trying both visit orders is free — it costs no extra API calls, only
    # combinations — and reversing the order is a genuine source of savings.
    try_both_orders: bool = True
    include_baselines: bool = True
    one_way: bool = False
    passengers: int = 1
    cabin: str = "economy"
    currency: str = "TWD"
    # 想搭的航空公司(IATA)。**這不會改變排名**,因為排名用的快取價根本沒有
    # 航空公司欄位 —— 它只會跟著深連結出去,讓點過去的搜尋只列這幾家。
    airlines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.stops:
            raise ValueError("a trip needs at least one destination")
        if self.depart_latest < self.depart_earliest:
            raise ValueError("departure window ends before it starts")


def default_links(stops: Sequence[Waypoint]) -> tuple[Link, ...]:
    """Guess how the traveller moves between consecutive stops.

    Same country → assume they get themselves there (新幹線/巴士). Different
    country → assume they fly. This is only a default; the point of an open-jaw
    search is usually exactly the surface hop, so getting it right by default
    saves the user a step.
    """
    links: list[Link] = []
    for previous, nxt in zip(stops, stops[1:]):
        same_country = bool(previous.country_code) and previous.country_code == nxt.country_code
        links.append(Link.SURFACE if same_country else Link.FLY)
    return tuple(links)


def expand_shapes(request: SearchRequest) -> list[Shape]:
    """Enumerate the itinerary skeletons worth pricing for this request.

    For 台北 + {東京, 大阪} that means:
      台北→東京 〜 大阪→台北      (開口,原順序)
      台北→大阪 〜 東京→台北      (開口,反向)
      台北→東京→台北              (基準:只玩東京)
      台北→大阪→台北              (基準:只玩大阪)
    """
    _validate_size(request)

    orders: list[tuple[Waypoint, ...]] = [tuple(request.stops)]
    if request.try_both_orders and len(request.stops) > 1:
        seen = {tuple(w.label for w in request.stops)}
        for permutation in itertools.permutations(request.stops):
            key = tuple(w.label for w in permutation)
            if key not in seen:
                seen.add(key)
                orders.append(permutation)

    shapes: list[Shape] = []
    for index, order in enumerate(orders):
        # Only the order the user actually typed inherits their per-gap
        # choices; a reversed order gets freshly derived defaults, because
        # "第二段自己走" doesn't mean the same thing once the stops swap.
        links = request.internal_links if index == 0 else None
        if links is None or len(links) != len(order) - 1:
            links = default_links(order)
        shapes.append(_build_shape(request, order, links, is_baseline=False))

    if request.include_baselines and len(request.stops) > 1:
        for stop in request.stops:
            shapes.append(_build_shape(request, (stop,), (), is_baseline=True))

    return shapes


def _build_shape(
    request: SearchRequest,
    order: Sequence[Waypoint],
    internal_links: Sequence[Link],
    *,
    is_baseline: bool,
) -> Shape:
    waypoints = [request.home, *order]
    links = [Link.FLY, *internal_links]
    if not request.one_way:
        # Coming home is a flight by definition, and home never accrues nights.
        waypoints.append(Waypoint(request.home.label, request.home.airports, request.home.country_code))
        links.append(Link.FLY)
    return Shape(tuple(waypoints), tuple(links), is_baseline=is_baseline)


def generate(shape: Shape, request: SearchRequest) -> Iterator[Combo]:
    """Yield every concrete combination for one shape.

    Dates fall out of (departure date, nights spent at each stop) rather than
    being enumerated per-leg. That is both how people actually plan a trip and
    what keeps the search finite: a 14-day window with two stops of 2–4 nights
    each is 14 × 3 × 3 date combinations, not 14³.
    """
    fly_indices = [i for i, link in enumerate(shape.links) if link is Link.FLY]
    depart_dates = _date_range(request.depart_earliest, request.depart_latest)
    # The final waypoint is never departed from, so however long you stay there
    # cannot move any leg date. Excluding it keeps the search from multiplying
    # out a dimension that changes nothing.
    interior = shape.waypoints[1:-1]

    airport_choices = [
        list(itertools.product(shape.waypoints[i].airports, shape.waypoints[i + 1].airports))
        for i in fly_indices
    ]

    for depart_date in depart_dates:
        for nights in _night_vectors(interior):
            if sum(nights) > MAX_TRIP_NIGHTS:
                continue
            # nights aligned to waypoint index; the first waypoint (home) and
            # the last (home again, or the final stop) never accrue offset.
            padded = [0, *nights, 0]
            offsets = list(itertools.accumulate(padded))
            for pairs in itertools.product(*airport_choices):
                legs = tuple(
                    FlightLeg(
                        origin=origin,
                        destination=destination,
                        depart_date=depart_date + timedelta(days=offsets[link_index]),
                        from_label=shape.waypoints[link_index].label,
                        to_label=shape.waypoints[link_index + 1].label,
                    )
                    for link_index, (origin, destination) in zip(fly_indices, pairs)
                )
                yield Combo(
                    legs=legs,
                    shape_label=shape.label,
                    is_baseline=shape.is_baseline,
                    nights=tuple(nights),
                )


def generate_all(request: SearchRequest) -> list[Combo]:
    """Every combination across every shape, with the ceiling enforced.

    Raises :class:`SpecTooLarge` rather than returning a partial list.
    """
    shapes = expand_shapes(request)
    estimate = sum(count_combos(shape, request) for shape in shapes)
    if estimate > MAX_COMBOS:
        raise SpecTooLarge(
            f"這組條件會產生約 {estimate:,} 種組合,超過上限 {MAX_COMBOS:,}。"
            "請縮短日期區間、減少候選機場,或固定造訪順序。",
            offender="combos",
            estimate=estimate,
        )
    return [combo for shape in shapes for combo in generate(shape, request)]


def count_combos(shape: Shape, request: SearchRequest) -> int:
    """Size a shape without materialising it."""
    days = (request.depart_latest - request.depart_earliest).days + 1
    interior = shape.waypoints[1:-1]
    nights_options = 1
    for wp in interior:
        nights_options *= wp.nights_max - wp.nights_min + 1
    airports = 1
    for i, link in enumerate(shape.links):
        if link is Link.FLY:
            airports *= len(shape.waypoints[i].airports) * len(shape.waypoints[i + 1].airports)
    return days * nights_options * airports


def route_pairs(request: SearchRequest) -> set[tuple[str, str]]:
    """Every (origin, destination) airport pair the search will need priced.

    This is the number that actually costs API calls — the date dimension is
    free because one calendar call covers a whole month. Used to show the user
    the cost of their search before running it.
    """
    pairs: set[tuple[str, str]] = set()
    for shape in expand_shapes(request):
        for i, link in enumerate(shape.links):
            if link is not Link.FLY:
                continue
            for origin in shape.waypoints[i].airports:
                for destination in shape.waypoints[i + 1].airports:
                    pairs.add((origin, destination))
    return pairs


def _validate_size(request: SearchRequest) -> None:
    if len(request.stops) > MAX_STOPS:
        raise SpecTooLarge(
            f"最多支援 {MAX_STOPS} 個停留點,目前有 {len(request.stops)} 個。",
            offender="stops",
        )
    window = (request.depart_latest - request.depart_earliest).days + 1
    if window > MAX_DEPART_WINDOW_DAYS:
        raise SpecTooLarge(
            f"出發日區間 {window} 天超過上限 {MAX_DEPART_WINDOW_DAYS} 天,請縮短。",
            offender="depart_window",
        )
    for wp in (request.home, *request.stops):
        if len(wp.airports) > MAX_AIRPORTS_PER_WAYPOINT:
            raise SpecTooLarge(
                f"「{wp.label}」選了 {len(wp.airports)} 個機場,超過上限 "
                f"{MAX_AIRPORTS_PER_WAYPOINT} 個,請減少。",
                offender=f"airports:{wp.label}",
            )
        if wp.nights_max > MAX_TRIP_NIGHTS:
            raise SpecTooLarge(
                f"「{wp.label}」停留 {wp.nights_max} 晚超過上限 {MAX_TRIP_NIGHTS} 晚。",
                offender=f"nights:{wp.label}",
            )


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _night_vectors(waypoints: Sequence[Waypoint]) -> Iterator[tuple[int, ...]]:
    ranges = [range(wp.nights_min, wp.nights_max + 1) for wp in waypoints]
    yield from itertools.product(*ranges)
