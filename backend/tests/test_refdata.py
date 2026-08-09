"""參考資料層的測試。

用寫死的假 payload 餵 loader,不打網路 —— 這些測試要在沒有網路的 CI 上也能跑,
而且要能鎖住兩個已經真的踩到的坑(見 test_bus_stations_never_reach_the_picker)。
"""

import sqlite3

import pytest

from app import refdata
from app.db import SCHEMA, source_health

from tests.fixtures import (
    AIRPORTS_PAYLOAD,
    CITIES_PAYLOAD,
    COUNTRIES_PAYLOAD,
    PAYLOADS,
    FakeClient,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    yield connection
    connection.close()


@pytest.fixture
def loaded(conn):
    client = FakeClient(PAYLOADS)
    refdata.refresh(conn, client=client)
    return conn


class TestLoading:
    def test_refresh_reports_rows_written_per_file(self, conn):
        client = FakeClient(PAYLOADS)
        written = refdata.refresh(conn, client=client)
        assert written == {"countries": 3, "cities": 4, "airports": 8, "airlines": 4}

    def test_refresh_is_idempotent(self, loaded):
        client = FakeClient(PAYLOADS)
        refdata.refresh(loaded, client=client)
        total = loaded.execute("SELECT COUNT(*) FROM airports").fetchone()[0]
        assert total == 8

    def test_airline_codes_get_names(self, loaded):
        """價格資料只給代碼(TW、MM、IT),沒有這張表畫面上就只有兩個字母。

        中文名跟國家、城市一樣自己維護(上游實測只附英文),但英文名要留著 ——
        使用者可能打「長榮」也可能打「EVA」,兩種都得找得到。"""
        row = loaded.execute("SELECT name, name_en FROM airlines WHERE code='IT'").fetchone()
        assert (row["name"], row["name_en"]) == ("台灣虎航", "Tigerair Taiwan")

    def test_an_airline_with_no_chinese_name_keeps_the_english_one(self, loaded):
        """沒收錄就回英文原名,不硬翻 —— 一個亂翻的公司名比英文原名更難認。"""
        row = loaded.execute("SELECT name FROM airlines WHERE code='AA'").fetchone()
        assert row["name"] == "美國航空"
        row = loaded.execute("SELECT name FROM airlines WHERE code='MM'").fetchone()
        assert row["name"] == "樂桃航空"

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

    def test_cities_sort_by_how_likely_you_are_to_go_there(self, loaded):
        """依名稱排序會把大分、小松排在福岡、沖繩前面;只看機場數量則讓離島
        小機場跟主要門戶同級。所以用手寫譯名表的排列順序當熱門度。"""
        cities = refdata.cities_in_country(loaded, "JP")
        assert [c.code for c in cities] == ["TYO", "OSA"]

    def test_popular_destinations_lead_the_country_list(self, loaded):
        countries = refdata.list_countries(loaded)
        assert [c["code"] for c in countries] == ["JP", "TW"]
        # Abkhazia 有國家資料但沒有可飛機場,不該出現。
        assert all(c["code"] != "AB" for c in countries)

    def test_country_list_carries_chinese_names(self, loaded):
        countries = {c["code"]: c["name_zh"] for c in refdata.list_countries(loaded)}
        assert countries["JP"] == "日本"

    def test_untranslated_places_fall_back_to_english(self, conn):
        client = FakeClient(PAYLOADS)
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
