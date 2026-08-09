"""把一個組合變成「一鍵去看真價」的連結。

沒有免費 API 能報一張真正的多段機票價格(Amadeus 自助 2026-07-17 關閉、
Kiwi Tequila 邀請制),所以深連結不是備案,是這個產品的預設出貨路徑:
我們負責把幾千種組合縮到值得一看的那幾組,實際成交價交給訂票網站。

三個目的地各有分工:
* **Google Flights** —— 驗證用的黃金標準,支援真正的 multi-city。
* **Kayak** —— URL 格式最直白、最不容易壞,適合當備援。
* **Aviasales** —— 我們快取價的同一個來源,所以它的報價最接近站內顯示的數字;
  也是 affiliate 連結。
"""

from __future__ import annotations

import base64
from datetime import date
from typing import Sequence

from app.combos import Combo, FlightLeg
from app.config import settings

CABIN_CODES = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}

TRIP_ROUND_TRIP = 1
TRIP_ONE_WAY = 2
TRIP_MULTI_CITY = 3


def _trip_type(legs: Sequence[FlightLeg]) -> int:
    """Which of Google's three search modes this itinerary is.

    A there-and-back pair must go out as a round trip, not multi-city. Both
    render the same fields, but Google prices them differently — a round-trip
    fare is constructed as one journey, a multi-city one is not. Sending a
    plain round trip as multi-city inflates it, which would quietly bias the
    倒買法 comparison against the ordinary way of buying. Verified in a
    browser: trip=1 shows 「Round trip」 with a Return field, trip=3 shows
    「Multi-city」.
    """
    if len(legs) == 1:
        return TRIP_ONE_WAY
    if (
        len(legs) == 2
        and legs[0].origin == legs[1].destination
        and legs[0].destination == legs[1].origin
    ):
        return TRIP_ROUND_TRIP
    return TRIP_MULTI_CITY


# --------------------------------------------------------------------------
# Google Flights
# --------------------------------------------------------------------------

