"""層一的**寫入路徑**:抓 → 解析 → 存 → 讀回來。

這條路徑在拿到 token 之前不會被任何人跑到,而使用者第一次真實搜尋就是 20–40
次連續外呼 —— 也就是最容易中途壞掉的形狀。所以這裡用假的 client 把整條路徑
在離線狀態下跑完,特別是「中途失敗」那條。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db import SCHEMA
from app.pricing import cached


def month_matrix(*prices, origin="TPE", destination="NRT"):
    return {
        "success": True,
        "data": [
            {
                "origin": origin,
                "destination": destination,
                "depart_date": f"2026-10-{day:02d}",
                "return_date": "",
                "value": price,
                "number_of_changes": 0,
            }
            for day, price in enumerate(prices, start=1)
        ],
    }


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class ScriptedClient:
    """Answers per (origin, destination); an Exception value is raised instead.

    每個 route-month 現在會打**兩支**端點:month-matrix(涵蓋廣、沒有航空公司)
    與 v3 prices_for_dates(涵蓋窄、每列都有航空公司)。`calls` 只記主要來源,
    因為那才是「這條航線抓過幾次」的意思;v3 的呼叫另外記在 `extra_calls`。
    """

    def __init__(self, script, v3=None):
        self.script = script
        self.v3 = v3 or {}
        self.calls: list[tuple[str, str]] = []
        self.extra_calls: list[tuple[str, str]] = []

    def get(self, url, params=None, headers=None):
        key = (params["origin"], params["destination"])
        if "prices_for_dates" in url:
            self.extra_calls.append(key)
            return FakeResponse(self.v3.get(key, {"data": []}))
        self.calls.append(key)
        answer = self.script[key]
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer)

    def close(self):
        return None


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _allow_settings_patch(monkeypatch):
    """`Settings` is frozen, so tests patch the module-level object's dict."""
    object.__setattr__(cached.settings, "travelpayouts_token", "test-token")
    yield
    object.__setattr__(cached.settings, "travelpayouts_token", "")


class TestHappyPath:
    def test_prices_survive_the_round_trip_to_sqlite(self, conn):
        client = ScriptedClient(
            {
                ("TPE", "NRT"): month_matrix(6800, 7100, destination="NRT"),
                ("NRT", "TPE"): month_matrix(6400, 6900, origin="NRT", destination="TPE"),
            }
        )
        outcome = cached.ensure_routes(
            conn, [("TPE", "NRT"), ("NRT", "TPE")], ["2026-10"],
            currency="TWD", client=client,
        )

        assert outcome.counts == {("TPE", "NRT", "2026-10"): 2, ("NRT", "TPE", "2026-10"): 2}
        assert not outcome.failures

        conn.rollback()  # 模擬請求結束:沒 commit 的都會消失
        lookup = cached.load_lookup(conn, [("TPE", "NRT")], "TWD")
        prices = sorted(point.price for point in lookup.values())
        assert prices == [6800, 7100]

    def test_row_counts_are_recorded_even_when_a_route_has_nothing(self, conn):
        client = ScriptedClient({("ITM", "TPE"): {"success": True, "data": []}})
        outcome = cached.ensure_routes(
            conn, [("ITM", "TPE")], ["2026-10"], currency="TWD", client=client
        )

        conn.rollback()
        assert outcome.counts == {("ITM", "TPE", "2026-10"): 0}
        assert outcome.empty_routes == ["ITM→TPE"]
        # 「查過、零筆」必須留下紀錄,否則每次搜尋都會重打一次冷門航線。
        assert cached._is_fresh(conn, "ITM", "TPE", "2026-10", "TWD") is True

    def test_a_second_pass_serves_from_cache_without_calling_out(self, conn):
        client = ScriptedClient({("TPE", "NRT"): month_matrix(6800)})
        cached.ensure_routes(conn, [("TPE", "NRT")], ["2026-10"], currency="TWD", client=client)
        cached.ensure_routes(conn, [("TPE", "NRT")], ["2026-10"], currency="TWD", client=client)
        assert len(client.calls) == 1

    def test_force_refetches(self, conn):
        client = ScriptedClient({("TPE", "NRT"): month_matrix(6800)})
        cached.ensure_routes(conn, [("TPE", "NRT")], ["2026-10"], currency="TWD", client=client)
        cached.ensure_routes(
            conn, [("TPE", "NRT")], ["2026-10"], currency="TWD", client=client, force=True
        )
        assert len(client.calls) == 2


