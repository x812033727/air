"""深連結測試。

Google Flights 的 `tfs` 是未公開格式,所以這裡把「已經在真實瀏覽器裡驗證過會
正確開啟」的那一組位元組寫死當黃金樣本。這不是為了測試 base64 會不會動,而是
為了讓「有人手滑改了欄位編號」變成一個紅燈,而不是一個看起來正常、實際上目的地
是空白的連結。

驗證紀錄見 docs/verification.md。
"""

import base64
from datetime import date

from app.combos import Combo, FlightLeg
from app.pricing import deeplinks

OUTBOUND = FlightLeg("TPE", "NRT", date(2026, 10, 5), "台北", "東京")
INBOUND = FlightLeg("ITM", "TPE", date(2026, 10, 11), "大阪", "台北")

# 2026-08-08 於 Chromium 實測:此連結開啟後 Google Flights 進入「多停點」模式,
# 兩段的出發地、目的地、日期全部正確帶入。
VERIFIED_OPEN_JAW_TFS = (
    "GhoSCjIwMjYtMTAtMDVqBRIDVFBFcgUSA05SVBoaEgoyMDI2LTEwLTExagUSA0lUTXIFEgNUUEVAAUgBmAED"
)


def decode(url: str) -> bytes:
    tfs = url.split("tfs=")[1].split("&")[0]
    return base64.urlsafe_b64decode(tfs + "=" * (-len(tfs) % 4))


class TestGoogleFlights:
    def test_open_jaw_matches_the_browser_verified_encoding(self):
        url = deeplinks.google_flights_url([OUTBOUND, INBOUND])
        assert f"tfs={VERIFIED_OPEN_JAW_TFS}" in url

    def test_destination_is_field_14(self):
        """欄位 16 是網路上常見的說法,實測會讓目的地整個空白。"""
        raw = decode(deeplinks.google_flights_url([OUTBOUND]))
        assert b"\x72\x05\x12\x03NRT" in raw, "destination must use field 14 (tag 0x72)"
        assert b"\x82\x01\x05\x12\x03NRT" not in raw, "field 16 leaves 「Where to?」 empty"

    def test_origin_is_field_13(self):
        raw = decode(deeplinks.google_flights_url([OUTBOUND]))
        assert b"\x6a\x05\x12\x03TPE" in raw

    def test_single_leg_is_flagged_one_way(self):
        raw = decode(deeplinks.google_flights_url([OUTBOUND]))
        assert raw.endswith(b"\x98\x01\x02")  # trip = 2 (one way)

    def test_an_open_jaw_is_flagged_multi_city(self):
        raw = decode(deeplinks.google_flights_url([OUTBOUND, INBOUND]))
        assert raw.endswith(b"\x98\x01\x03")  # trip = 3 (multi-city)

    def test_a_there_and_back_pair_is_flagged_round_trip(self):
        """Google 對來回票與多停點是不同的計價方式,把普通來回送成多停點會高估它。
        在倒買法的比較裡,那等於系統性偏袒倒買法。"""
        home_again = FlightLeg("NRT", "TPE", date(2026, 10, 11), "東京", "台北")
        raw = decode(deeplinks.google_flights_url([OUTBOUND, home_again]))
        assert raw.endswith(b"\x98\x01\x01")  # trip = 1 (round trip)

    def test_a_round_trip_through_a_different_airport_stays_multi_city(self):
        """桃園去、松山回不是單純的來回,不能當來回票送。"""
        other_airport = FlightLeg("NRT", "TSA", date(2026, 10, 11), "東京", "台北")
        raw = decode(deeplinks.google_flights_url([OUTBOUND, other_airport]))
        assert raw.endswith(b"\x98\x01\x03")

    def test_each_passenger_gets_its_own_entry(self):
        raw = decode(deeplinks.google_flights_url([OUTBOUND], passengers=3))
        assert raw.count(b"\x40\x01") == 3

    def test_cabin_class_is_carried(self):
        raw = decode(deeplinks.google_flights_url([OUTBOUND], cabin="business"))
        assert b"\x48\x03" in raw  # seat = 3 (business)

    def test_unknown_cabin_falls_back_to_economy(self):
        raw = decode(deeplinks.google_flights_url([OUTBOUND], cabin="hammock"))
        assert b"\x48\x01" in raw