def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        out.append(chunk | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _tag(field: int, wire_type: int) -> bytes:
    return _varint((field << 3) | wire_type)


def _string_field(field: int, value: str) -> bytes:
    raw = value.encode()
    return _tag(field, 2) + _varint(len(raw)) + raw


def _message_field(field: int, value: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(value)) + value


def _varint_field(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


# Field numbers inside Google Flights' undocumented `tfs` protobuf. These were
# established by loading candidate URLs in a real browser and reading back the
# form Google rendered — field 16 for the destination (what several public
# snippets claim) silently leaves 「Where to?」 blank, which produces a link
# that opens a plausible-looking but wrong search.
_GF_LEG = 3
_GF_DATE = 2
_GF_AIRLINE = 6
_GF_ORIGIN = 13
_GF_DESTINATION = 14
_GF_PASSENGER = 8
_GF_SEAT = 9
_GF_TRIP = 19


def _google_leg(leg: FlightLeg, airlines: Sequence[str] = ()) -> bytes:
    """One leg inside Google Flights' `tfs` protobuf.

    The airline filter is a repeated IATA code **inside the leg**, not a
    top-level field — read back out of the URL Google itself produced after
    ticking 長榮 and 華航 in its own filter panel. Setting it on every leg is
    what the UI does for multi-city, so we do the same.
    """
    body = _string_field(_GF_DATE, leg.depart_date.isoformat())
    for code in airlines:
        body += _string_field(_GF_AIRLINE, code)
    return (
        body
        + _message_field(_GF_ORIGIN, _string_field(2, leg.origin))
        + _message_field(_GF_DESTINATION, _string_field(2, leg.destination))
    )


def normalise_airlines(codes: Sequence[str] | None) -> tuple[str, ...]:
    """Two-letter IATA codes, upper-cased, de-duplicated, order preserved.

    Anything else is dropped rather than passed through: a junk code in the
    filter makes Google return an empty result list, which reads exactly like
    「這條航線那天沒有班機」 — the one message this whole change exists to stop
    being wrong.
    """
    seen: list[str] = []
    for raw in codes or ():
        code = (raw or "").strip().upper()
        if len(code) == 2 and code.isalnum() and code not in seen:
            seen.append(code)
    return tuple(seen)


def google_flights_url(
    legs: Sequence[FlightLeg],
    *,
    passengers: int = 1,
    cabin: str = "economy",
    airlines: Sequence[str] = (),
) -> str:
    """Build a Google Flights search URL that opens on this exact itinerary.

    Google encodes the whole search as a protobuf in the `tfs` parameter. The
    format is undocumented and Google can change it, so when this link starts
    opening the wrong search the fix is to re-derive the field numbers in a
    browser rather than to guess — see `docs/verification.md`.
    """
    picked = normalise_airlines(airlines)
    body = b"".join(_message_field(_GF_LEG, _google_leg(leg, picked)) for leg in legs)
    body += b"".join(_varint_field(_GF_PASSENGER, 1) for _ in range(max(1, passengers)))
    body += _varint_field(_GF_SEAT, CABIN_CODES.get(cabin, 1))
    body += _varint_field(_GF_TRIP, _trip_type(legs))
    tfs = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"https://www.google.com/travel/flights?tfs={tfs}&curr={settings.default_currency}&hl=zh-TW"


# --------------------------------------------------------------------------
# Kayak
# --------------------------------------------------------------------------

def kayak_url(legs: Sequence[FlightLeg], *, passengers: int = 1, cabin: str = "economy") -> str:
    """Kayak takes each leg as a plain `ORIGIN-DEST/YYYY-MM-DD` path segment."""
    path = "/".join(f"{leg.origin}-{leg.destination}/{leg.depart_date.isoformat()}" for leg in legs)
    suffix = f"?sort=price_a&travelers={max(1, passengers)}"
    if cabin != "economy":
        suffix += f"&cabin={cabin}"
    return f"https://www.kayak.com/flights/{path}{suffix}"


# --------------------------------------------------------------------------
# Aviasales
# --------------------------------------------------------------------------

def _aviasales_segment(leg: FlightLeg) -> str:
    return f"{leg.origin}{leg.depart_date.strftime('%d%m')}{leg.destination}"


def aviasales_url(
    legs: Sequence[FlightLeg], *, passengers: int = 1, marker: str | None = None
) -> str:
    """Aviasales packs the search into the path: ORIGIN + DDMM + DEST, repeated.

    ``marker`` is the affiliate id; it defaults to the configured one but stays
    an argument so tests don't have to reach into global settings.
    """
    route = "".join(_aviasales_segment(leg) for leg in legs)
    url = f"https://www.aviasales.com/search/{route}{max(1, passengers)}"
    marker = settings.travelpayouts_marker if marker is None else marker
    if marker:
        url += f"?marker={marker}"
    return url


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

# 只有 Google Flights 的航空公司篩選是實測過的(在瀏覽器裡勾選再讀回 URL)。
# Kayak 擋機器人、Aviasales 的參數沒驗過,所以那兩個連結刻意不帶篩選 ——
# 送出一個沒驗證過的篩選參數,失敗的樣子是「查無班機」,跟真的沒有班機分不開。
AIRLINE_FILTER_TARGETS = ("google_flights",)


def links_for_single_ticket(
    combo: Combo,
    *,
    passengers: int = 1,
    cabin: str = "economy",
    marker: str | None = None,
    airlines: Sequence[str] = (),
) -> dict[str, str]:
    """Links that price the whole itinerary as one ticket."""
    return {
        "google_flights": google_flights_url(
            combo.legs, passengers=passengers, cabin=cabin, airlines=airlines
        ),
        "kayak": kayak_url(combo.legs, passengers=passengers, cabin=cabin),
        "aviasales": aviasales_url(combo.legs, passengers=passengers, marker=marker),
    }


def links_for_split_tickets(
    combo: Combo,
    *,
    passengers: int = 1,
    cabin: str = "economy",
    marker: str | None = None,
    airlines: Sequence[str] = (),
) -> list[dict[str, str]]:
    """One set of links per leg — this is the 拼票 買法.

    Each leg is searched as an independent one-way, which is exactly what makes
    it cheaper and exactly what makes it riskier: nobody is responsible for the
    connection between two separately booked tickets.
    """
    return [
        {
            "leg": f"{leg.origin}→{leg.destination}",
            "date": leg.depart_date.isoformat(),
            "google_flights": google_flights_url(
                [leg], passengers=passengers, cabin=cabin, airlines=airlines
            ),
            "kayak": kayak_url([leg], passengers=passengers, cabin=cabin),
            "aviasales": aviasales_url([leg], passengers=passengers, marker=marker),
        }
        for leg in combo.legs
    ]


def one_way_url(origin: str, destination: str, depart: date, *, passengers: int = 1) -> str:
    """Convenience for the spike script and manual price checks."""
    leg = FlightLeg(origin, destination, depart, origin, destination)
    return google_flights_url([leg], passengers=passengers)
