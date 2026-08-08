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

TRIP_ONE_WAY = 2
TRIP_MULTI_CITY = 3


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
_GF_ORIGIN = 13
_GF_DESTINATION = 14
_GF_PASSENGER = 8
_GF_SEAT = 9
_GF_TRIP = 19


def _google_leg(leg: FlightLeg) -> bytes:
    """One leg inside Google Flights' `tfs` protobuf."""
    return (
        _string_field(_GF_DATE, leg.depart_date.isoformat())
        + _message_field(_GF_ORIGIN, _string_field(2, leg.origin))
        + _message_field(_GF_DESTINATION, _string_field(2, leg.destination))
    )


def google_flights_url(
    legs: Sequence[FlightLeg], *, passengers: int = 1, cabin: str = "economy"
) -> str:
    """Build a Google Flights search URL that opens on this exact itinerary.

    Google encodes the whole search as a protobuf in the `tfs` parameter. The
    format is undocumented and Google can change it, so when this link starts
    opening the wrong search the fix is to re-derive the field numbers in a
    browser rather than to guess — see `docs/verification.md`.
    """
    body = b"".join(_message_field(_GF_LEG, _google_leg(leg)) for leg in legs)
    body += b"".join(_varint_field(_GF_PASSENGER, 1) for _ in range(max(1, passengers)))
    body += _varint_field(_GF_SEAT, CABIN_CODES.get(cabin, 1))
    body += _varint_field(_GF_TRIP, TRIP_ONE_WAY if len(legs) == 1 else TRIP_MULTI_CITY)
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

def links_for_single_ticket(
    combo: Combo, *, passengers: int = 1, cabin: str = "economy", marker: str | None = None
) -> dict[str, str]:
    """Links that price the whole itinerary as one ticket."""
    return {
        "google_flights": google_flights_url(combo.legs, passengers=passengers, cabin=cabin),
        "kayak": kayak_url(combo.legs, passengers=passengers, cabin=cabin),
        "aviasales": aviasales_url(combo.legs, passengers=passengers, marker=marker),
    }


def links_for_split_tickets(
    combo: Combo,
    *,
    passengers: int = 1,
    cabin: str = "economy",
    marker: str | None = None,
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
            "google_flights": google_flights_url([leg], passengers=passengers, cabin=cabin),
            "kayak": kayak_url([leg], passengers=passengers, cabin=cabin),
            "aviasales": aviasales_url([leg], passengers=passengers, marker=marker),
        }
        for leg in combo.legs
    ]


def one_way_url(origin: str, destination: str, depart: date, *, passengers: int = 1) -> str:
    """Convenience for the spike script and manual price checks."""
    leg = FlightLeg(origin, destination, depart, origin, destination)
    return google_flights_url([leg], passengers=passengers)
