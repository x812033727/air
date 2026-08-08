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
            "https://www.kayak.com/flights/TPE-NRT/2026-10-05/ITM-TPE/2026-10-11"
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
