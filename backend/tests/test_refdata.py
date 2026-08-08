"""參考資料層的測試。

用寫死的假 payload 餵 loader,不打網路 —— 這些測試要在沒有網路的 CI 上也能跑,
而且要能鎖住兩個已經真的踩到的坑(見 test_bus_stations_never_reach_the_picker)。
"""

import sqlite3

import pytest

from app import refdata
from app.db import SCHEMA, source_health

# 取自真實 Travelpayouts 回應的節錄,含一筆 flightable=true 的巴士站。
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
        # 這一筆是真的:Travelpayouts 把東京車站前的巴士總站標成 flightable。
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
        # 已停用的機場:iata_type 對,但 flightable 是 false。
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


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    """Serves the canned payloads and records what was asked for."""

    def __init__(self, payloads):
        self._payloads = payloads
        self.requested: list[str] = []

    def get(self, url, headers=None):
        self.requested.append(url)
        for name, payload in self._payloads.items():
            if url.endswith(f"{name}.json"):
                return FakeResponse(payload)
        raise AssertionError(f"unexpected url {url}")


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    yield connection
    connection.close()


@pytest.fixture
def loaded(conn):
    client = FakeClient(
        {
            "countries": COUNTRIES_PAYLOAD,
            "cities": CITIES_PAYLOAD,
            "airports": AIRPORTS_PAYLOAD,
        }
    )
    refdata.refresh(conn, client=client)
    return conn


class TestLoading:
    def test_refresh_reports_rows_written_per_file(self, conn):
        client = FakeClient(
            {
                "countries": COUNTRIES_PAYLOAD,
                "cities": CITIES_PAYLOAD,
                "airports": AIRPORTS_PAYLOAD,
            }
        )
        written = refdata.refresh(conn, client=client)
        assert written == {"countries": 3, "cities": 4, "airports": 8}

    def test_refresh_is_idempotent(self, loaded):
        client = FakeClient(
            {
                "countries": COUNTRIES_PAYLOAD,
                "cities": CITIES_PAYLOAD,
                "airports": AIRPORTS_PAYLOAD,
            }
        )
        refdata.refresh(loaded, client=client)
        total = loaded.execute("SELECT COUNT(*) FROM airports").fetchone()[0]
        assert total == 8

    def test_every_fetch_records_its_row_count(self, loaded):
        health = source_health(loaded)
        assert len(health) == 1
        entry = health[0]
        assert entry["source"] == "travelpayouts-refdata"
        assert entry["empty_calls"] == 0
        assert entry["failed_calls"] == 0
        assert entry["last_nonempty_at"] is not None


class TestPicker:
    def test_bus_stations_never_reach_the_picker(self, loaded):
        """Travelpayouts 把 386 個火車站/巴士站/港口標成 flightable。

        只看 flightable 的話,「Tokyo Bus Station」就會出現在東京的機場選單裡,
        然後被送去查價 —— 查一個不存在的航班。
        """
        tokyo = next(c for c in refdata.cities_in_country(loaded, "JP") if c.code == "TYO")
        assert [a.code for a in tokyo.airports] == ["HND", "NRT"]

    def test_decommissioned_airports_are_excluded(self, loaded):
        codes = {
            airport.code
            for city in refdata.cities_in_country(loaded, "JP")
            for airport in city.airports
        }
        assert "RBJ" not in codes

    def test_multi_airport_cities_sort_first(self, loaded):
        # 機場多的城市才是「替代機場」省錢的來源,擺前面。
        cities = refdata.cities_in_country(loaded, "JP")
        assert [c.code for c in cities] == ["OSA", "TYO"]

    def test_popular_destinations_lead_the_country_list(self, loaded):
        countries = refdata.list_countries(loaded)
        assert [c["code"] for c in countries] == ["JP", "TW"]
        # Abkhazia 有國家資料但沒有可飛機場,不該出現。
        assert all(c["code"] != "AB" for c in countries)

    def test_country_list_carries_chinese_names(self, loaded):
        countries = {c["code"]: c["name_zh"] for c in refdata.list_countries(loaded)}
        assert countries["JP"] == "日本"

    def test_untranslated_places_fall_back_to_english(self, conn):
        client = FakeClient(
            {
                "countries": COUNTRIES_PAYLOAD,
                "cities": CITIES_PAYLOAD,
                "airports": AIRPORTS_PAYLOAD,
            }
        )
        refdata.refresh(conn, client=client)
        haneda = conn.execute("SELECT name_zh FROM airports WHERE code='HND'").fetchone()
        rebun = conn.execute("SELECT name_zh FROM airports WHERE code='RBJ'").fetchone()
        assert haneda["name_zh"] == "羽田"
        assert rebun["name_zh"] == "Rebun"  # 沒收錄就給英文,不硬翻


class TestExpansion:
    def test_a_city_code_expands_to_all_its_airports(self, loaded):
        airports = refdata.expand_airports(loaded, ["TYO"])
        assert [a.code for a in airports] == ["HND", "NRT"]

    def test_an_airport_code_stays_a_single_airport(self, loaded):
        airports = refdata.expand_airports(loaded, ["NRT"])
        assert [a.code for a in airports] == ["NRT"]

    def test_city_and_airport_codes_can_be_mixed(self, loaded):
        airports = refdata.expand_airports(loaded, ["TPE", "KIX"])
        assert [a.code for a in airports] == ["KIX", "TPE", "TSA"]

    def test_expansion_carries_the_city_label_for_display(self, loaded):
        airports = refdata.expand_airports(loaded, ["OSA"])
        assert {a.city_name for a in airports} == {"大阪"}

    def test_unknown_codes_yield_nothing_rather_than_erroring(self, loaded):
        assert refdata.expand_airports(loaded, ["ZZZ"]) == []

    def test_empty_input_is_handled(self, loaded):
        assert refdata.expand_airports(loaded, []) == []
        assert refdata.expand_airports(loaded, ["", "  "]) == []


class TestStaleness:
    def test_empty_database_counts_as_stale(self, conn):
        assert refdata.is_stale(conn) is True

    def test_a_fresh_load_is_not_stale(self, loaded):
        assert refdata.is_stale(loaded) is False
