"""倒買法組票的測試。

這個模組不算價格,所以測試的重點全在**組票的正確性**:哪幾段綁在同一張票上、
順序對不對、什麼情況下這張票根本開不出來。組錯了不會有任何地方報錯,使用者只會
拿著一組永遠訂不成的連結去訂票網站,然後以為是網站的問題。

情境沿用文章的例子:台北出發,去東京一趟、去大阪一趟。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.combos import SpecTooLarge
from app.reverse import (
    Plan,
    Ticket,
    Trip,
    TripsOverlap,
    build_plans,
    enumerate_plans,
    risks,
)

HOME = ("TPE",)
TOKYO = Trip("東京", ("NRT",), depart=date(2026, 10, 5), back=date(2026, 10, 10))
OSAKA = Trip("大阪", ("KIX",), depart=date(2026, 12, 6), back=date(2026, 12, 11))


def plan_of(plans: list[Plan], method: str) -> Plan:
    return next(p for p in plans if p.method == method)


def legs_of(ticket: Ticket) -> list[str]:
    return [f"{leg.origin}→{leg.destination}@{leg.depart_date.isoformat()}" for leg in ticket.legs]


class TestTheFourBuyingMethods:
    def test_all_four_are_produced(self):
        methods = {p.method for p in build_plans(HOME, TOKYO, OSAKA)}
        assert methods == {"normal", "reverse", "hybrid", "split"}

    def test_normal_is_two_plain_round_trips(self):
        plan = plan_of(build_plans(HOME, TOKYO, OSAKA), "normal")
        assert legs_of(plan.tickets[0]) == ["TPE→NRT@2026-10-05", "NRT→TPE@2026-10-10"]
        assert legs_of(plan.tickets[1]) == ["TPE→KIX@2026-12-06", "KIX→TPE@2026-12-11"]

    def test_reverse_crosses_the_two_trips(self):
        """這是整個功能的定義:包覆票拿第一趟的去程配第二趟的回程,倒買票拿剩下兩段。"""
        plan = plan_of(build_plans(HOME, TOKYO, OSAKA), "reverse")
        wrapper, reverse_ticket = plan.tickets

        assert wrapper.role == "包覆票"
        assert legs_of(wrapper) == ["TPE→NRT@2026-10-05", "KIX→TPE@2026-12-11"]

        assert reverse_ticket.role == "倒買票"
        assert legs_of(reverse_ticket) == ["NRT→TPE@2026-10-10", "TPE→KIX@2026-12-06"]

    def test_split_is_four_separate_one_ways(self):
        plan = plan_of(build_plans(HOME, TOKYO, OSAKA), "split")
        assert len(plan.tickets) == 4
        assert all(len(t.legs) == 1 for t in plan.tickets)

    def test_the_same_four_flights_appear_in_every_method(self):
        """四種買法飛的是同樣四段,差別只在怎麼綁票。"""
        plans = build_plans(HOME, TOKYO, OSAKA)
        signatures = {
            p.method: sorted(f"{l.origin}→{l.destination}@{l.depart_date}" for l in p.legs)
            for p in plans
        }
        assert (
            signatures["normal"]
            == signatures["reverse"]
            == signatures["hybrid"]
            == signatures["split"]
        )


class TestHybrid:
    """單程＋反向:兩張單程包住一張倒買票。

    這是圖解文章示範的買法 —— 倒買票以外站(第一趟的目的地)為出發地計價,
    省錢與便宜商務艙都來自那張;其餘兩段拆成單程交給廉航。
    """

    def test_hybrid_is_two_one_ways_around_the_reverse_ticket(self):
        plan = plan_of(build_plans(HOME, TOKYO, OSAKA), "hybrid")
        out, reverse_ticket, back = plan.tickets

        assert (out.role, reverse_ticket.role, back.role) == ("單程", "倒買票", "單程")
        assert legs_of(out) == ["TPE→NRT@2026-10-05"]
        assert legs_of(reverse_ticket) == ["NRT→TPE@2026-10-10", "TPE→KIX@2026-12-06"]
        assert legs_of(back) == ["KIX→TPE@2026-12-11"]

    def test_the_kept_ticket_is_exactly_the_reverse_plans_dao_mai_ticket(self):
        """留下的必須是倒買票(外站出發那張),不是包覆票 —— 包覆票只是兩段
        台灣出發的航段黏在一起,沒有外站計價可佔便宜,所以鏡像變體刻意不做。"""
        plans = build_plans(HOME, TOKYO, OSAKA)
        kept = plan_of(plans, "hybrid").tickets[1]
        dao_mai = next(t for t in plan_of(plans, "reverse").tickets if t.role == "倒買票")
        assert kept.legs == dao_mai.legs

    def test_only_the_reverse_ticket_is_open_jaw(self):
        plan = plan_of(build_plans(HOME, TOKYO, OSAKA), "hybrid")
        assert tuple(t.is_open_jaw for t in plan.tickets) == (False, True, False)
        # 單一出發機場時倒買票中間是連續的(NRT→TPE、TPE→KIX),開口在兩端。
        assert plan.tickets[1].has_gap is False

    def test_only_the_one_way_tickets_are_priceable(self):
        """單程票有真的單程快取價可標;倒買票是按來回計價開的票,
        用單程價拼它就是整個模組拒絕顯示的那種保證錯的數字。"""
        plans = build_plans(HOME, TOKYO, OSAKA)
        hybrid = plan_of(plans, "hybrid")
        assert tuple(t.priceable for t in hybrid.tickets) == (True, False, True)
        assert all(t.priceable for t in plan_of(plans, "split").tickets)
        assert not any(t.priceable for t in plan_of(plans, "normal").tickets)
        assert not any(t.priceable for t in plan_of(plans, "reverse").tickets)

    def test_a_priceable_plan_only_contains_priceable_tickets(self):
        """plan 級的 priceable 意思是「總價是誠實的」,所以它蘊含每張票都可標價。"""
        for plan in build_plans(HOME, TOKYO, OSAKA):
            if plan.priceable:
                assert all(t.priceable for t in plan.tickets)


class TestChronology:
    def test_every_ticket_has_its_legs_in_travel_order(self):
        """票上的兩段必須依序使用,所以第二段的日期不能早於第一段。"""
        for plan in build_plans(HOME, TOKYO, OSAKA):
            for ticket in plan.tickets:
                dates = [leg.depart_date for leg in ticket.legs]
                assert dates == sorted(dates), f"{ticket.label} 的航段順序不對"

    def test_overlapping_trips_are_rejected(self):
        overlapping = Trip("大阪", ("KIX",), depart=date(2026, 10, 8), back=date(2026, 10, 14))
        with pytest.raises(TripsOverlap, match="必須晚於"):
            build_plans(HOME, TOKYO, overlapping)

    def test_trips_that_touch_are_rejected(self):
        """第二趟出發日 == 第一趟回程日也不行:那天你人還在飛第一張票的回程。"""
        touching = Trip("大阪", ("KIX",), depart=date(2026, 10, 10), back=date(2026, 10, 15))
        with pytest.raises(TripsOverlap):
            build_plans(HOME, TOKYO, touching)

    def test_a_trip_that_returns_before_it_departs_is_rejected(self):
        with pytest.raises(ValueError, match="回程日早於去程日"):
            Trip("東京", ("NRT",), depart=date(2026, 10, 10), back=date(2026, 10, 5))


class TestOpenJaw:
    def test_both_reverse_tickets_are_open_jaw(self):
        """倒買法的兩張票都是開口票 —— 這正是它跟普通來回不同的地方。"""
        plan = plan_of(build_plans(HOME, TOKYO, OSAKA), "reverse")
        assert all(t.is_open_jaw for t in plan.tickets)

    def test_the_two_reverse_tickets_are_open_in_different_ways(self):
        """包覆票的缺口在中間(東京落地、關西起飛);倒買票中間是連續的,
        開口在兩端(NRT 出發、KIX 結束)。只看中間會把倒買票誤判成普通來回。"""
        wrapper, reverse_ticket = plan_of(build_plans(HOME, TOKYO, OSAKA), "reverse").tickets
        assert wrapper.has_gap is True
        assert reverse_ticket.has_gap is False
        assert reverse_ticket.legs[0].origin != reverse_ticket.legs[-1].destination

    def test_plain_round_trips_are_not_open_jaw(self):
        plan = plan_of(build_plans(HOME, TOKYO, OSAKA), "normal")
        assert not any(t.is_open_jaw for t in plan.tickets)

    def test_a_one_way_is_not_an_open_jaw(self):
        """單程票的起訖當然不同,但那是單程的定義,不是開口。
        標成開口會讓四張單程票看起來像四張開口票。"""
        plan = plan_of(build_plans(HOME, TOKYO, OSAKA), "split")
        assert not any(t.is_open_jaw for t in plan.tickets)


class TestPricing:
    def test_only_the_split_method_claims_a_price(self):
        """台灣航線沒有來回快取資料,而倒買法省的就是來回計價。
        用單程加總去猜,算出來的數字保證看不到那個效果。"""
        plans = build_plans(HOME, TOKYO, OSAKA)
        assert plan_of(plans, "split").priceable is True
        assert plan_of(plans, "normal").priceable is False
        assert plan_of(plans, "reverse").priceable is False
        assert plan_of(plans, "hybrid").priceable is False

    def test_every_unpriceable_method_says_why(self):
        for plan in build_plans(HOME, TOKYO, OSAKA):
            if not plan.priceable:
                assert plan.unavailable_reason
                assert "來回" in plan.unavailable_reason


class TestAirportSubstitution:
    def test_each_airport_pairing_is_enumerated(self):
        tokyo = Trip("東京", ("NRT", "HND"), depart=date(2026, 10, 5), back=date(2026, 10, 10))
        groups = enumerate_plans(("TPE",), tokyo, OSAKA, try_both_orders=False)
        # 東京去程 2 × 東京回程 2 = 4(台北與大阪各只有一個機場)
        assert len(groups) == 4

    def test_flying_into_one_airport_and_out_of_another_is_allowed(self):
        """飛進成田、從羽田回來是正常的買法,而且常常比較便宜。"""
        tokyo = Trip("東京", ("NRT", "HND"), depart=date(2026, 10, 5), back=date(2026, 10, 10))
        groups = enumerate_plans(("TPE",), tokyo, OSAKA, try_both_orders=False)
        pairs = {
            (
                plan_of(g, "normal").tickets[0].legs[0].destination,
                plan_of(g, "normal").tickets[0].legs[1].origin,
            )
            for g in groups
        }
        assert ("NRT", "HND") in pairs

    def test_visiting_order_can_be_swapped(self):
        groups = enumerate_plans(HOME, TOKYO, OSAKA, try_both_orders=True)
        firsts = {plan_of(g, "normal").tickets[0].legs[0].destination for g in groups}
        assert firsts == {"NRT", "KIX"}

    def test_swapping_keeps_the_dates_where_the_user_put_them(self):
        """對調的是目的地,不是日期 —— 日期是使用者訂的。"""
        groups = enumerate_plans(HOME, TOKYO, OSAKA, try_both_orders=True)
        for group in groups:
            first_ticket = plan_of(group, "normal").tickets[0]
            assert first_ticket.legs[0].depart_date == date(2026, 10, 5)

    def test_too_many_airports_raises_rather_than_truncating(self):
        crowded = Trip(
            "日本", ("NRT", "HND", "KIX", "ITM", "NGO"),
            depart=date(2026, 10, 5), back=date(2026, 10, 10),
        )
        with pytest.raises(SpecTooLarge, match="最多選"):
            enumerate_plans(HOME, crowded, OSAKA)


class TestRisks:
    def test_reverse_warns_that_the_trips_are_bound_together(self):
        notes = " ".join(risks(plan_of(build_plans(HOME, TOKYO, OSAKA), "reverse")))
        assert "自動失效" in notes
        assert "兩趟旅行綁在一起" in notes

    def test_split_warns_about_baggage_and_delays(self):
        notes = " ".join(risks(plan_of(build_plans(HOME, TOKYO, OSAKA), "split")))
        assert "行李不直掛" in notes

    def test_normal_is_described_as_flexible_but_usually_dearer(self):
        notes = " ".join(risks(plan_of(build_plans(HOME, TOKYO, OSAKA), "normal")))
        assert "彈性" in notes

    def test_hybrid_warns_about_the_binding_and_the_loose_one_ways(self):
        """倒買票一張就把兩趟綁在一起;兩張單程則是各自獨立的散票。"""
        notes = " ".join(risks(plan_of(build_plans(HOME, TOKYO, OSAKA), "hybrid")))
        assert "綁住兩趟" in notes
        assert "自動失效" in notes
        assert "各自獨立訂票" in notes
        assert "要自己移動" not in notes

    def test_the_gap_in_a_reverse_ticket_is_not_called_self_transfer(self):
        """包覆票確實在東京落地、下一段從關西起飛,但中間那段你是搭**另一張票**
        飛回台北再飛出去的。照單趟旅行的邏輯標成「要自己移動」,等於叫使用者去搭
        一趟根本不存在的陸路。"""
        plan = plan_of(build_plans(HOME, TOKYO, OSAKA), "reverse")
        wrapper = plan.tickets[0]
        assert wrapper.has_gap is True, "這個測試要的就是有缺口的那張票"
        assert "要自己移動" not in " ".join(risks(plan))
