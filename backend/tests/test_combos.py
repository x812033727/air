"""The combination generator is the one place where a quiet mistake becomes a
wrong recommendation rather than a crash, so these tests pin down the date
arithmetic and the ceilings in detail.

Scenario throughout: 台北 → 日本兩城開口 (the trip this project was built for).
"""

from datetime import date

import pytest

from app import combos
from app.combos import (
    Link,
    SearchRequest,
    SpecTooLarge,
    Waypoint,
    count_combos,
    default_links,
    expand_shapes,
    generate,
    generate_all,
    route_pairs,
)

TAIPEI = Waypoint("台北", ("TPE", "TSA"), country_code="TW")
TOKYO = Waypoint("東京", ("NRT", "HND"), country_code="JP", nights_min=3, nights_max=3)
OSAKA = Waypoint("大阪", ("KIX", "ITM"), country_code="JP", nights_min=2, nights_max=2)


def japan_request(**overrides) -> SearchRequest:
    defaults = dict(
        home=TAIPEI,
        stops=(TOKYO, OSAKA),
        depart_earliest=date(2026, 10, 1),
        depart_latest=date(2026, 10, 1),
    )
    defaults.update(overrides)
    return SearchRequest(**defaults)


class TestShapes:
    def test_two_cities_yield_both_orders_plus_baselines(self):
        shapes = expand_shapes(japan_request())
        labels = [s.label for s in shapes]

        assert "台北→東京〜大阪→台北" in labels
        assert "台北→大阪〜東京→台北" in labels
        # 決策 3:一般來回也必須被產生出來,由同一個產生器、同一套排名。
        assert "台北→東京→台北" in labels
        assert "台北→大阪→台北" in labels

    def test_baselines_are_flagged_not_mixed_in(self):
        shapes = expand_shapes(japan_request())
        baselines = {s.label for s in shapes if s.is_baseline}
        assert baselines == {"台北→東京→台北", "台北→大阪→台北"}

    def test_same_country_hops_default_to_surface(self):
        assert default_links((TOKYO, OSAKA)) == (Link.SURFACE,)

    def test_cross_border_hops_default_to_flying(self):
        bangkok = Waypoint("曼谷", ("BKK",), country_code="TH")
        singapore = Waypoint("新加坡", ("SIN",), country_code="SG")
        assert default_links((bangkok, singapore)) == (Link.FLY,)

    def test_user_supplied_links_apply_only_to_the_order_they_typed(self):
        # 使用者說「東京→大阪這段我要飛」,但反向的 shape 是不同的一段路,
        # 不該把這個選擇原封不動套過去。
        request = japan_request(internal_links=(Link.FLY,))
        shapes = expand_shapes(request)
        typed = next(s for s in shapes if s.label.startswith("台北→東京"))
        reversed_order = next(
            s for s in shapes if s.label.startswith("台北→大阪") and not s.is_baseline
        )
        assert typed.links == (Link.FLY, Link.FLY, Link.FLY)
        assert reversed_order.links == (Link.FLY, Link.SURFACE, Link.FLY)

    def test_single_stop_has_no_baseline_of_its_own(self):
        request = japan_request(stops=(TOKYO,))
        shapes = expand_shapes(request)
        assert [s.label for s in shapes] == ["台北→東京→台北"]
        assert not any(s.is_baseline for s in shapes)


class TestDateArithmetic:
    def test_open_jaw_return_date_accounts_for_both_stays(self):
        request = japan_request()
        shape = next(
            s
            for s in expand_shapes(request)
            if s.label == "台北→東京〜大阪→台北"
        )
        first = next(iter(generate(shape, request)))

        # 出發 10/1,東京 3 晚、大阪 2 晚 → 回程 10/6。
        assert first.legs[0].depart_date == date(2026, 10, 1)
        assert first.legs[-1].depart_date == date(2026, 10, 6)

    def test_surface_hop_produces_no_flight_leg(self):
        request = japan_request()
        shape = next(
            s for s in expand_shapes(request) if s.label == "台北→東京〜大阪→台北"
        )
        combo = next(iter(generate(shape, request)))

        assert len(combo.legs) == 2
        assert (combo.legs[0].from_label, combo.legs[0].to_label) == ("台北", "東京")
        assert (combo.legs[1].from_label, combo.legs[1].to_label) == ("大阪", "台北")

    def test_flying_the_internal_hop_produces_three_legs(self):
        request = japan_request(internal_links=(Link.FLY,), try_both_orders=False,
                                include_baselines=False)
        shape = expand_shapes(request)[0]
        combo = next(iter(generate(shape, request)))

        assert len(combo.legs) == 3
        assert combo.legs[1].depart_date == date(2026, 10, 4)  # 東京住 3 晚後飛

    def test_variable_stay_shifts_the_return_leg(self):
        tokyo = Waypoint("東京", ("NRT",), country_code="JP", nights_min=2, nights_max=4)
        request = japan_request(stops=(tokyo,), try_both_orders=False)
        shape = expand_shapes(request)[0]
        returns = sorted({c.legs[-1].depart_date for c in generate(shape, request)})

        assert returns == [date(2026, 10, 3), date(2026, 10, 4), date(2026, 10, 5)]

    def test_departure_window_walks_the_whole_trip(self):
        request = japan_request(
            depart_earliest=date(2026, 10, 1), depart_latest=date(2026, 10, 3)
        )
        shape = next(
            s for s in expand_shapes(request) if s.label == "台北→東京〜大阪→台北"
        )
        pairs = sorted({(c.legs[0].depart_date, c.legs[-1].depart_date)
                        for c in generate(shape, request)})

        assert pairs == [
            (date(2026, 10, 1), date(2026, 10, 6)),
            (date(2026, 10, 2), date(2026, 10, 7)),
            (date(2026, 10, 3), date(2026, 10, 8)),
        ]

    def test_one_way_has_no_return_leg(self):
        request = japan_request(stops=(TOKYO,), one_way=True, try_both_orders=False)
        shape = expand_shapes(request)[0]
        combo = next(iter(generate(shape, request)))

        assert len(combo.legs) == 1
        assert combo.legs[0].origin in ("TPE", "TSA")
        assert combo.legs[0].destination in ("NRT", "HND")


