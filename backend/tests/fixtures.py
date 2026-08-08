"""Canned Travelpayouts reference payloads, shared by the tests.

Excerpted from real responses, including one entry that matters: `LMJ`, a bus
terminal that Travelpayouts really does mark `flightable: true` inside city TYO.
"""

from __future__ import annotations

AIRPORTS_PAYLOAD = [
    {
        "code": "NRT", "city_code": "TYO", "country_code": "JP", "iata_type": "airport",
        "flightable": True, "name_translations": {"en": "Narita International Airport"},
        "coordinates": {"lat": 35.77, "lon": 140.39}, "time_zone": "Asia/Tokyo",
    },
    {
        "code": "HND", "city_code": "TYO", "country_code": "JP", "iata_type": "airport",
        "flightable": True, "name_translations": {"en": "Haneda Airport"},
        "coordinates": {"lat": 35.55, "lon": 139.78}, "time_zone": "Asia/Tokyo",
    },
    {
        "code": "LMJ", "city_code": "TYO", "country_code": "JP", "iata_type": "bus",
        "flightable": True, "name_translations": {"en": "Tokyo Bus Station"},
        "coordinates": {"lat": 35.69, "lon": 139.69}, "time_zone": "Asia/Tokyo",
    },
    {
        "code": "KIX", "city_code": "OSA", "country_code": "JP", "iata_type": "airport",
        "flightable": True, "name_translations": {"en": "Kansai International Airport"},
        "coordinates": {"lat": 34.43, "lon": 135.23}, "time_zone": "Asia/Tokyo",
    },
    {
        "code": "ITM", "city_code": "OSA", "country_code": "JP", "iata_type": "airport",
        "flightable": True, "name_translations": {"en": "Itami Airport"},
        "coordinates": {"lat": 34.79, "lon": 135.44}, "time_zone": "Asia/Tokyo",
    },
    {
        "code": "RBJ", "city_code": "RBJ", "country_code": "JP", "iata_type": "airport",
        "flightable": False, "name_translations": {"en": "Rebun"},
        "coordinates": {"lat": 45.38, "lon": 141.03}, "time_zone": "Asia/Tokyo",
    },
    {
        "code": "TPE", "city_code": "TPE", "country_code": "TW", "iata_type": "airport",
        "flightable": True,
        "name_translations": {"en": "Taiwan Taoyuan International Airport"},
        "coordinates": {"lat": 25.08, "lon": 121.22}, "time_zone": "Asia/Taipei",
    },
    {
        "code": "TSA", "city_code": "TPE", "country_code": "TW", "iata_type": "airport",
        "flightable": True, "name_translations": {"en": "Taipei Songshan Airport"},
        "coordinates": {"lat": 25.06, "lon": 121.55}, "time_zone": "Asia/Taipei",
    },
]

CITIES_PAYLOAD = [
    {"code": "TYO", "country_code": "JP", "name_translations": {"en": "Tokyo"},
     "has_flightable_airport": True, "coordinates": {"lat": 35.68, "lon": 139.65}},
    {"code": "OSA", "country_code": "JP", "name_translations": {"en": "Osaka"},
     "has_flightable_airport": True, "coordinates": {"lat": 34.69, "lon": 135.50}},
    {"code": "RBJ", "country_code": "JP", "name_translations": {"en": "Rebun"},
     "has_flightable_airport": False, "coordinates": {"lat": 45.38, "lon": 141.03}},
    {"code": "TPE", "country_code": "TW", "name_translations": {"en": "Taipei"},
     "has_flightable_airport": True, "coordinates": {"lat": 25.03, "lon": 121.57}},
]

COUNTRIES_PAYLOAD = [
    {"code": "JP", "name_translations": {"en": "Japan"}, "currency": "JPY"},
    {"code": "TW", "name_translations": {"en": "Taiwan"}, "currency": "TWD"},
    {"code": "AB", "name_translations": {"en": "Abkhazia"}, "currency": "RUB"},
]

AIRLINES_PAYLOAD = [
    {"code": "BR", "name_translations": {"en": "EVA Air"}},
    {"code": "IT", "name_translations": {"en": "Tigerair Taiwan"}},
    {"code": "MM", "name_translations": {"en": "Peach"}},
]

PAYLOADS = {
    "countries": COUNTRIES_PAYLOAD,
    "cities": CITIES_PAYLOAD,
    "airports": AIRPORTS_PAYLOAD,
    "airlines": AIRLINES_PAYLOAD,
}


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeClient:
    """Serves the canned payloads and records what was asked for."""

    def __init__(self, payloads=None):
        self._payloads = payloads or PAYLOADS
        self.requested: list[str] = []

    def get(self, url, headers=None, params=None):
        self.requested.append(url)
        for name, payload in self._payloads.items():
            if url.endswith(f"{name}.json"):
                return FakeResponse(payload)
        raise AssertionError(f"unexpected url {url}")

    def close(self):
        return None
