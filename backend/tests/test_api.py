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

    @pytest.mark.parametrize("query", ["長榮", "EVA", "BR"])
    def test_the_list_can_be_searched_in_either_language(self, client, query):
        """同一個人可能打「長榮」、「EVA」或「BR」,取決於他上一次在哪裡看到那個名字。
        只比對其中一種,另外兩種就會回「找不到」—— 而那家公司明明就在清單裡。"""
        body = client.get(f"/api/ref/airlines?q={query}").json()
        assert any(a["code"] == "BR" for a in body["airlines"])

    def test_the_default_list_is_not_padded_with_airlines_nobody_wants(self, client):
        """全球一千多家,拿字母序補滿版面只會在長榮旁邊放一家沒人聽過的區域航空。"""
        codes = [a["code"] for a in client.get("/api/ref/airlines").json()["airlines"]]
        assert "AA" in codes          # 收錄過的照樣在
        assert "2B" not in codes      # 沒收錄的不靠字母序擠進來

    def test_picking_an_airline_changes_only_the_verified_links(self, client, priced):
        plain = client.post("/api/search", json=JAPAN_SEARCH).json()["results"][0]
        picked = client.post(
            "/api/search", json={**JAPAN_SEARCH, "airlines": ["BR", "CI"]}
        ).json()["results"][0]

        assert picked["split_total"] == plain["split_total"]
        # Aviasales 的篩選參數沒驗過,所以那條連結一個字都不該變。
        assert (
            picked["links"]["single_ticket"]["aviasales"]
            == plain["links"]["single_ticket"]["aviasales"]
        )
        for site in ("google_flights", "kayak"):
            assert (
                picked["links"]["single_ticket"][site]
                != plain["links"]["single_ticket"][site]
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


class TestTheReverseCardIsNotJustBlank:
    """畫面上顯示的是「兩張交叉的來回票」,而那種買法**一個數字都沒有** ——
    兩張都按來回計價,台灣航線沒有來回快取。一張全空的卡片沒有告訴使用者任何事,
    所以四段的單程價要以參考基準的身分出現(逐段列、不給總和)。"""

    def _reverse_plan(self, client):
        body = client.post("/api/reverse", json=REVERSE_TRIPS).json()
        return next(
            p for p in body["groups"][0]["plans"] if p["method"] == "reverse"
        )

    def test_the_two_tickets_carry_the_names_the_article_uses(self, client, priced):
        plan = self._reverse_plan(client)
        assert [t["role"] for t in plan["tickets"]] == ["第 1 張票", "第 2 張票"]
        assert [t["code"] for t in plan["tickets"]] == ["台北出發", "東京出發"]
        assert [leg["code"] for t in plan["tickets"] for leg in t["legs"]] == [
            "東京 去程", "大阪 回程", "東京 回程", "大阪 去程",
        ]

    def test_it_says_which_segments_you_fly_on_which_trip(self, client, priced):
        """票是交叉的,行程不是。少了這段,使用者會以為得照票面順序飛。"""
        plan = self._reverse_plan(client)
        assert [tuple(t["codes"]) for t in plan["sequence"]] == [
            ("第 1 張票", "第 2 張票"), ("第 2 張票", "第 1 張票"),
        ]

    def test_the_four_one_way_prices_come_along_as_a_reference(self, client, priced):
        plan = self._reverse_plan(client)
        legs = plan["reference_legs"]
        assert len(legs) == 4
        assert {leg["code"] for leg in legs} == {
            "東京 去程", "東京 回程", "大阪 去程", "大阪 回程",
        }
        assert any(leg["price"] is not None for leg in legs)

    def test_the_reference_never_offers_a_total(self, client, priced):
        """單程加總看不到來回計價的效果,而且錯的方向剛好讓倒買法看起來更好。
        給一個總和,使用者一定會拿它當這兩張票的價格。"""
        plan = self._reverse_plan(client)
        assert plan["pricing"] is None
        assert plan["priceable"] is False
        assert all("total" not in leg for leg in plan["reference_legs"])

    def test_an_unpriced_reference_leg_still_says_why(self, client, priced):
        """參考基準裡的空格跟別處的空格一樣要有理由 —— 它同樣會被讀成
        「那天沒有班機」。"""
        body = client.post(
            "/api/reverse",
            json={**REVERSE_TRIPS, "home": ["TPE", "TSA"]},
        ).json()
        blanks = [
            leg
            for plan in body["groups"][0]["plans"]
            for leg in plan["reference_legs"]
            if leg["price"] is None
        ]
        assert blanks, "這組固定資料本來就留了查無資料的航段"
        for leg in blanks:
            assert leg["gap"]["reason"] in {
                "not_fetched", "route_empty", "nearby", "far_only"
            }
            assert leg["gap"]["text"]


class TestPickingAPlace:
    """點一下要有得選,打字要能過濾。**兩個都要。**

    舊的挑法(選國家 → 從前 12 個城市裡點)是死路:`slice(0, 12)` 讓日本 72 個
    可飛城市只列得出 12 個,岡山、函館、石垣點不到,而畫面上寫著「日本 (77)」。
    但只給一個搜尋框也一樣是死路 —— 換成不知道要打什麼的人卡在空白輸入框前面。
    """

    def test_an_empty_query_still_offers_somewhere_to_start(self, client):
        places = client.get("/api/ref/places").json()["places"]
        assert places, "沒打字也要有得選,否則搜尋框對還沒想好的人就是空白一片"
        assert places[0]["code"] == "TPE"  # 台灣旅客的起點排最前面

    @pytest.mark.parametrize("query", ["東京", "Tokyo", "TYO", "NRT"])
    def test_a_city_is_findable_by_any_of_its_names(self, client, query):
        """3,522 個可飛城市裡只有 138 個有中文名,所以英文與代碼一定要能搜 ——
        不然沒有中文名的地方一樣是死路,只是死得比較隱晦。"""
        places = client.get(f"/api/ref/places?q={query}").json()["places"]
        assert any(p["code"] == "TYO" for p in places)

    def test_typing_a_country_lists_its_cities(self, client):
        """「日本」比「福岡」更容易是腦中的第一個詞,尤其還沒決定去哪一城的時候。"""
        places = client.get("/api/ref/places?q=日本").json()["places"]
        assert {p["code"] for p in places} >= {"TYO", "OSA"}

    def test_a_city_comes_with_every_one_of_its_airports(self, client):
        """選城市 = 把機場全部納入。多機場的城市正是機場替代能省錢的來源。"""
        (tokyo,) = [
            p for p in client.get("/api/ref/places?q=TYO").json()["places"]
            if p["code"] == "TYO"
        ]
        assert {a["code"] for a in tokyo["airports"]} == {"NRT", "HND"}

    def test_bus_stations_never_reach_the_picker(self, client):
        """Travelpayouts 把 LMJ(東京巴士總站)標成 flightable。"""
        places = client.get("/api/ref/places?q=東京").json()["places"]
        assert "LMJ" not in {a["code"] for p in places for a in p["airports"]}


class TestTheDatesCanFlex:
    """固定四個確切日期是這個功能一直沒有價格的原因之一 —— 哪幾天有快取資料
    使用者無從得知。實測同一組行程只把第二趟從 10/20 挪到 10/06,
    算得出總價的機場組合就從 0 種變成 36 種。"""

    def _body(self, **first):
        return {
            "home": ["TPE"],
            "first": {"codes": ["TYO"], **first},
            "second": {
                "codes": ["OSA"], "depart_earliest": "2026-12-06",
                "nights_min": 5, "nights_max": 5,
            },
            "try_both_orders": False, "warm": False,
        }

    def test_exact_dates_still_work(self, client, priced):
        """舊式的 depart + back 不能壞掉 —— 它是這支端點原本的介面。"""
        body = client.post(
            "/api/reverse",
            json=self._body(depart="2026-10-05", back="2026-10-10"),
        ).json()
        seq = body["groups"][0]["plans"][0]["sequence"]
        assert (seq[0]["depart"], seq[0]["back"]) == ("2026-10-05", "2026-10-10")

    def test_a_window_picks_the_priced_day_over_the_blank_one(self, client, priced):
        """固定資料只在 10-05 有價。給一個涵蓋 10-03…10-07 的區間,
        排名要自己挑中 10-05 —— 那正是使用者猜不到的那一天。"""
        body = client.post(
            "/api/reverse",
            json=self._body(
                depart_earliest="2026-10-03", depart_latest="2026-10-07",
                nights_min=5, nights_max=5,
            ),
        ).json()
        assert body["groups"][0]["plans"][0]["sequence"][0]["depart"] == "2026-10-05"

    def test_combinations_are_ranked_by_real_price(self, client, priced):
        """原本產生 144 種、取前 12、顯示第 0 種,而那個順序跟有沒有價格無關 ——
        實測回傳的 12 組全部以 TPE→HND 開頭,HND 沒資料就全滅。"""
        body = client.post(
            "/api/reverse",
            json=self._body(depart="2026-10-05", back="2026-10-10"),
        ).json()
        totals = [g["split_total"] for g in body["groups"]]
        priced_only = [t for t in totals if t is not None]
        assert priced_only == sorted(priced_only), "有價的要照便宜排"
        assert totals.index(None) > len(priced_only) - 1 if None in totals else True

    def test_the_total_is_the_four_one_ways_and_never_a_partial_sum(self, client, priced):
        body = client.post(
            "/api/reverse",
            json=self._body(depart="2026-10-05", back="2026-10-10"),
        ).json()
        for group in body["groups"]:
            legs = [
                leg
                for plan in group["plans"] if plan["method"] == "reverse"
                for leg in plan["reference_legs"]
            ]
            if any(leg["price"] is None for leg in legs):
                assert group["split_total"] is None, "缺一段就不給總價"
                assert group["missing"]
            else:
                assert group["split_total"] == pytest.approx(
                    sum(leg["price"] for leg in legs)
                )


class TestCountryThenCity:
    """先選國家、再選城市 —— 但兩個原本的 bug 要一起修掉,否則又是同一條死路。"""

    def test_every_city_of_a_country_comes_back(self, client):
        """伺服器本來就回全部,截斷發生在前端的 `slice(0, 12)`。這條測試釘住
        後端這一側:日本 72 個可飛城市不能因為排序或分頁少掉任何一個。"""
        body = client.get("/api/ref/countries/JP/airports").json()
        assert len(body["cities"]) == 2  # 固定資料只有東京、大阪
        codes = {a["code"] for c in body["cities"] for a in c["airports"]}
        assert codes == {"NRT", "HND", "KIX", "ITM"}

    def test_countries_with_a_chinese_name_come_before_the_english_ones(self, client):
        """只照 name_zh 排會出事:沒收錄中文名的國家,name_zh 存的是英文字串,
        於是 147 個英文國名會夾在中文國名中間,把挪威、瑞典擠到它們之後。"""
        countries = client.get("/api/ref/countries").json()["countries"]
        translated = [i for i, c in enumerate(countries) if c["translated"]]
        english = [i for i, c in enumerate(countries) if not c["translated"]]
        if english and translated:
            assert max(translated) < min(english)