class TestKayak:
    def test_each_leg_becomes_a_path_segment(self):
        url = deeplinks.kayak_url([OUTBOUND, INBOUND])
        assert url.startswith(
            "https://www.tw.kayak.com/flights/TPE-NRT/2026-10-05/ITM-TPE/2026-10-11"
        )

    def test_cheapest_first(self):
        assert "sort=price_a" in deeplinks.kayak_url([OUTBOUND])

    def test_economy_does_not_add_a_cabin_parameter(self):
        assert "cabin=" not in deeplinks.kayak_url([OUTBOUND])


class TestAviasales:
    def test_route_is_packed_as_origin_ddmm_destination(self):
        url = deeplinks.aviasales_url([OUTBOUND, INBOUND])
        assert url.startswith("https://www.aviasales.com/search/TPE0510NRTITM1110TPE1")

    def test_marker_is_omitted_when_unset(self):
        assert "marker=" not in deeplinks.aviasales_url([OUTBOUND], marker="")

    def test_marker_is_appended_when_configured(self):
        assert deeplinks.aviasales_url([OUTBOUND], marker="654321").endswith("?marker=654321")


class TestAssembly:
    def test_single_ticket_links_cover_the_whole_itinerary(self):
        combo = Combo(legs=(OUTBOUND, INBOUND), shape_label="台北→東京〜大阪→台北",
                      is_baseline=False)
        links = deeplinks.links_for_single_ticket(combo)
        assert set(links) == {"google_flights", "kayak", "aviasales"}
        assert "ITM-TPE" in links["kayak"]

    def test_split_links_are_one_search_per_leg(self):
        combo = Combo(legs=(OUTBOUND, INBOUND), shape_label="台北→東京〜大阪→台北",
                      is_baseline=False)
        per_leg = deeplinks.links_for_split_tickets(combo)
        assert [entry["leg"] for entry in per_leg] == ["TPE→NRT", "ITM→TPE"]
        # 每一段都必須是獨立的單程搜尋 —— 這正是「拼票」的定義。
        for entry, expected in zip(per_leg, (OUTBOUND, INBOUND)):
            raw = decode(entry["google_flights"])
            assert raw.endswith(b"\x98\x01\x02")
            assert expected.origin.encode() in raw


class TestAirlineFilter:
    """航空公司篩選是**航段裡的欄位 6**,重複出現一次代表一家。

    這個欄位編號是實測來的:在 Chromium 開一次 TPE→NRT,用 Google 自己的
    篩選面板勾「僅限長榮」,再把它產生的 URL 讀回來解碼。猜欄位編號的下場
    上次已經見過一次 —— 目的地誤用欄位 16,連結照樣打得開,只是目的地是空白的。
    """

    # 2026-08-09 於 Chromium 實測:此連結開啟後,篩選列顯示
    # 「長榮航空 +1, 航空公司, 已選取」。
    VERIFIED_TWO_AIRLINE_TFS = (
        "GiISCjIwMjYtMTAtMDcyAkJSMgJDSWoFEgNUUEVyBRIDTlJUQAFIAZgBAg"
    )

    def test_the_verified_bytes_are_reproduced_exactly(self):
        leg = FlightLeg("TPE", "NRT", date(2026, 10, 7), "台北", "東京")
        url = deeplinks.google_flights_url([leg], airlines=["BR", "CI"])
        assert url.split("tfs=")[1].split("&")[0] == self.VERIFIED_TWO_AIRLINE_TFS

    def test_picking_nobody_leaves_the_link_byte_for_byte_unchanged(self):
        """沒選航空公司的連結必須跟以前一模一樣,否則這次改動會悄悄改掉
        每一條既有連結,而那些連結是這個站唯一的出口。"""
        assert deeplinks.google_flights_url([OUTBOUND, INBOUND]) == deeplinks.google_flights_url(
            [OUTBOUND, INBOUND], airlines=[]
        )
        assert decode(deeplinks.google_flights_url([OUTBOUND, INBOUND])) == base64.urlsafe_b64decode(
            VERIFIED_OPEN_JAW_TFS + "=" * (-len(VERIFIED_OPEN_JAW_TFS) % 4)
        )

    def test_every_leg_carries_the_filter(self):
        """多停點的篩選是逐段設定的 —— Google 自己的介面就是這樣寫進 URL 的。
        只設第一段,後面幾段會回到「所有航空公司」。"""
        url = deeplinks.google_flights_url([OUTBOUND, INBOUND], airlines=["BR"])
        assert decode(url).count(b"BR") == 2

    def test_junk_codes_are_dropped_rather_than_forwarded(self):
        """一個亂碼篩選會讓 Google 回一張空清單,而空清單長得就像
        「那天沒有班機」—— 正好是這次改動要消滅的那句誤導。"""
        assert deeplinks.normalise_airlines(["br", "BR", "", "LONG", "C I", None]) == ("BR",)

    def test_kayak_gets_the_filter_too(self):
        """實測:帶了 `fs=airlines=BR,CI` 之後,Kayak 送回來的頁面裡
        `serverRequestState.params.fs` 就是那個值;不帶則整份 HTML 找不到
        `airlines=`。它真的進了 Kayak 的查詢狀態,不是被丟掉的網址參數。"""
        url = deeplinks.kayak_url([OUTBOUND, INBOUND], airlines=["BR", "CI"])
        assert "fs=airlines=BR,CI" in url

    def test_kayak_stays_clean_when_nobody_is_picked(self):
        assert "fs=" not in deeplinks.kayak_url([OUTBOUND, INBOUND])

    def test_aviasales_is_left_unfiltered_and_says_so(self):
        """Aviasales 的篩選參數沒驗過。送一個沒驗證過的參數出去,壞掉的樣子跟
        「真的沒有班機」分不開 —— 所以寧可不帶,而且要在按鈕上標出來,
        否則使用者按下去只會覺得篩選壞了(他真的回報過)。"""
        combo = Combo(legs=(OUTBOUND, INBOUND), shape_label="x", is_baseline=False)
        links = deeplinks.links_for_single_ticket(combo, airlines=["BR"])
        assert "BR" not in links["aviasales"]
        assert deeplinks.LINK_INFO["aviasales"]["filters_airlines"] is False
        assert b"BR" in decode(links["google_flights"])
        assert "BR" in links["kayak"]

    def test_every_link_declares_its_language_and_filter_state(self):
        """三顆按鈕長得一模一樣,差別必須寫在上面。這份對照表是實測結果:
        Aviasales 自家 hreflang 只列 en/ru/az/hy/ka/kk/ky/es,沒有中文。"""
        assert set(deeplinks.LINK_INFO) == {"google_flights", "kayak", "aviasales"}
        for name, info in deeplinks.LINK_INFO.items():
            assert info["label"] and info["locale"]
            assert isinstance(info["filters_airlines"], bool)
        assert deeplinks.LINK_INFO["google_flights"]["locale"] == "繁中"
        assert deeplinks.LINK_INFO["kayak"]["locale"] == "繁中"
        assert deeplinks.LINK_INFO["aviasales"]["locale"] == "英文"


