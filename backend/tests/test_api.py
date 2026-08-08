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
    """站台是公開的、沒有登入。金鑰存在伺服器就等於任何找到設定頁的人都讀得走,
    所以由瀏覽器保管、隨請求送出,伺服器用完就忘。"""

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
