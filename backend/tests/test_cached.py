"""層一(快取價)的解析與計價測試。

重點全部集中在「不知道」與「不便宜」的分野上 —— 這是這個工具最容易誤導人的地方。
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from app.combos import Combo, FlightLeg
from app.db import SCHEMA, utcnow
from app.pricing import cached
from app.pricing.cached import ComboPricing, LegPricing, PricePoint, parse_month_payload

OUTBOUND = FlightLeg("TPE", "NRT", date(2026, 10, 5), "台北", "東京")
INBOUND = FlightLeg("ITM", "TPE", date(2026, 10, 11), "大阪", "台北")
COMBO = Combo(legs=(OUTBOUND, INBOUND), shape_label="台北→東京〜大阪→台北", is_baseline=False)


def point(origin, destination, day, price, fetched_at=None) -> PricePoint:
    return PricePoint(
        origin=origin,
        destination=destination,
        depart_date=day,
        price=price,
        currency="TWD",
        transfers=0,
        airline="BR",
        flight_number=None,
        found_at=None,
        fetched_at=fetched_at or utcnow(),
    )


class TestMonthMatrixParsing:
    def test_round_trip_rows_are_discarded(self):
        """帶回程日期的那一列是來回票價。

        把它當單程價加總,拼票總價就會把回家那段算兩次 —— 而且結果看起來
        完全合理,不會有任何一個地方報錯。
        """
        payload = {
            "success": True,
            "data": [
                {"origin": "TPE", "destination": "NRT", "depart_date": "2026-10-05",
                 "return_date": "", "value": 6800, "number_of_changes": 0},
                {"origin": "TPE", "destination": "NRT", "depart_date": "2026-10-05",
                 "return_date": "2026-10-12", "value": 11200, "number_of_changes": 0},
            ],
        }
        points = parse_month_payload("month-matrix", payload, "TPE", "NRT", "TWD")
        assert [p.price for p in points] == [6800]

    def test_cheapest_wins_when_a_day_repeats(self):
        payload = {
            "data": [
                {"depart_date": "2026-10-05", "return_date": "", "value": 9000},
                {"depart_date": "2026-10-05", "return_date": "", "value": 6800},
                {"depart_date": "2026-10-06", "return_date": "", "value": 7100},
            ]
        }
        points = parse_month_payload("month-matrix", payload, "TPE", "NRT", "TWD")
        assert [(p.depart_date.day, p.price) for p in points] == [(5, 6800), (6, 7100)]

    def test_rows_without_a_price_are_skipped(self):
        payload = {"data": [{"depart_date": "2026-10-05", "return_date": "", "value": None}]}
        assert parse_month_payload("month-matrix", payload, "TPE", "NRT", "TWD") == []

    def test_empty_response_is_not_an_error(self):
        assert parse_month_payload("month-matrix", {"success": True, "data": []},
                                   "TPE", "NRT", "TWD") == []

    def test_transfer_count_is_kept(self):
        payload = {"data": [{"depart_date": "2026-10-05", "return_date": "",
                             "value": 5200, "number_of_changes": 1}]}
        points = parse_month_payload("month-matrix", payload, "TPE", "NRT", "TWD")
        assert points[0].transfers == 1


class TestCalendarParsing:
    def test_daily_map_is_flattened(self):
        payload = {
            "data": {
                "2026-10-05": {"price": 6800, "airline": "BR", "flight_number": 198,
                               "departure_at": "2026-10-05T09:00:00Z"},
                "2026-10-06": {"price": 7100, "airline": "JX",
                               "departure_at": "2026-10-06T09:00:00Z"},
            }
        }
        points = parse_month_payload("calendar", payload, "TPE", "NRT", "TWD")
        assert [(p.depart_date.day, p.price, p.airline) for p in points] == [
            (5, 6800.0, "BR"),
            (6, 7100.0, "JX"),
        ]

    def test_entries_carrying_a_return_leg_are_discarded(self):
        payload = {
            "data": {
                "2026-10-05": {"price": 11200, "return_at": "2026-10-12T09:00:00Z",
                               "departure_at": "2026-10-05T09:00:00Z"},
            }
        }
        assert parse_month_payload("calendar", payload, "TPE", "NRT", "TWD") == []

    def test_unknown_endpoint_is_rejected_loudly(self):
        with pytest.raises(ValueError):
            parse_month_payload("guesswork", {"data": []}, "TPE", "NRT", "TWD")


class TestComboPricing:
    def test_complete_combination_sums_its_legs(self):
        lookup = {
            ("TPE", "NRT", date(2026, 10, 5)): point("TPE", "NRT", date(2026, 10, 5), 6800),
            ("ITM", "TPE", date(2026, 10, 11)): point("ITM", "TPE", date(2026, 10, 11), 5400),
        }
        pricing = cached.price_combo(COMBO, lookup)
        assert pricing.is_complete
        assert pricing.total == 12200

    def test_incomplete_combination_has_no_total_rather_than_a_partial_one(self):
        """只加總「剛好有資料」的那幾段,會讓殘缺的行程看起來最便宜。"""
        lookup = {("TPE", "NRT", date(2026, 10, 5)): point("TPE", "NRT", date(2026, 10, 5), 6800)}
        pricing = cached.price_combo(COMBO, lookup)
        assert pricing.total is None
        assert pricing.missing_legs == (INBOUND,)

    def test_asked_and_got_nothing_is_labelled_no_data(self):
        fetched = {("ITM", "TPE", "2026-10"): 0}
        pricing = cached.price_combo(COMBO, {}, fetched)
        statuses = {leg.leg.origin: leg.status for leg in pricing.legs}
        assert statuses["ITM"] == "no_data"

    def test_never_asked_is_labelled_not_fetched(self):
        pricing = cached.price_combo(COMBO, {}, {})
        assert {leg.status for leg in pricing.legs} == {"not_fetched"}

    def test_age_comes_from_the_stalest_leg(self):
        old = utcnow() - timedelta(hours=5)
        lookup = {
            ("TPE", "NRT", date(2026, 10, 5)): point("TPE", "NRT", date(2026, 10, 5), 6800, old),
            ("ITM", "TPE", date(2026, 10, 11)): point("ITM", "TPE", date(2026, 10, 11), 5400),
        }
        pricing = cached.price_combo(COMBO, lookup)
        assert pricing.oldest_fetch == old


class TestRanking:
    def _pricing(self, total: float | None) -> ComboPricing:
        if total is None:
            return ComboPricing(combo=COMBO, legs=(LegPricing(OUTBOUND, "no_data"),))
        return ComboPricing(
            combo=COMBO,
            legs=(LegPricing(OUTBOUND, "ok", point("TPE", "NRT", date(2026, 10, 5), total)),),
        )

    def test_cheapest_first(self):
        priced, _ = cached.rank([self._pricing(9000), self._pricing(6800), self._pricing(7500)])
        assert [p.total for p in priced] == [6800, 7500, 9000]

    def test_unpriceable_combinations_are_returned_not_dropped(self):
        """丟掉查無資料的組合,等於告訴使用者「這裡沒有更便宜的」—— 那是猜的。"""
        priced, unpriced = cached.rank([self._pricing(6800), self._pricing(None)])
        assert len(priced) == 1
        assert len(unpriced) == 1


class TestFreshness:
    @pytest.fixture
    def conn(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        yield connection
        connection.close()

    def test_a_route_never_fetched_is_not_fresh(self, conn):
        assert cached._is_fresh(conn, "TPE", "NRT", "2026-10", "TWD") is False

    def test_an_expired_fetch_is_not_fresh(self, conn):
        past = (utcnow() - timedelta(hours=1)).isoformat()
        conn.execute(
            "INSERT INTO route_fetch VALUES (?,?,?,?,?,?,?)",
            ("TPE", "NRT", "2026-10", "TWD", 30, past, past),
        )
        assert cached._is_fresh(conn, "TPE", "NRT", "2026-10", "TWD") is False

    def test_a_recent_fetch_is_fresh_even_when_it_found_nothing(self, conn):
        """列數 0 也算「查過了」。否則每次搜尋都會重打一次冷門航線。"""
        now = utcnow().isoformat()
        future = (utcnow() + timedelta(hours=6)).isoformat()
        conn.execute(
            "INSERT INTO route_fetch VALUES (?,?,?,?,?,?,?)",
            ("ITM", "TPE", "2026-10", "TWD", 0, now, future),
        )
        assert cached._is_fresh(conn, "ITM", "TPE", "2026-10", "TWD") is True
        assert cached._cached_row_count(conn, "ITM", "TPE", "2026-10", "TWD") == 0

    def test_missing_token_raises_rather_than_returning_nothing(self, conn):
        with pytest.raises(cached.MissingToken):
            cached.ensure_routes(conn, [("TPE", "NRT")], ["2026-10"])


class TestMonthCoverage:
    def test_a_range_spanning_two_months_needs_both(self):
        dates = [date(2026, 10, 30), date(2026, 11, 2)]
        assert cached.months_covering(dates) == ["2026-10", "2026-11"]

    def test_duplicate_months_collapse(self):
        dates = [date(2026, 10, 1), date(2026, 10, 20)]
        assert cached.months_covering(dates) == ["2026-10"]


class TestAirportCodesSurviveTheRoundTrip:
    """Aviasales 用**城市碼**回答機場層級的查詢:問 KIX 回 `destination: "OSA"`、
    問 NRT 回 `"TYO"`。但資料確實是分機場的 —— 同一個月 TPE→NRT 與 TPE→HND
    沒有一筆價格相同。

    照抄回傳的城市碼當索引,查詢時用機場碼就永遠對不上,整站會在坐擁資料的情況下
    顯示「此航段查無資料」。
    """

    def _payload(self, echoed_destination):
        return {
            "data": [
                {"origin": "TPE", "destination": echoed_destination,
                 "depart_date": "2026-10-01", "return_date": "", "value": 3471,
                 "number_of_changes": 0}
            ]
        }

    def test_the_requested_airport_is_what_gets_stored(self):
        points = parse_month_payload("month-matrix", self._payload("OSA"), "TPE", "KIX", "TWD")
        assert points[0].destination == "KIX"

    def test_the_echoed_city_code_is_ignored(self):
        points = parse_month_payload("month-matrix", self._payload("TYO"), "TPE", "NRT", "TWD")
        assert points[0].destination == "NRT"
        assert points[0].origin == "TPE"

    def test_a_secondary_origin_airport_is_kept(self):
        """問 TSA 也會被回成 TPE —— 松山和桃園是不同的候選,不能被併掉。"""
        payload = {
            "data": [
                {"origin": "TPE", "destination": "TYO", "depart_date": "2026-10-01",
                 "return_date": "", "value": 4801, "number_of_changes": 0}
            ]
        }
        points = parse_month_payload("month-matrix", payload, "TSA", "NRT", "TWD")
        assert (points[0].origin, points[0].destination) == ("TSA", "NRT")

    def test_prices_are_findable_by_the_code_the_search_uses(self):
        points = parse_month_payload("month-matrix", self._payload("OSA"), "TPE", "KIX", "TWD")
        lookup = {(p.origin, p.destination, p.depart_date): p for p in points}
        assert ("TPE", "KIX", date(2026, 10, 1)) in lookup


class TestAirportCodesSurviveTheRoundTrip:
    """Aviasales 用**城市碼**回答機場層級的查詢:問 KIX 回 `destination: "OSA"`、
    問 NRT 回 `"TYO"`、問 TSA 回 `origin: "TPE"`。

    但資料確實是分機場的 —— 實測 2026-10 整個月,TPE→NRT 與 TPE→HND 各 29 列、
    交集 0;TPE→KIX 30 列與 TPE→ITM 14 列也交集 0。

    照抄回傳的城市碼當索引,查詢時用機場碼就永遠對不上,整站會在坐擁資料的情況下
    顯示「此航段查無資料」。
    """

    def _payload(self, echoed_origin, echoed_destination):
        return {
            "data": [
                {"origin": echoed_origin, "destination": echoed_destination,
                 "depart_date": "2026-10-01", "return_date": "", "value": 3471,
                 "number_of_changes": 0}
            ]
        }

    def test_the_requested_airport_is_what_gets_stored(self):
        points = parse_month_payload(
            "month-matrix", self._payload("TPE", "OSA"), "TPE", "KIX", "TWD"
        )
        assert points[0].destination == "KIX"

    def test_the_echoed_city_code_is_ignored(self):
        points = parse_month_payload(
            "month-matrix", self._payload("TPE", "TYO"), "TPE", "NRT", "TWD"
        )
        assert (points[0].origin, points[0].destination) == ("TPE", "NRT")

    def test_a_secondary_origin_airport_is_not_collapsed(self):
        """問 TSA 也會被回成 TPE。松山跟桃園是兩個不同的候選,不能被併掉。"""
        points = parse_month_payload(
            "month-matrix", self._payload("TPE", "TYO"), "TSA", "NRT", "TWD"
        )
        assert points[0].origin == "TSA"

    def test_the_calendar_endpoint_behaves_the_same(self):
        payload = {
            "data": {
                "2026-10-01": {"price": 3471, "origin": "TPE", "destination": "OSA",
                               "departure_at": "2026-10-01T09:00:00Z"}
            }
        }
        points = parse_month_payload("calendar", payload, "TPE", "KIX", "TWD")
        assert points[0].destination == "KIX"

    def test_prices_are_findable_by_the_code_the_search_uses(self):
        """這是這組測試真正在守的東西:存進去之後,查得回來。"""
        points = parse_month_payload(
            "month-matrix", self._payload("TPE", "OSA"), "TPE", "KIX", "TWD"
        )
        lookup = {(p.origin, p.destination, p.depart_date): p for p in points}
        assert ("TPE", "KIX", date(2026, 10, 1)) in lookup
