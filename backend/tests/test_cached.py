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


class TestNearbyDates:
    """「這天查無資料」單看會被讀成「那天沒有飛機」。實際上通常是這條航線只有
    少數幾天被人搜過 —— 實測 ITM→TPE 在 2026-12 只有 3 天有價,同城的 KIX→TPE
    有 28 天。抓價是整月一起抓的,所以那幾天就在手上。
    """

    @pytest.fixture
    def conn(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        now = utcnow().isoformat()
        rows = [("2026-12-02", 6476), ("2026-12-04", 7149), ("2026-12-29", 10871),
                ("2026-10-29", 4548)]
        for day, price in rows:
            connection.execute(
                """INSERT INTO price_cache (origin, destination, depart_date, currency,
                       price, transfers, airline, flight_number, found_at, fetched_at,
                       expires_at, source)
                   VALUES ('ITM','TPE',?,'TWD',?,0,NULL,NULL,NULL,?,?,'test')""",
                (day, price, now, now),
            )
        yield connection
        connection.close()

    def test_the_closest_priced_days_come_back_first(self, conn):
        found = cached.nearest_priced_dates(conn, "ITM", "TPE", date(2026, 12, 13))
        assert [p.depart_date.isoformat() for p in found] == ["2026-12-04", "2026-12-02"]

    def test_days_far_outside_the_window_are_not_offered(self, conn):
        """快取裡還留著上次搜尋別的月份抓的資料。四十天外的日子不是替代方案,
        是雜訊 —— 而且會讓人以為那條航線附近真的有便宜票。"""
        found = cached.nearest_priced_dates(conn, "ITM", "TPE", date(2026, 12, 13))
        assert all(abs((p.depart_date - date(2026, 12, 13)).days) <= 14 for p in found)
        assert "2026-10-29" not in [p.depart_date.isoformat() for p in found]

    def test_a_wider_window_can_be_asked_for(self, conn):
        found = cached.nearest_priced_dates(
            conn, "ITM", "TPE", date(2026, 12, 13), window_days=60, limit=5
        )
        assert "2026-10-29" in [p.depart_date.isoformat() for p in found]

    def test_a_route_with_nothing_nearby_returns_empty(self, conn):
        assert cached.nearest_priced_dates(conn, "KIX", "TPE", date(2026, 12, 13)) == []


class TestGateIsNotAnAirline:
    """`gate` 是訂票網站,`airline` 是航空公司。實測 month-matrix 只給得出前者:
    Kupi.com、Aviakassa、City.Travel 全都出現在 `gate` 欄位裡,而整個端點
    根本沒有航空公司欄位。

    把 gate 當 airline 存,航空公司篩選就會把「Kupi.com」當成一家航空公司端出來,
    而且那個錯誤在畫面上完全合理 —— 它就是一個看起來像公司名的字串。
    """

    def test_month_matrix_leaves_the_airline_unknown(self):
        payload = {
            "data": [
                {"depart_date": "2026-10-05", "return_date": "", "value": 6800,
                 "gate": "Kupi.com"},
            ],
        }
        (found,) = parse_month_payload("month-matrix", payload, "TPE", "NRT", "TWD")
        assert found.gate == "Kupi.com"
        assert found.airline is None

    def test_calendar_really_does_carry_the_carrier(self):
        payload = {
            "data": {
                "2026-10-05": {"price": 6800, "airline": "BR", "flight_number": 189,
                               "departure_at": "2026-10-05T09:00:00+08:00"},
            },
        }
        (found,) = parse_month_payload("calendar", payload, "TPE", "NRT", "TWD")
        assert found.airline == "BR"
        assert found.gate is None


class TestExplainingAMissingPrice:
    """四種空價格,四種下一步。全部併成「查無資料」就是把使用者能做的事一起藏起來。"""

    @pytest.fixture
    def conn(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        now = utcnow().isoformat()

        def price(origin, destination, day, value):
            connection.execute(
                """INSERT INTO price_cache (origin, destination, depart_date, currency,
                       price, transfers, airline, gate, flight_number, found_at,
                       fetched_at, expires_at, source)
                   VALUES (?,?,?,'TWD',?,0,NULL,NULL,NULL,NULL,?,?,'test')""",
                (origin, destination, day, value, now, now),
            )

        def asked(origin, destination, month, rows):
            connection.execute(
                "INSERT INTO route_fetch VALUES (?,?,?,'TWD',?,?,?)",
                (origin, destination, month, rows, now, now),
            )

        # 實測的大阪:同城兩個機場,一個幾乎沒資料,一個滿的。
        asked("ITM", "TPE", "2026-12", 3)
        for day, value in [("2026-12-02", 6476), ("2026-12-04", 7149)]:
            price("ITM", "TPE", day, value)
        asked("KIX", "TPE", "2026-12", 28)
        price("KIX", "TPE", "2026-12-13", 3808)
        # 實測的福岡:整個月一列都沒有。
        asked("FUK", "TPE", "2026-10", 0)
        # 只有遠處有價的航線。
        asked("OKA", "TPE", "2026-12", 10)
        price("OKA", "TPE", "2026-12-30", 3072)
        yield connection
        connection.close()

    def test_a_route_month_nobody_asked_about_says_so(self, conn):
        gap = cached.explain_gap(conn, "CTS", "TPE", date(2026, 12, 13))
        assert gap.reason == "not_fetched"

    def test_a_route_month_with_no_rows_at_all_is_not_the_same_thing(self, conn):
        """查過了、整個月一列都沒有 —— 挪日期沒有用,只能換機場或換月份。
        這正是使用者回報「還是沒拿到價格」時看到的那一格:附近沒有日子可以推薦。"""
        gap = cached.explain_gap(conn, "FUK", "TPE", date(2026, 10, 12))
        assert gap.reason == "route_empty"
        assert gap.nearby == ()

    def test_nearby_days_are_offered_when_they_exist(self, conn):
        gap = cached.explain_gap(conn, "ITM", "TPE", date(2026, 12, 13))
        assert gap.reason == "nearby"
        assert [p.depart_date.isoformat() for p in gap.nearby][:1] == ["2026-12-04"]

    def test_prices_beyond_the_near_window_still_beat_saying_nothing(self, conn):
        """12-30 離 12-13 有十七天,不算「附近」—— 但「這個月有票、要整趟往後挪」
        跟「這條航線根本沒有票」是兩件事,而且使用者要做的事完全不同。"""
        gap = cached.explain_gap(conn, "OKA", "TPE", date(2026, 12, 13))
        assert gap.reason == "far_only"
        assert [p.depart_date.isoformat() for p in gap.nearby] == ["2026-12-30"]

    def test_last_months_leftovers_never_answer_this_months_question(self, conn):
        """快取是跨搜尋共用的:上一次查 10 月留下的票還躺在裡面。放寬距離時不綁月份,
        就會拿 10-30 的票去回答 12-08 的問題,再配上一句寫著「2026-12」的說明 ——
        那不是資訊不足,是直接說錯話。而且兩者要做的事完全不同:挪三天 vs 整趟重排。"""
        now = utcnow().isoformat()
        conn.execute(
            """INSERT INTO price_cache (origin, destination, depart_date, currency,
                   price, transfers, airline, gate, flight_number, found_at,
                   fetched_at, expires_at, source)
               VALUES ('TPE','ITM','2026-10-30','TWD',7051,0,NULL,NULL,NULL,NULL,?,?,'test')""",
            (now, now),
        )
        conn.execute(
            "INSERT INTO route_fetch VALUES ('TPE','ITM','2026-12','TWD',0,?,?)",
            (now, now),
        )
        gap = cached.explain_gap(conn, "TPE", "ITM", date(2026, 12, 8))
        assert gap.reason == "route_empty"
        assert gap.nearby == ()

    def test_the_same_city_next_door_is_an_answer_that_needs_no_date_change(self, conn):
        """伊丹那天沒有價,關西那天有 —— 同一天、同一個城市、換一個機場。
        資料早就在同一次搜尋裡抓回來了,因為城市本來就展開成多個機場。"""
        gap = cached.explain_gap(
            conn, "ITM", "TPE", date(2026, 12, 13), sibling_origins=("ITM", "KIX")
        )
        assert [(p.origin, p.price) for p in gap.same_city] == [("KIX", 3808)]

    def test_the_airport_itself_is_never_offered_as_its_own_alternative(self, conn):
        gap = cached.explain_gap(
            conn, "KIX", "TPE", date(2026, 12, 2), sibling_origins=("ITM", "KIX")
        )
        assert all(p.origin != "KIX" for p in gap.same_city)


class TestOnlyDirectFlights:
    """直達跟航空公司是完全不同的東西:轉機次數我們**真的有資料**
    (`price_cache.transfers`),所以它可以誠實地改變站內顯示的價格。
    """

    @pytest.fixture
    def conn(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        now = utcnow().isoformat()
        rows = [
            ("2026-10-05", 4120, 1),    # 便宜但要轉機
            ("2026-10-06", 5609, 0),    # 貴一點但直達
            ("2026-10-07", 3900, None), # 轉幾次不知道
        ]
        for day, price, transfers in rows:
            connection.execute(
                """INSERT INTO price_cache (origin, destination, depart_date, currency,
                       price, transfers, airline, gate, flight_number, found_at,
                       fetched_at, expires_at, source)
                   VALUES ('TPE','FUK',?,'TWD',?,?,NULL,NULL,NULL,NULL,?,?,'test')""",
                (day, price, transfers, now, now),
            )
        connection.execute(
            "INSERT INTO route_fetch VALUES ('TPE','FUK','2026-10','TWD',3,?,?)",
            (now, now),
        )
        yield connection
        connection.close()

    def test_the_cheaper_connecting_fare_disappears(self, conn):
        """轉機的那筆比較便宜。要直達的人看到 4,120 卻訂不到,比看不到更糟。"""
        full = cached.load_lookup(conn, [("TPE", "FUK")])
        direct = cached.load_lookup(conn, [("TPE", "FUK")], nonstop=True)
        assert len(full) == 3
        assert [p.price for p in direct.values()] == [5609]

    def test_unknown_transfers_is_not_counted_as_direct(self, conn):
        """`transfers IS NULL` 代表我們不知道那一筆轉幾次。把「不知道」算成
        「直達」正是這個站到處在防的那種話 —— 而且它剛好會挑最便宜的那筆。"""
        direct = cached.load_lookup(conn, [("TPE", "FUK")], nonstop=True)
        assert all(p.transfers == 0 for p in direct.values())
        assert 3900 not in [p.price for p in direct.values()]

    def test_nearby_days_respect_it_too(self, conn):
        """建議的替代日期如果混進轉機班,使用者照著改日期還是訂不到直達。"""
        found = cached.nearest_priced_dates(
            conn, "TPE", "FUK", date(2026, 10, 20), limit=5, nonstop=True
        )
        assert [p.depart_date.isoformat() for p in found] == ["2026-10-06"]

    def test_a_day_with_only_connecting_flights_reads_as_no_data(self, conn):
        """10-05 有價,但只有轉機的。要直達的人那天就是沒有 —— 而且理由要說清楚,
        不能只留一個空格。"""
        gap = cached.explain_gap(conn, "TPE", "FUK", date(2026, 10, 5), nonstop=True)
        assert gap.reason in {"nearby", "far_only"}
        assert [p.depart_date.isoformat() for p in gap.nearby] == ["2026-10-06"]

    def test_a_route_with_only_connecting_flights_is_not_called_unsearched(self, conn):
        """實測抓到的:ITM→TPE 2026-12 有三天有價,但三天全是轉機的。
        勾了「只要直達」之後說「整個月一天都沒有快取價 —— 沒人搜過」是**假的**,
        而且方向相反:那句話會叫使用者去重查,但重查一百次也不會多出一班直飛。
        """
        conn.execute(
            "INSERT INTO route_fetch VALUES ('TPE','KIX','2026-10','TWD',1,?,?)",
            (utcnow().isoformat(), utcnow().isoformat()),
        )
        conn.execute(
            """INSERT INTO price_cache (origin, destination, depart_date, currency,
                   price, transfers, airline, gate, flight_number, found_at,
                   fetched_at, expires_at, source)
               VALUES ('TPE','KIX','2026-10-20','TWD',6476,1,NULL,NULL,NULL,NULL,?,?,'test')""",
            (utcnow().isoformat(), utcnow().isoformat()),
        )
        gap = cached.explain_gap(conn, "TPE", "KIX", date(2026, 10, 20), nonstop=True)
        assert gap.reason == "connections_only"

    def test_without_the_filter_that_route_is_simply_priced(self, conn):
        """同一條航線不勾直達就有價 —— 所以那個空格是篩選造成的,不是資料沒有。"""
        conn.execute(
            "INSERT INTO route_fetch VALUES ('TPE','KIX','2026-10','TWD',1,?,?)",
            (utcnow().isoformat(), utcnow().isoformat()),
        )
        conn.execute(
            """INSERT INTO price_cache (origin, destination, depart_date, currency,
                   price, transfers, airline, gate, flight_number, found_at,
                   fetched_at, expires_at, source)
               VALUES ('TPE','KIX','2026-10-20','TWD',6476,1,NULL,NULL,NULL,NULL,?,?,'test')""",
            (utcnow().isoformat(), utcnow().isoformat()),
        )
        assert cached.load_lookup(conn, [("TPE", "KIX")])
        assert not cached.load_lookup(conn, [("TPE", "KIX")], nonstop=True)
