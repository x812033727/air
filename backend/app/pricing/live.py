"""層二:即時實價。

這一層是**可抽換的**,而且預設實作不需要任何 API 金鑰 —— 這是刻意的設計決定,
不是降級方案。理由:

* Amadeus 自助 API 已於 2026-07-17 關閉,Kiwi Tequila 是邀請制;
* Duffel 有真實資料,但 live 模式需要驗證商業實體,個人專案不保證申請得過;
* Duffel 的 test mode 是虛構航空、價格不真實,所以沒有「先用 sandbox 開發再切
  live」這條路可走。

所以 `DeepLinkProvider` 是預設出貨路徑,`DuffelProvider` 是拿到 live token 之後
自動接上的加值。兩者都實作同一個 protocol,產品在有無金鑰的情況下都完整。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence

import httpx

from app.combos import Combo, FlightLeg
from app.config import settings
from app.db import utcnow

DUFFEL_BASE = "https://api.duffel.com"


@dataclass(frozen=True)
class Quote:
    """一筆即時報價,或一個「為什麼沒有報價」的明確理由。

    `total is None` 一定伴隨 `unavailable_reason`。沒有理由的空值不准出現 ——
    介面上一個沒有解釋的空格,使用者只會讀成「這個組合很貴」。
    """

    source: str
    currency: str
    total: float | None = None
    unavailable_reason: str | None = None
    carrier: str | None = None
    offer_count: int = 0
    fetched_at: datetime = field(default_factory=utcnow)
    # Duffel 的 test mode 回的是**虛構航空的假價**(carrier「Duffel Airways」,
    # 幣別 AUD,TPE→KIX ＋ NRT→TPE 報 161)。那些數字長得跟真的一模一樣,
    # 一旦流進比價就會用假價算出一個看起來很真的結論 —— 所以要帶著旗標走,
    # 由呼叫端決定怎麼擋,而不是讓它安靜地混進去。
    test_mode: bool = False

    def __post_init__(self) -> None:
        if self.total is None and not self.unavailable_reason:
            raise ValueError("a quote without a price must say why")


@dataclass(frozen=True)
class SplitQuote:
    """拼票:各段分開買的實價,以及加總。"""

    legs: tuple[Quote, ...]
    currency: str

    @property
    def total(self) -> float | None:
        if any(leg.total is None for leg in self.legs):
            return None
        return sum(leg.total or 0.0 for leg in self.legs)

    @property
    def unavailable_reason(self) -> str | None:
        missing = [leg for leg in self.legs if leg.total is None]
        if not missing:
            return None
        return missing[0].unavailable_reason


class LivePriceProvider(Protocol):
    name: str
    available: bool

    def price_single_ticket(
        self, combo: Combo, *, passengers: int = 1, cabin: str = "economy"
    ) -> Quote:
        """整趟當成一張多段票的價格。"""

    def price_split_tickets(
        self, combo: Combo, *, passengers: int = 1, cabin: str = "economy"
    ) -> SplitQuote:
        """各段分開買、各自是獨立單程票的價格。"""


class DeepLinkProvider:
    """預設實作:不報價,只確保每個組合都有可以按下去看真價的出口。

    回傳的是「明確的沒有價格」而不是零或空白。頁面上呈現的會是一個連結,
    不是一個看起來像 0 元的空格。
    """

    name = "deeplink"
    available = True

    def price_single_ticket(
        self, combo: Combo, *, passengers: int = 1, cabin: str = "economy"
    ) -> Quote:
        return Quote(
            source=self.name,
            currency=settings.default_currency,
            unavailable_reason="未接即時報價來源,請用右側連結查看實際票價",
        )

    def price_split_tickets(
        self, combo: Combo, *, passengers: int = 1, cabin: str = "economy"
    ) -> SplitQuote:
        return SplitQuote(
            legs=tuple(
                Quote(
                    source=self.name,
                    currency=settings.default_currency,
                    unavailable_reason="未接即時報價來源,請用右側連結查看實際票價",
                )
                for _ in combo.legs
            ),
            currency=settings.default_currency,
        )


class DuffelProvider:
    """即時可訂價。搜尋不收費,只有真的開票才收 $3/訂單 —— 我們不開票。

    Duffel 回的是航空公司的結帳幣別,不見得是 TWD,所以 `Quote.currency` 一定
    照實帶回來,不做匯率換算後假裝是台幣。
    """

    name = "duffel"

    #: Duffel 用 token 前綴區分沙盒與正式,這是官方的命名,不是猜的。
    TEST_PREFIX = "duffel_test_"

    def __init__(self, token: str | None = None, client: httpx.Client | None = None) -> None:
        self._token = token if token is not None else settings.duffel_token
        self._client = client
        self.available = bool(self._token)
        self.test_mode = self._token.startswith(self.TEST_PREFIX)

    def _post_offer_request(self, slices: Sequence[dict[str, str]], passengers: int, cabin: str):
        client = self._client or httpx.Client(timeout=settings.request_timeout_s)
        owns_client = self._client is None
        try:
            return client.post(
                f"{DUFFEL_BASE}/air/offer_requests",
                params={"return_offers": "true"},
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Duffel-Version": settings.duffel_api_version,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={
                    "data": {
                        "slices": list(slices),
                        "passengers": [{"type": "adult"} for _ in range(max(1, passengers))],
                        "cabin_class": cabin,
                    }
                },
            )
        finally:
            if owns_client:
                client.close()

    def _quote(self, slices: Sequence[dict[str, str]], passengers: int, cabin: str) -> Quote:
        if not self.available:
            return Quote(
                source=self.name,
                currency=settings.default_currency,
                unavailable_reason="未設定 DUFFEL_TOKEN",
            )
        try:
            response = self._post_offer_request(slices, passengers, cabin)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            return Quote(
                source=self.name,
                currency=settings.default_currency,
                unavailable_reason=f"即時查價失敗:{type(exc).__name__}",
                test_mode=self.test_mode,
            )

        offers = (payload.get("data") or {}).get("offers") or []
        if not offers:
            # 這是預期常態,不是 bug:很多城市對根本沒有可售的單一開口票。
            return Quote(
                source=self.name,
                currency=settings.default_currency,
                unavailable_reason="此行程查無可售票種",
                test_mode=self.test_mode,
            )

        cheapest = min(offers, key=lambda offer: float(offer.get("total_amount", "inf")))
        return Quote(
            source=self.name,
            currency=cheapest.get("total_currency") or settings.default_currency,
            total=float(cheapest["total_amount"]),
            carrier=(cheapest.get("owner") or {}).get("name"),
            offer_count=len(offers),
            test_mode=self.test_mode,
        )

    @staticmethod
    def _slice(leg: FlightLeg) -> dict[str, str]:
        return {
            "origin": leg.origin,
            "destination": leg.destination,
            "departure_date": leg.depart_date.isoformat(),
        }

    def price_single_ticket(
        self, combo: Combo, *, passengers: int = 1, cabin: str = "economy"
    ) -> Quote:
        return self._quote([self._slice(leg) for leg in combo.legs], passengers, cabin)

    def price_split_tickets(
        self, combo: Combo, *, passengers: int = 1, cabin: str = "economy"
    ) -> SplitQuote:
        quotes = tuple(
            self._quote([self._slice(leg)], passengers, cabin) for leg in combo.legs
        )
        currency = next((q.currency for q in quotes if q.total is not None),
                        settings.default_currency)
        return SplitQuote(legs=quotes, currency=currency)


def get_provider(token: str | None = None) -> LivePriceProvider:
    """Pick the live provider the current configuration supports.

    `token` 讓呼叫端把**存在站台上**的金鑰傳進來 —— 使用者是在網頁上填的,
    不是在部署時寫進 .env 的,所以不能只看環境變數。
    """
    resolved = token if token is not None else settings.duffel_token
    if resolved:
        return DuffelProvider(resolved)
    return DeepLinkProvider()
