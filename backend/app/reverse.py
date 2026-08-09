"""倒買法(反向票):兩趟旅行交叉綁票。

同樣去日本兩次,不買兩張普通來回,而是把四段航程重新配對成兩張**開口來回票**:

    普通買法   票1: 台北→東京→台北        票2: 台北→大阪→台北
    倒買法     票1: 台北→東京 ＋ 大阪→台北  票2: 東京→台北 ＋ 台北→大阪
                  (包覆票)                    (倒買票)

省錢的來源是「來回票價不是兩張單程相加」—— 航空公司按航點組合定價。

**這個模組不算價格,而且那不是還沒做完。** 實測 Travelpayouts 對台灣出發的航線
一列來回快取都沒有(`one_way=false` 回 0 列;莫斯科、倫敦航線則各有 100 與 11 列)。
既然省錢的來源就是來回計價,用手上的單程價加總算出來的數字,保證看不到那個效果 ——
顯示它會比不顯示更糟。所以這裡產出的是**組票與連結**,價格交給訂票網站。

數字只掛在**單程票**上(四段全拆的四張、單程＋反向的兩張),因為單程價我們真的有。
倒買票永遠不猜價 —— 它省錢的機制就是來回計價,拿單程價拼它必然算錯。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date
from typing import Iterator, Sequence

from app.combos import Combo, FlightLeg, SpecTooLarge

# 兩趟行程、每趟兩段,機場替代已經是 (家 × 城市)² 的規模。上限比單趟搜尋緊,
# 因為這裡每一個組合都要產生四張票、每張票三個深連結。
MAX_AIRPORTS_PER_PLACE = 4
MAX_PLANS = 2_000


class TripsOverlap(ValueError):
    """第二趟必須整個在第一趟之後。

    每張票的兩段都必須依序使用,所以行程一旦重疊,交叉出來的票在時序上就不合法 ——
    這不是「比較差的選項」,是根本開不出來的票。
    """


@dataclass(frozen=True)
class Trip:
    """一趟旅行:去一個城市,某天出發、某天回來。"""

    label: str
    airports: tuple[str, ...]
    depart: date
    back: date

    def __post_init__(self) -> None:
        if not self.airports:
            raise ValueError(f"{self.label} 沒有可用的機場")
        if self.back < self.depart:
            raise ValueError(f"{self.label} 的回程日早於去程日")


@dataclass(frozen=True)
class Ticket:
    """一張票。兩段的順序就是必須搭乘的順序。"""

    label: str
    role: str
    legs: tuple[FlightLeg, ...]
    # 這張票能不能誠實標價。只有單程票是 True:單程快取價就是單程票的真價;
    # 按來回計價開的票(來回、開口)用單程價拼必然算錯,所以永遠是 False。
    priceable: bool = False
    # 「A」或「B」,以及每一段的代號(A1、A2…)。倒買法談的是「哪一段在哪張票上」,
    # 而那件事光看兩條航線圖是看不出來的 —— 四段長得都像去程或回程。
    code: str = ""
    leg_codes: tuple[str, ...] = ()
    note: str | None = None

    @property
    def combo(self) -> Combo:
        """轉成既有的 Combo 型別,好沿用深連結與計價那套程式。"""
        return Combo(legs=self.legs, shape_label=self.label, is_baseline=False)

    @property
    def has_gap(self) -> bool:
        """票上某一段的落地機場,不是下一段的起飛機場。

        在單趟旅行裡這代表要自己走(新幹線之類)。在倒買法裡**不代表** ——
        那個缺口是另一張票飛掉的,這正是交叉綁票的定義。所以這個屬性只描述形狀,
        不下「要自己移動」的結論。
        """
        return any(a.destination != b.origin for a, b in zip(self.legs, self.legs[1:]))

    @property
    def is_open_jaw(self) -> bool:
        """不是單純的原地來回。

        兩種都算:中間有缺口(飛進成田、從關西回來),或**兩端不同**
        (倒買票是 NRT→台北→KIX,中間連續但起點終點不一樣)。倒買法的兩張票
        剛好一種一個,漏掉後者會把倒買票誤判成普通來回。

        單程票不算 —— 它的起訖當然不同,但那是單程的定義,不是開口。
        """
        if len(self.legs) < 2:
            return False
        if self.has_gap:
            return True
        return self.legs[0].origin != self.legs[-1].destination


@dataclass(frozen=True)
class TripOrder:
    """一趟旅行當天實際會飛的兩段,依序。

    這是倒買法唯一真正難懂的地方:票是交叉的,但**飛的順序是正常的**。
    第一趟照樣是去了再回來,只是回程那張票上印的出發地是外站。沒有這張對照表,
    使用者看著兩張交叉的票會以為自己得照票面順序飛。
    """

    label: str
    depart: date
    back: date
    codes: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    """一種買法:買哪幾張票,以及它有什麼風險。"""

    method: str
    method_label: str
    tickets: tuple[Ticket, ...]
    priceable: bool
    unavailable_reason: str | None = None
    sequence: tuple[TripOrder, ...] = ()

    @property
    def legs(self) -> tuple[FlightLeg, ...]:
        return tuple(leg for ticket in self.tickets for leg in ticket.legs)


NO_ROUND_TRIP_DATA = (
    "台灣出發的航線沒有來回票的快取資料,而倒買法省的就是來回計價,"
    "所以這裡不猜價格。請用右邊的連結各自查真價再比。"
)

HYBRID_NO_TOTAL = (
    "兩張單程有快取價(標在各張票上),但倒買票是按來回計價開的票,"
    "台灣出發的航線沒有來回快取資料。缺一張就不算總價 —— "
    "把有價的兩張加一加當總價,比不顯示更會誤導。"
)


def build_plans(home: Sequence[str], first: Trip, second: Trip) -> list[Plan]:
    """把兩趟行程攤成四種買法。

    `home` 是出發地的候選機場(台北 = TPE + TSA)。這個函式只處理**一組**具體的
    機場選擇;跨機場的窮舉在 :func:`enumerate_plans`。
    """
    if second.depart <= first.back:
        raise TripsOverlap(
            f"「{second.label}」的出發日({second.depart})必須晚於"
            f"「{first.label}」的回程日({first.back})。"
            "倒買法把兩趟的航段交叉綁在同一張票上,而同一張票必須依序使用,"
            "所以兩趟不能重疊。"
        )

    home_out, home_back = home[0], home[-1]
    a_out = FlightLeg(home_out, first.airports[0], first.depart, "家", first.label)
    a_back = FlightLeg(first.airports[-1], home_back, first.back, first.label, "家")
    b_out = FlightLeg(home_out, second.airports[0], second.depart, "家", second.label)
    b_back = FlightLeg(second.airports[-1], home_back, second.back, second.label, "家")

    # 代號是**航段**的屬性,不是某一張票的:哪一段在哪張票上會隨買法變,但
    # 「A1 是第一趟的去程」從頭到尾不變,所以每種買法都能用同一套代號對照。
    #   A1 第一趟去程   B1 第一趟回程   B2 第二趟去程   A2 第二趟回程
    # 這組命名照的是流通版本的講法(A 票台北起點、B 票外站起點),不是自創的。
    codes = {a_out: "A1", a_back: "B1", b_out: "B2", b_back: "A2"}

    # 飛的順序永遠是正常的:去了再回來。交叉的是票,不是行程。
    sequence = (
        TripOrder(first.label, first.depart, first.back, ("A1", "B1")),
        TripOrder(second.label, second.depart, second.back, ("B2", "A2")),
    )

    normal = Plan(
        method="normal",
        method_label="普通買法:兩張各自的來回票",
        tickets=(
            Ticket(f"台北→{first.label}→台北", "第一趟來回", (a_out, a_back),
                   leg_codes=("A1", "B1")),
            Ticket(f"台北→{second.label}→台北", "第二趟來回", (b_out, b_back),
                   leg_codes=("B2", "A2")),
        ),
        priceable=False,
        unavailable_reason=NO_ROUND_TRIP_DATA,
        sequence=sequence,
    )

    # 交叉:每張票各拿一趟的去程與另一趟的回程。兩張都是開口來回票。
    reverse = Plan(
        method="reverse",
        method_label="反向機票:兩張交叉的來回票",
        tickets=(
            Ticket(
                f"台北→{first.label} ＋ {second.label}→台北",
                "包覆票",
                (a_out, b_back),
                code="A",
                leg_codes=("A1", "A2"),
                note="台北起點。第一趟的去程 ＋ 第二趟的回程",
            ),
            Ticket(
                f"{first.label}→台北 ＋ 台北→{second.label}",
                "倒買票",
                (a_back, b_out),
                code="B",
                leg_codes=("B1", "B2"),
                note=f"{first.label}起點 —— 省錢與便宜商務艙就來自這張",
            ),
        ),
        priceable=False,
        unavailable_reason=NO_ROUND_TRIP_DATA,
        sequence=sequence,
    )

    # 混搭:只留倒買票(外站出發那張 —— 省錢的機制就在外站計價,商務艙也常便宜),
    # 包覆票的兩段拆成兩張單程交給廉航。鏡像變體(留包覆票、拆倒買票)刻意不做:
    # 包覆票只是兩段台灣出發的航段黏在一起,沒有外站計價可佔便宜。
    hybrid = Plan(
        method="hybrid",
        method_label="單程＋反向:兩張單程＋一張倒買票",
        tickets=(
            Ticket(
                f"{a_out.origin}→{a_out.destination}", "單程", (a_out,),
                priceable=True, leg_codes=("A1",),
            ),
            Ticket(
                f"{first.label}→台北 ＋ 台北→{second.label}",
                "倒買票",
                (a_back, b_out),
                code="B",
                leg_codes=("B1", "B2"),
            ),
            Ticket(
                f"{b_back.origin}→{b_back.destination}", "單程", (b_back,),
                priceable=True, leg_codes=("A2",),
            ),
        ),
        priceable=False,
        unavailable_reason=HYBRID_NO_TOTAL,
        sequence=sequence,
    )

    # 四張單程。這是唯一整個買法都有資料可以算的。
    split = Plan(
        method="split",
        method_label="四段全拆:四張單程票",
        tickets=tuple(
            Ticket(f"{leg.origin}→{leg.destination}", "單程", (leg,),
                   priceable=True, leg_codes=(codes[leg],))
            for leg in (a_out, a_back, b_out, b_back)
        ),
        priceable=True,
        sequence=sequence,
    )

    return [normal, reverse, hybrid, split]


def enumerate_plans(
    home: Sequence[str], first: Trip, second: Trip, *, try_both_orders: bool = True
) -> list[list[Plan]]:
    """窮舉機場替代(以及哪一趟先)之後的所有買法組。

    機場維度跟單趟搜尋是同一回事:東京可以是成田也可以是羽田,而兩者的價差是真的
    (實測同一個月 TPE→NRT 與 TPE→HND 的價格交集為 0)。
    """
    for place in (home, first.airports, second.airports):
        if len(place) > MAX_AIRPORTS_PER_PLACE:
            raise SpecTooLarge(
                f"每個地點最多選 {MAX_AIRPORTS_PER_PLACE} 個機場,"
                f"目前有 {len(place)} 個。倒買法每組要產四張票,組合長得比單趟搜尋快。",
                offender="airports",
            )

    orders = [(first, second)]
    if try_both_orders:
        # 哪一趟先走是可以換的 —— 只要日期跟著換。這裡不自作主張改日期,
        # 所以只有在兩趟的日期窗真的可以對調時才產出反向那組。
        swapped = _swap_dates(first, second)
        if swapped is not None:
            orders.append(swapped)

    plans: list[list[Plan]] = []
    for a, b in orders:
        for home_pair, a_pair, b_pair in itertools.product(
            _pairs(home), _pairs(a.airports), _pairs(b.airports)
        ):
            plans.append(
                build_plans(
                    home_pair,
                    Trip(a.label, a_pair, a.depart, a.back),
                    Trip(b.label, b_pair, b.depart, b.back),
                )
            )
            if len(plans) > MAX_PLANS:
                raise SpecTooLarge(
                    f"這組條件會產生超過 {MAX_PLANS:,} 種組合,請減少候選機場。",
                    offender="plans",
                    estimate=len(plans),
                )
    return plans


def _swap_dates(first: Trip, second: Trip) -> tuple[Trip, Trip] | None:
    """把兩個目的地對調、日期留在原位。

    也就是「先去大阪再去東京」,而不是「把東京那趟往後搬」。日期是使用者訂的,
    這裡不動它。
    """
    if first.label == second.label:
        return None
    return (
        Trip(second.label, second.airports, first.depart, first.back),
        Trip(first.label, first.airports, second.depart, second.back),
    )


def _pairs(airports: Sequence[str]) -> Iterator[tuple[str, str]]:
    """一個地點的(去程用、回程用)機場組合。

    刻意允許兩者不同:飛進成田、從羽田回來是完全正常的,而且常常比較便宜。
    """
    return itertools.product(airports, repeat=2)


def risks(plan: Plan) -> list[str]:
    """這種買法實際上會出什麼事,照實說。

    倒買法最大的風險不是價格,是**承諾**:兩張票各自把兩趟旅行綁在一起,
    第一趟沒去,第二趟的機票就跟著失效。那是一個決定,不是一個搜尋結果。
    """
    notes: list[str] = []

    if plan.method == "reverse":
        notes.append(
            "同一張票必須依序使用:第一趟的航段沒搭或取消,同一張票上第二趟的航段會自動失效"
        )
        notes.append("兩張票要同時買 —— 這是「我確定兩趟都會去」換來的價差,不是一個搜尋結果")
        notes.append("兩趟旅行綁在一起,改期或退票要同時處理兩趟")
        notes.append("傳統航空的旺季最有感;廉航促銷常常還是更便宜,一定要點開比過再決定")
    if plan.method == "hybrid":
        notes.append(
            "倒買票一張就綁住兩趟:第一趟的回程沒搭或取消,同一張票上第二趟的去程會自動失效"
        )
        notes.append("兩張單程各自獨立訂票,跟倒買票之間行李不直掛,誤點沒有人負責銜接")
    if plan.method == "split":
        notes.append("四段各自獨立訂票,行李不直掛,任一段誤點都沒有人負責下一段")
    if plan.method == "normal":
        notes.append("兩張票互不相干,最有彈性,但通常也最貴")

    # 這裡刻意**不**為票上的缺口加註「中間要自己移動」。倒買法的包覆票確實會在
    # 東京落地、下一段從大阪起飛,但那中間你是搭另一張票飛回台北再飛出去的 ——
    # 缺口是被另一張票蓋住的。照單趟旅行的邏輯套上去,會叫使用者去搭一趟根本不
    # 存在的陸路。
    return notes