class TestNonstopFilter:
    """直達是**航段裡的 field 5**(轉機次數上限,0 = 只要直達)。

    跟航空公司一樣是在瀏覽器裡按 Google 自己的篩選面板、再把它產生的網址讀回來
    解碼得到的。兩個一起帶的多段行程實測會顯示「所有篩選器 (2)」,
    左邊是「直達, 轉機次數, 已選取」,右邊是「BR +1, 航空公司, 已選取」。
    """

    def test_nonstop_lands_in_every_leg(self):
        url = deeplinks.google_flights_url([OUTBOUND, INBOUND], nonstop=True)
        # field 5 varint 0 → tag 0x28,兩段各一個。
        assert decode(url).count(b"\x28\x00") == 2

    def test_not_asking_leaves_the_bytes_untouched(self):
        assert deeplinks.google_flights_url([OUTBOUND, INBOUND], nonstop=False) == (
            deeplinks.google_flights_url([OUTBOUND, INBOUND])
        )
        assert b"\x28\x00" not in decode(deeplinks.google_flights_url([OUTBOUND, INBOUND]))

    def test_kayak_joins_two_filters_with_a_semicolon(self):
        """Kayak 把所有篩選塞在一個 fs 參數裡。實測 `fs=airlines=BR,CI;stops=0`
        會原封不動出現在它回應的 `serverRequestState.params.fs`。"""
        url = deeplinks.kayak_url([OUTBOUND], airlines=["BR", "CI"], nonstop=True)
        assert "fs=airlines=BR,CI;stops=0" in url

    def test_kayak_takes_nonstop_on_its_own(self):
        assert "fs=stops=0" in deeplinks.kayak_url([OUTBOUND], nonstop=True)

    def test_aviasales_is_still_left_alone(self):
        combo = Combo(legs=(OUTBOUND,), shape_label="x", is_baseline=False)
        links = deeplinks.links_for_single_ticket(combo, nonstop=True)
        assert "stops" not in links["aviasales"]
        assert deeplinks.LINK_INFO["aviasales"]["filters_stops"] is False