class TestAirportSubstitution:
    def test_every_airport_pairing_is_tried(self):
        request = japan_request(try_both_orders=False, include_baselines=False)
        shape = expand_shapes(request)[0]
        outbound = {(c.legs[0].origin, c.legs[0].destination) for c in generate(shape, request)}

        # 台北 2 個機場 × 東京 2 個機場
        assert outbound == {("TPE", "NRT"), ("TPE", "HND"), ("TSA", "NRT"), ("TSA", "HND")}

    def test_open_jaw_picks_arrival_and_departure_airports_independently(self):
        request = japan_request(try_both_orders=False, include_baselines=False)
        shape = expand_shapes(request)[0]
        combos_out = list(generate(shape, request))

        # 4 (台北×東京) × 4 (大阪×台北) = 16
        assert len(combos_out) == 16

    def test_route_pairs_counts_what_actually_costs_api_calls(self):
        # 日期維度是免費的(一次 calendar 呼叫涵蓋整月),只有機場對要付錢。
        request = japan_request(
            depart_earliest=date(2026, 10, 1), depart_latest=date(2026, 10, 14)
        )
        pairs = route_pairs(request)

        assert ("TPE", "NRT") in pairs
        assert ("ITM", "TSA") in pairs
        # 兩種順序 + 兩個基準來回,加起來仍遠少於組合數
        assert len(pairs) < 40


class TestCeilings:
    def test_oversized_departure_window_names_the_offender(self):
        request = japan_request(
            depart_earliest=date(2026, 10, 1), depart_latest=date(2026, 12, 1)
        )
        with pytest.raises(SpecTooLarge) as excinfo:
            expand_shapes(request)
        assert excinfo.value.offender == "depart_window"

    def test_too_many_airports_names_the_waypoint(self):
        crowded = Waypoint(
            "日本", tuple("ABCDEFG"[i] * 3 for i in range(7)), country_code="JP"
        )
        with pytest.raises(SpecTooLarge) as excinfo:
            expand_shapes(japan_request(stops=(crowded,)))
        assert excinfo.value.offender == "airports:日本"

    def test_combination_explosion_raises_instead_of_truncating(self, monkeypatch):
        # 靜默截斷會讓「被砍掉的組合」長得跟「沒有更便宜的」一模一樣 —— 這是
        # 這個工具最不該做的事,所以寧可報錯。
        monkeypatch.setattr(combos, "MAX_COMBOS", 10)
        with pytest.raises(SpecTooLarge) as excinfo:
            generate_all(japan_request())
        assert excinfo.value.offender == "combos"
        assert excinfo.value.estimate is not None and excinfo.value.estimate > 10

    def test_estimate_matches_what_is_actually_generated(self):
        request = japan_request(
            depart_earliest=date(2026, 10, 1), depart_latest=date(2026, 10, 5)
        )
        for shape in expand_shapes(request):
            assert count_combos(shape, request) == len(list(generate(shape, request)))


class TestComboIdentity:
    def test_key_is_stable_and_distinguishes_itineraries(self):
        request = japan_request()
        all_combos = generate_all(request)
        keys = [c.key for c in all_combos]

        assert len(keys) == len(set(keys)), "每個組合的 key 必須唯一"
        assert generate_all(request)[0].key == all_combos[0].key, "key 必須可重現"

    def test_rejects_a_stay_range_that_cannot_be_satisfied(self):
        with pytest.raises(ValueError):
            Waypoint("東京", ("NRT",), nights_min=5, nights_max=2)

    def test_rejects_a_waypoint_with_no_airports(self):
        with pytest.raises(ValueError):
            Waypoint("無處", ())
