"""API 層與搜尋主線的端到端測試(不打外部網路)。

最重要的一組測試是「沒有 token 時會發生什麼」:答案必須是**明確的警告 + 標成
查無資料的組合**,而不是一個空的結果清單。空清單會被讀成「這條路線沒有便宜票」,
那是我們沒有根據的斷言。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import refdata
from app.db import closing_conn, init_db, utcnow
from app.main import app
from tests.fixtures import FakeClient


@pytest.fixture(scope="module")
def client():
    init_db()
    with closing_conn() as conn:
        refdata.refresh(conn, client=FakeClient())
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def priced(client):
    """Seed the price cache so the ranking path can be exercised offline."""
    now = utcnow()
    expires = now + timedelta(hours=6)
    # 回程日期由停留天數決定,所以每種行程骨架回來的日子不一樣:
    #   開口(東京 3 晚 + 大阪 2 晚) → 10-10
    #   基準・只玩東京(3 晚)        → 10-08
    #   基準・只玩大阪(2 晚)        → 10-07
    rows = [
        ("TPE", "NRT", "2026-10-05", 6800),
        ("TPE", "HND", "2026-10-05", 7900),
        ("TPE", "KIX", "2026-10-05", 6200),
        ("TPE", "ITM", "2026-10-05", 9900),
        ("NRT", "TPE", "2026-10-10", 6400),
        ("HND", "TPE", "2026-10-10", 7100),
        ("KIX", "TPE", "2026-10-10", 5100),
        ("ITM", "TPE", "2026-10-10", 8800),
        ("NRT", "TPE", "2026-10-08", 7300),
        ("HND", "TPE", "2026-10-08", 8100),
        ("KIX", "TPE", "2026-10-07", 6900),
        ("ITM", "TPE", "2026-10-07", 9400),
    ]
    with closing_conn() as conn:
        conn.execute("DELETE FROM price_cache")
        conn.execute("DELETE FROM route_fetch")
        for origin, destination, day, price in rows:
            conn.execute(
                """INSERT INTO price_cache (origin, destination, depart_date, currency,
                       price, transfers, airline, flight_number, found_at, fetched_at,
                       expires_at, source)
                   VALUES (?,?,?,'TWD',?,0,'BR',NULL,NULL,?,?,'test')""",
                (origin, destination, day, price, now.isoformat(), expires.isoformat()),
            )
        for origin, destination, _, _ in rows:
            conn.execute(
                """INSERT OR REPLACE INTO route_fetch
                       (origin, destination, month, currency, row_count, fetched_at, expires_at)
                   VALUES (?,?, '2026-10', 'TWD', 31, ?, ?)""",
                (origin, destination, now.isoformat(), expires.isoformat()),
            )
        # 大阪伊丹的回程刻意留成「查過、零筆」,用來驗證查無資料的呈現。
        conn.execute(
            """INSERT OR REPLACE INTO route_fetch
                   (origin, destination, month, currency, row_count, fetched_at, expires_at)
               VALUES ('ITM','TSA','2026-10','TWD', 0, ?, ?)""",
            (now.isoformat(), expires.isoformat()),
        )
        conn.commit()
    return rows


JAPAN_SEARCH = {
    "home": ["TPE"],
    "stops": [
        {"codes": ["TYO"], "nights_min": 3, "nights_max": 3},
        {"codes": ["OSA"], "nights_min": 2, "nights_max": 2},
    ],
    "depart_earliest": "2026-10-05",
    "depart_latest": "2026-10-05",
}


class TestReferenceEndpoints:
    def test_country_list_leads_with_popular_destinations(self, client):
        body = client.get("/api/ref/countries").json()
        assert [c["code"] for c in body["countries"]] == ["JP", "TW"]

    def test_airport_tree_groups_by_city(self, client):
        body = client.get("/api/ref/countries/JP/airports").json()
        cities = {c["code"]: [a["code"] for a in c["airports"]] for c in body["cities"]}
        assert cities["TYO"] == ["HND", "NRT"]
        assert cities["OSA"] == ["ITM", "KIX"]

    def test_unknown_country_is_a_404(self, client):
        assert client.get("/api/ref/countries/ZZ/airports").status_code == 404


class TestSearch:
    def test_open_jaw_and_baselines_come_back_separately(self, client, priced):
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        shapes = {r["shape"] for r in body["results"]}
        baselines = {b["shape"] for b in body["baselines"]}

        assert "台北→東京〜大阪→台北" in shapes
        assert "台北→大阪〜東京→台北" in shapes
        # 單城來回是「基準」,不跟多城組合同列競爭 —— 看到的城市數不一樣。
        assert baselines == {"台北→東京→台北", "台北→大阪→台北"}

    def test_results_are_cheapest_first(self, client, priced):
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        totals = [r["split_total"] for r in body["results"]]
        assert totals == sorted(totals)

    def test_every_price_carries_its_source_and_age(self, client, priced):
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        first = body["results"][0]
        assert first["price_source"] == "cache"
        assert first["oldest_fetch"] is not None
        for leg in first["legs"]:
            assert leg["status"] == "ok"
            assert leg["age_hours"] is not None

    def test_savings_are_measured_against_the_cheapest_single_city_trip(self, client, priced):
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        baseline = min(b["split_total"] for b in body["baselines"])
        best = body["results"][0]
        assert best["savings_vs_baseline"] == pytest.approx(baseline - best["split_total"])

    def test_surface_hop_is_flagged_as_a_risk(self, client, priced):
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        risks = " ".join(body["results"][0]["risks"])
        assert "自己移動" in risks
        assert "行李不直掛" in risks

    def test_every_result_carries_booking_links(self, client, priced):
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        links = body["results"][0]["links"]
        assert links["single_ticket"]["google_flights"].startswith(
            "https://www.google.com/travel/flights?tfs="
        )
        assert len(links["split"]) == 2

    def test_the_search_reports_what_it_will_cost_in_api_calls(self, client, priced):
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        cost = body["cost"]
        assert cost["route_pairs"] > 0
        assert cost["api_calls"] == cost["route_pairs"] * len(cost["months"])


class TestHonestGaps:
    def test_missing_token_warns_instead_of_returning_an_empty_page(self, client):
        """沒有 token 時,答案是「我不知道」,不是「沒有便宜的」。"""
        with closing_conn() as conn:
            conn.execute("DELETE FROM price_cache")
            conn.execute("DELETE FROM route_fetch")
            conn.commit()
        body = client.post("/api/search", json=JAPAN_SEARCH).json()

        assert body["results"] == []
        assert body["unpriceable"], "查無價格的組合必須被回傳,不能靜默消失"
        assert any("Travelpayouts token" in w for w in body["warnings"])
        assert all(
            leg["status"] == "not_fetched"
            for combo in body["unpriceable"]
            for leg in combo["legs"]
        )

    def test_routes_that_returned_nothing_are_named_in_the_warnings(self, client, priced):
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        assert any("查無任何價格" in w for w in body["warnings"])


class TestPerRequestCredentials:
    """標頭帶進來的金鑰只影響這一次請求,不會被存起來 —— 拿另一組額度試一次
    這種需求仍然要能用。"""

    def test_a_token_in_the_header_silences_the_no_token_warning(self, client):
        body = client.post(
            "/api/search", json=JAPAN_SEARCH,
            headers={"X-Travelpayouts-Token": "supplied-by-the-browser"},
        ).json()
        assert not any("Travelpayouts token" in w for w in body["warnings"])

    def test_without_a_token_the_warning_names_where_to_put_one(self, client):
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        assert any("Travelpayouts token" in w for w in body["warnings"])

    def test_the_marker_reaches_the_affiliate_links(self, client, priced):
        body = client.post(
            "/api/search", json=JAPAN_SEARCH,
            headers={"X-Travelpayouts-Marker": "654321"},
        ).json()
        aviasales = body["results"][0]["links"]["single_ticket"]["aviasales"]
        assert aviasales.endswith("?marker=654321")

    def test_no_marker_means_a_plain_link_rather_than_a_broken_one(self, client, priced):
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        assert "marker=" not in body["results"][0]["links"]["single_ticket"]["aviasales"]

    def test_the_marker_reaches_the_per_leg_links_too(self, client, priced):
        body = client.post(
            "/api/search", json=JAPAN_SEARCH,
            headers={"X-Travelpayouts-Marker": "654321"},
        ).json()
        for leg in body["results"][0]["links"]["split"]:
            assert leg["aviasales"].endswith("?marker=654321")


class TestPayloadSize:
    def test_a_realistic_search_reports_thousands_but_ships_dozens(self, client, priced):
        """排名涵蓋全部,序列化只做前 N 筆。

        每一列帶九個深連結、其中三個要編 protobuf。真實的 14 天視窗會產生數千
        種組合,全部序列化會做出幾萬個 URL 和一份瀏覽器算不動的 JSON。
        """
        payload = dict(
            JAPAN_SEARCH,
            depart_earliest="2026-10-01",
            depart_latest="2026-10-14",
            stops=[
                {"codes": ["TYO"], "nights_min": 2, "nights_max": 4},
                {"codes": ["OSA"], "nights_min": 2, "nights_max": 4},
            ],
        )
        body = client.post("/api/search", json=payload).json()

        assert body["counts"]["combinations"] > 1000
        assert len(body["results"]) <= 50
        assert len(body["unpriceable"]) <= 10
        # 被截掉的部分要有數字交代,不是靜靜消失。
        assert body["counts"]["unpriceable"] >= len(body["unpriceable"])

    def test_counts_describe_the_full_ranking_not_the_visible_slice(self, client, priced):
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        counts = body["counts"]
        assert counts["combinations"] == (
            counts["priced"] + counts["baselines"] + counts["unpriceable"]
        )
        assert counts["shown"] == len(body["results"])


class TestWarm:
    def test_warm_reports_missing_token_rather_than_failing(self, client):
        response = client.post("/api/search/warm", json=JAPAN_SEARCH)
        assert response.status_code == 200
        assert any("Travelpayouts token" in w for w in response.json()["warnings"])

    def test_warm_is_bounded_by_the_same_guardrails(self, client):
        payload = dict(JAPAN_SEARCH, depart_earliest="2026-10-01", depart_latest="2026-12-01")
        assert client.post("/api/search/warm", json=payload).status_code == 400


class TestRiskLabels:
    def test_a_same_day_surface_hop_is_not_called_a_missed_connection(self, client, priced):
        """東京到大阪搭新幹線不是「當天轉機」—— 那班飛機根本不存在。"""
        # 兩站都住 0 晚 = 當天飛進東京、搭車到大阪、當天飛出去。
        payload = dict(
            JAPAN_SEARCH,
            stops=[
                {"codes": ["TYO"], "nights_min": 0, "nights_max": 0},
                {"codes": ["OSA"], "nights_min": 0, "nights_max": 0},
            ],
        )
        body = client.post("/api/search", json=payload).json()
        combo = (body["results"] or body["unpriceable"])[0]
        risks = " ".join(combo["risks"])
        assert combo["depart_date"] == combo["return_date"], "這個測試要的是同一天的情境"
        assert "當天陸路移動" in risks
        assert "當天轉機" not in risks

    def test_an_overnight_surface_hop_is_labelled_plainly(self, client, priced):
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        risks = " ".join((body["results"] or body["unpriceable"])[0]["risks"])
        assert "之間要自己移動(陸路)" in risks


class TestGuardrails:
    def test_an_oversized_window_says_which_knob_to_turn(self, client):
        payload = dict(JAPAN_SEARCH, depart_earliest="2026-10-01", depart_latest="2026-12-01")
        response = client.post("/api/search", json=payload)
        assert response.status_code == 400
        assert response.json()["detail"]["offender"] == "depart_window"

    def test_an_unknown_place_is_rejected_with_the_code(self, client):
        payload = dict(JAPAN_SEARCH, home=["ZZZ"])
        response = client.post("/api/search", json=payload)
        assert response.status_code == 400
        assert "ZZZ" in response.json()["detail"]

    def test_a_backwards_date_window_is_rejected(self, client):
        payload = dict(JAPAN_SEARCH, depart_earliest="2026-10-10", depart_latest="2026-10-01")
        assert client.post("/api/search", json=payload).status_code == 400


class TestVerify:
    def test_without_a_live_provider_it_still_returns_links_and_a_reason(self, client):
        response = client.post(
            "/api/verify",
            json={
                "legs": [
                    {"origin": "TPE", "destination": "NRT", "date": "2026-10-05"},
                    {"origin": "ITM", "destination": "TPE", "date": "2026-10-10"},
                ]
            },
        )
        body = response.json()
        assert body["provider"] == "deeplink"
        assert body["single_ticket"]["total"] is None
        # 空值一定伴隨理由 —— 沒有解釋的空格會被讀成「很貴」。
        assert body["single_ticket"]["unavailable_reason"]
        assert body["links"]["single_ticket"]["kayak"]


class TestHealth:
    def test_health_reports_row_counts_not_just_a_status(self, client):
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["row_counts"]["airports"] > 0

    def test_health_shows_which_pricing_layers_are_configured(self, client):
        body = client.get("/api/health").json()
        assert body["config"]["live_provider"] == "deeplink"
        assert body["config"]["cached_prices"] is False

    def test_health_tracks_when_each_source_last_returned_anything(self, client):
        """`last_success_at` 一直前進但 `last_nonempty_at` 卡住,就是壞掉的樣子。"""
        sources = {s["source"]: s for s in client.get("/api/health").json()["sources"]}
        assert sources["travelpayouts-refdata"]["last_nonempty_at"] is not None


class TestStoredKeys:
    """金鑰改存伺服器。原本只存瀏覽器是因為站台公開;站台上鎖之後那個理由消失,
    而「在網頁上按了儲存卻只有那台瀏覽器算數」對使用者來說跟壞掉沒兩樣。"""

    def test_nothing_stored_initially(self, client):
        client.delete("/api/keys")
        body = client.get("/api/keys").json()
        assert body["configured"] is False
        assert body["source"] == "none"

    def test_saving_makes_it_stick(self, client):
        client.put("/api/keys", json={"token": "stored-token-1234", "marker": "559947"})
        body = client.get("/api/keys").json()
        assert body["configured"] is True
        assert body["source"] == "saved"
        assert body["marker"] == "559947"
        client.delete("/api/keys")

    def test_the_token_itself_is_never_returned(self, client):
        client.put("/api/keys", json={"token": "stored-token-1234", "marker": ""})
        body = client.get("/api/keys").json()
        assert "stored-token-1234" not in str(body)
        assert body["masked_token"] == "stor…1234"
        client.delete("/api/keys")

    def test_a_stored_key_silences_the_no_token_warning(self, client):
        client.put("/api/keys", json={"token": "stored-token-1234", "marker": ""})
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        assert not any("Travelpayouts token" in w for w in body["warnings"])
        client.delete("/api/keys")

    def test_a_stored_marker_reaches_the_links(self, client, priced):
        client.put("/api/keys", json={"token": "stored-token-1234", "marker": "654321"})
        body = client.post("/api/search", json=JAPAN_SEARCH).json()
        assert body["results"][0]["links"]["single_ticket"]["aviasales"].endswith(
            "?marker=654321"
        )
        client.delete("/api/keys")

    def test_a_request_header_overrides_the_stored_key(self, client, priced):
        """一次性覆寫仍然要能用 —— 例如想拿另一組額度試一次。"""
        client.put("/api/keys", json={"token": "stored-token-1234", "marker": "111111"})
        body = client.post(
            "/api/search", json=JAPAN_SEARCH,
            headers={"X-Travelpayouts-Token": "one-off", "X-Travelpayouts-Marker": "222222"},
        ).json()
        assert body["results"][0]["links"]["single_ticket"]["aviasales"].endswith(
            "?marker=222222"
        )
        client.delete("/api/keys")

    def test_clearing_removes_it(self, client):
        client.put("/api/keys", json={"token": "stored-token-1234", "marker": "1"})
        client.delete("/api/keys")
        assert client.get("/api/keys").json()["configured"] is False

    def test_health_reports_where_the_key_came_from(self, client):
        client.put("/api/keys", json={"token": "stored-token-1234", "marker": ""})
        config = client.get("/api/health").json()["config"]
        assert config["cached_prices"] is True
        assert config["key_source"] == "saved"
        client.delete("/api/keys")


class TestAirlinePicker:
    """使用者選了航空公司之後,唯一會變的是連結,排名一個字都不動。

    這條線刻意畫得死:排名用的 month-matrix 根本沒有航空公司欄位,拿它去篩會
    篩掉航線卻篩不掉價格 —— 顯示的數字根本不是那家的,那比沒有篩選更會誤導。
    """

    def test_the_picker_list_leads_with_carriers_a_taipei_traveller_would_name(self, client):
        """千百家航空公司照字母排,長榮會在很後面。台北出發的人要找的那幾家
        排在前面,選單才有人用得下去。"""
        codes = [a["code"] for a in client.get("/api/ref/airlines").json()["airlines"]]
        assert set(codes[:3]) == {"BR", "IT", "MM"}
        assert codes.index("AA") > codes.index("BR")

    def test_the_list_can_be_searched(self, client):
        body = client.get("/api/ref/airlines?q=EVA").json()
        assert any(a["code"] == "BR" for a in body["airlines"])

    def test_picking_an_airline_only_changes_the_google_link(self, client, priced):
        plain = client.post("/api/search", json=JAPAN_SEARCH).json()["results"][0]
        picked = client.post(
            "/api/search", json={**JAPAN_SEARCH, "airlines": ["BR", "CI"]}
        ).json()["results"][0]

        assert picked["split_total"] == plain["split_total"]
        assert picked["links"]["single_ticket"]["kayak"] == plain["links"]["single_ticket"]["kayak"]
        assert (
            picked["links"]["single_ticket"]["google_flights"]
            != plain["links"]["single_ticket"]["google_flights"]
        )

    def test_the_filter_reaches_the_per_leg_links_too(self, client, priced):
        """拼票是逐段各買一張。篩選只套到「整趟一張票」那條連結,
        點分段連結的人就會看到自己沒選的航空公司。"""
        import base64

        picked = client.post(
            "/api/search", json={**JAPAN_SEARCH, "airlines": ["BR"]}
        ).json()["results"][0]
        for leg in picked["links"]["split"]:
            tfs = leg["google_flights"].split("tfs=")[1].split("&")[0]
            assert b"BR" in base64.urlsafe_b64decode(tfs + "=" * (-len(tfs) % 4))


class TestAGapExplainsItself:
    def test_a_missing_leg_says_which_of_the_four_situations_it_is(self, client, priced):
        body = client.post("/api/search", json={**JAPAN_SEARCH, "home": ["TPE", "TSA"]}).json()
        legs = [
            leg
            for combo in body["unpriceable"]
            for leg in combo["legs"]
            if leg["status"] != "ok"
        ]
        assert legs, "這組固定資料本來就留了查無資料的航段"
        for leg in legs:
            assert leg["gap"]["reason"] in {
                "not_fetched", "route_empty", "nearby", "far_only"
            }
            assert leg["gap"]["text"]

    def test_a_route_month_that_came_back_empty_is_not_called_never_asked(
        self, client, priced
    ):
        """ITM→TSA 在固定資料裡是「查過、零筆」。說成「還沒查過」會叫使用者
        去按一個按鈕,而那個按鈕做完之後畫面完全不會變。"""
        body = client.post("/api/search", json={**JAPAN_SEARCH, "home": ["TPE", "TSA"]}).json()
        gaps_ = [
            leg["gap"]
            for combo in body["unpriceable"]
            for leg in combo["legs"]
            if leg["origin"] == "ITM" and leg["destination"] == "TSA"
        ]
        assert gaps_
        assert all(g["reason"] != "not_fetched" for g in gaps_)


REVERSE_TRIPS = {
    "home": ["TPE"],
    "first": {"codes": ["TYO"], "depart": "2026-10-05", "back": "2026-10-10"},
    "second": {"codes": ["OSA"], "depart": "2026-12-06", "back": "2026-12-13"},
    "try_both_orders": False,
    "warm": False,
}


class TestReverseModeExplainsItsGapsToo:
    """「附近有價的日子」第一次只做進了單趟搜尋,倒買法漏掉,使用者看到的就是
    一句沒有下文的「查無資料」。序列化因此收在同一個地方。"""

    def test_an_unpriced_leg_carries_a_reason(self, client, priced):
        body = client.post("/api/reverse", json=REVERSE_TRIPS).json()
        alternatives = [
            alt
            for plan in body["groups"][0]["plans"]
            for ticket in plan["tickets"]
            if ticket.get("pricing")
            for alt in ticket["pricing"]["alternatives"]
        ]
        assert alternatives
        for alt in alternatives:
            assert alt["gap"]["reason"] in {
                "not_fetched", "route_empty", "nearby", "far_only"
            }
            assert alt["leg"]

    def test_the_airline_choice_reaches_the_reverse_tickets(self, client, priced):
        import base64

        body = client.post(
            "/api/reverse", json={**REVERSE_TRIPS, "airlines": ["BR"]}
        ).json()
        for plan in body["groups"][0]["plans"]:
            for ticket in plan["tickets"]:
                tfs = ticket["links"]["google_flights"].split("tfs=")[1].split("&")[0]
                assert b"BR" in base64.urlsafe_b64decode(tfs + "=" * (-len(tfs) % 4))
