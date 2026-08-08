"""層二(即時報價 adapter)的測試。

核心不變式:**沒有價格的地方一定有理由。** 一個沒有解釋的空格,使用者只會
讀成「這個組合很貴」,而那是我們沒有根據的斷言。
"""

from datetime import date

import pytest

from app.combos import Combo, FlightLeg
from app.pricing.live import (
    DeepLinkProvider,
    DuffelProvider,
    Quote,
    SplitQuote,
)

OUTBOUND = FlightLeg("TPE", "NRT", date(2026, 10, 5), "台北", "東京")
INBOUND = FlightLeg("ITM", "TPE", date(2026, 10, 11), "大阪", "台北")
COMBO = Combo(legs=(OUTBOUND, INBOUND), shape_label="台北→東京〜大阪→台北", is_baseline=False)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests: list[dict] = []

    def post(self, url, params=None, headers=None, json=None):
        self.requests.append({"url": url, "params": params, "headers": headers, "json": json})
        return self._responses.pop(0) if self._responses else FakeResponse({"data": {"offers": []}})

    def close(self):
        return None


def offers(*amounts, currency="TWD", carrier="EVA Air"):
    return FakeResponse(
        {
            "data": {
                "offers": [
                    {"total_amount": str(amount), "total_currency": currency,
                     "owner": {"name": carrier}}
                    for amount in amounts
                ]
            }
        }
    )


class TestQuoteInvariant:
    def test_a_quote_without_a_price_must_explain_itself(self):
        with pytest.raises(ValueError):
            Quote(source="duffel", currency="TWD")

    def test_a_priced_quote_needs_no_reason(self):
        assert Quote(source="duffel", currency="TWD", total=12200).total == 12200


class TestDeepLinkProvider:
    def test_it_is_always_available(self):
        assert DeepLinkProvider().available is True

    def test_it_returns_an_explicit_no_price_rather_than_zero(self):
        quote = DeepLinkProvider().price_single_ticket(COMBO)
        assert quote.total is None
        assert "連結" in (quote.unavailable_reason or "")

    def test_split_quote_covers_every_leg(self):
        split = DeepLinkProvider().price_split_tickets(COMBO)
        assert len(split.legs) == 2
        assert split.total is None


class TestDuffelProvider:
    def test_without_a_token_it_says_so(self):
        quote = DuffelProvider(token="").price_single_ticket(COMBO)
        assert quote.total is None
        assert "DUFFEL_TOKEN" in (quote.unavailable_reason or "")

    def test_cheapest_offer_wins(self):
        client = FakeClient(offers(18000, 12200, 15400))
        quote = DuffelProvider(token="tok", client=client).price_single_ticket(COMBO)
        assert quote.total == 12200
        assert quote.offer_count == 3
        assert quote.carrier == "EVA Air"

    def test_multi_city_is_sent_as_one_slice_per_leg(self):
        client = FakeClient(offers(12200))
        DuffelProvider(token="tok", client=client).price_single_ticket(COMBO)
        body = client.requests[0]["json"]["data"]
        assert [s["origin"] for s in body["slices"]] == ["TPE", "ITM"]
        assert [s["destination"] for s in body["slices"]] == ["NRT", "TPE"]
        assert body["slices"][1]["departure_date"] == "2026-10-11"

    def test_no_offers_is_an_expected_outcome_not_an_error(self):
        """很多城市對根本沒有可售的單一開口票 —— 空白是常態,不是 bug。"""
        client = FakeClient(FakeResponse({"data": {"offers": []}}))
        quote = DuffelProvider(token="tok", client=client).price_single_ticket(COMBO)
        assert quote.total is None
        assert quote.unavailable_reason == "此行程查無可售票種"

    def test_a_failed_call_becomes_a_reason_not_an_exception(self):
        client = FakeClient(FakeResponse({}, status_code=500))
        quote = DuffelProvider(token="tok", client=client).price_single_ticket(COMBO)
        assert quote.total is None
        assert "即時查價失敗" in (quote.unavailable_reason or "")

    def test_split_tickets_are_searched_one_leg_at_a_time(self):
        client = FakeClient(offers(6800), offers(5400))
        split = DuffelProvider(token="tok", client=client).price_split_tickets(COMBO)
        assert len(client.requests) == 2
        assert all(len(r["json"]["data"]["slices"]) == 1 for r in client.requests)
        assert split.total == 12200

    def test_split_total_is_none_when_any_leg_is_unsellable(self):
        client = FakeClient(offers(6800), FakeResponse({"data": {"offers": []}}))
        split = DuffelProvider(token="tok", client=client).price_split_tickets(COMBO)
        assert split.total is None
        assert split.unavailable_reason == "此行程查無可售票種"

    def test_the_airline_currency_is_reported_as_is(self):
        """不做匯率換算再假裝是台幣 —— 換算後的數字沒辦法拿去跟訂票頁對帳。"""
        client = FakeClient(offers(52000, currency="JPY"))
        quote = DuffelProvider(token="tok", client=client).price_single_ticket(COMBO)
        assert quote.currency == "JPY"

    def test_passenger_count_is_passed_through(self):
        client = FakeClient(offers(12200))
        DuffelProvider(token="tok", client=client).price_single_ticket(COMBO, passengers=3)
        assert len(client.requests[0]["json"]["data"]["passengers"]) == 3


class TestSplitQuote:
    def test_totals_add_up(self):
        split = SplitQuote(
            legs=(
                Quote(source="duffel", currency="TWD", total=6800),
                Quote(source="duffel", currency="TWD", total=5400),
            ),
            currency="TWD",
        )
        assert split.total == 12200
        assert split.unavailable_reason is None