class TestPartialFailure:
    """一次 warm 是 20–40 次連續外呼。中途出現 429 或逾時是常態,不是意外。"""

    def _run(self, conn):
        client = ScriptedClient(
            {
                ("TPE", "NRT"): month_matrix(6800, 7100),
                ("TPE", "HND"): RuntimeError("HTTP 429"),
                ("TPE", "KIX"): month_matrix(6100, destination="KIX"),
            }
        )
        outcome = cached.ensure_routes(
            conn,
            [("TPE", "NRT"), ("TPE", "HND"), ("TPE", "KIX")],
            ["2026-10"],
            currency="TWD",
            client=client,
        )
        # In production the request ends and `get_conn` closes the connection.
        # Anything not committed by then is discarded. Reading back on the same
        # open transaction would show uncommitted writes and prove nothing, so
        # the rollback stands in for that close.
        conn.rollback()
        return client, outcome

    def test_one_bad_route_does_not_abort_the_others(self, conn):
        client, outcome = self._run(conn)
        assert client.calls == [("TPE", "NRT"), ("TPE", "HND"), ("TPE", "KIX")]
        assert outcome.failed_routes == ["TPE→HND"]

    def test_successful_writes_are_not_rolled_back_by_a_later_failure(self, conn):
        """單一 commit 放在迴圈結尾的話,最後一條航線的 429 會把前面全部丟掉,
        重試時再燒一次額度。"""
        self._run(conn)
        stored = conn.execute("SELECT DISTINCT destination FROM price_cache").fetchall()
        assert sorted(row["destination"] for row in stored) == ["KIX", "NRT"]

    def test_the_failure_itself_is_recorded(self, conn):
        """錯誤紀錄跟著交易一起被 rollback 的話,/api/health 會顯示一個
        「從來沒出過問題」的資料來源 —— 儀器對最常見的失敗完全失明。"""
        self._run(conn)
        errors = conn.execute(
            "SELECT endpoint, error FROM fetch_log WHERE error IS NOT NULL"
        ).fetchall()
        assert len(errors) == 1
        assert "429" in errors[0]["error"]

    def test_every_call_logs_its_row_count(self, conn):
        self._run(conn)
        rows = conn.execute(
            "SELECT row_count FROM fetch_log WHERE source = ? ORDER BY id",
            (cached.SOURCE,),
        ).fetchall()
        assert [row["row_count"] for row in rows] == [2, 0, 1]

    def test_a_failed_route_is_not_marked_fresh(self, conn):
        """抓失敗不能被當成「查過了」,否則六小時內都不會再試。"""
        self._run(conn)
        assert cached._is_fresh(conn, "TPE", "HND", "2026-10", "TWD") is False


class TestRejectedKey:
    """token 打錯時,「這 20 條航線沒回來」會讓人去找根本不存在的網路問題。"""

    def _run(self, conn, error):
        client = ScriptedClient({("TPE", "NRT"): error, ("TPE", "KIX"): error})
        outcome = cached.ensure_routes(
            conn, [("TPE", "NRT"), ("TPE", "KIX")], ["2026-10"],
            currency="TWD", client=client,
        )
        conn.rollback()
        return outcome

    def test_all_routes_rejected_is_reported_as_a_key_problem(self, conn):
        outcome = self._run(conn, RuntimeError("Client error '401 Unauthorized' for url ..."))
        assert outcome.unauthorized is True

    def test_a_forbidden_response_counts_too(self, conn):
        outcome = self._run(conn, RuntimeError("Client error '403 Forbidden' for url ..."))
        assert outcome.unauthorized is True

    def test_a_timeout_is_not_a_key_problem(self, conn):
        outcome = self._run(conn, RuntimeError("ReadTimeout"))
        assert outcome.unauthorized is False

    def test_a_partial_failure_is_not_a_key_problem(self, conn):
        """有些成功、有些 401,那就不是金鑰整體無效,別誤導使用者去換 token。"""
        client = ScriptedClient({
            ("TPE", "NRT"): month_matrix(6800),
            ("TPE", "KIX"): RuntimeError("Client error '401 Unauthorized' for url ..."),
        })
        outcome = cached.ensure_routes(
            conn, [("TPE", "NRT"), ("TPE", "KIX")], ["2026-10"],
            currency="TWD", client=client,
        )
        conn.rollback()
        assert outcome.unauthorized is False

    def test_no_failures_at_all_is_not_a_key_problem(self, conn):
        client = ScriptedClient({("TPE", "NRT"): month_matrix(6800)})
        outcome = cached.ensure_routes(
            conn, [("TPE", "NRT")], ["2026-10"], currency="TWD", client=client
        )
        assert outcome.unauthorized is False
