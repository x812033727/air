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
from datetime import date, timedelta
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
    # `code` 是這張票從哪裡出發(台北出發 / 東京出發),`leg_codes` 是每一段
    # 在使用者腦中的名字(東京 去程、大阪 回程)。倒買法真正難懂的是
    # 「哪一段印在哪張票上」,而那件事光看兩條航線圖看不出來。
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
    "這兩張都是來回票,而台灣出發的航線沒有來回票的快取資料。"
    "反向機票省的就是來回計價,拿單程價去拼必然算錯 —— 所以這裡不猜,"
    "兩張票的真價請點各自的連結查。"
)

HYBRID_NO_TOTAL = (
    "兩張單程有快取價(標在各張票上),但綁在一起的那張是來回票,"
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
    # 「東京 去程」不管在哪種買法裡都是同一段,所以四種買法共用同一套名字。
    # 名詞只留三個:**去程、回程、第幾張票**。
    # 原本是 A 票 / B 票 / 包覆票 / 倒買票 / 開口 / A1 / A2 / B1 / B2 ——
    # 一張票上同時掛四個標籤,而其中沒有一個講的是「你要做什麼」。
    # 使用者回報看不懂,那就是看不懂。
    codes = {
        a_out: f"{first.label} 去程",
        a_back: f"{first.label} 回程",
        b_out: f"{second.label} 去程",
        b_back: f"{second.label} 回程",
    }

    # 飛的順序永遠是正常的:去了再回來。交叉的是票,不是行程。
    sequence = (
        TripOrder(first.label, first.depart, first.back, ("第 1 張票", "第 2 張票")),
        TripOrder(second.label, second.depart, second.back, ("第 2 張票", "第 1 張票")),
    )

    normal = Plan(
        method="normal",
        method_label="普通買法:兩張各自的來回票",
        tickets=(
            Ticket(f"台北→{first.label}→台北", "第 1 張票", (a_out, a_back),
                   leg_codes=(codes[a_out], codes[a_back])),
            Ticket(f"台北→{second.label}→台北", "第 2 張票", (b_out, b_back),
                   leg_codes=(codes[b_out], codes[b_back])),
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
                "第 1 張票",
                (a_out, b_back),
                code="台北出發",
                leg_codes=(codes[a_out], codes[b_back]),
                note=f"{first.label}的去程,加上{second.label}回家那段",
            ),
            Ticket(
                f"{first.label}→台北 ＋ 台北→{second.label}",
                "第 2 張票",
                (a_back, b_out),
                code=f"{first.label}出發",
                leg_codes=(codes[a_back], codes[b_out]),
                note=f"{first.label}回家那段,加上{second.label}的去程 —— 便宜的是這張",
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
                priceable=True, leg_codes=(codes[a_out],),
            ),
            Ticket(
                f"{first.label}→台北 ＋ 台北→{second.label}",
                "綁在一起的那張",
                (a_back, b_out),
                code=f"{first.label}出發",
                leg_codes=(codes[a_back], codes[b_out]),
            ),
            Ticket(
                f"{b_back.origin}→{b_back.destination}", "單程", (b_back,),
                priceable=True, leg_codes=(codes[b_back],),
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


@dataclass(frozen=True)
class TripWindow:
    """一趟旅行的**區間**:大概什麼時候出發、玩幾天,哪些機場都可以。

    固定成四個確切日期是這個功能一直沒有價格的原因之一。上游的快取是別人搜出來的,
    同一條航線某幾天有價、某幾天沒有,而使用者**沒有辦法知道是哪幾天** ——
    實測同一組行程只把第二趟從 10/20 挪到 10/06,可算出總價的機場組合就從 0 變成 36。
    讓日期在區間內浮動,等於把「猜中有資料的那天」這件事從使用者身上拿掉。
    """

    label: str
    airports: tuple[str, ...]
    depart_earliest: date
    depart_latest: date
    nights_min: int
    nights_max: int

    def __post_init__(self) -> None:
        if not self.airports:
            raise ValueError(f"{self.label} 沒有可用的機場")
        if self.depart_latest < self.depart_earliest:
            raise ValueError(f"{self.label} 的出發區間結束早於開始")
        if self.nights_min < 0 or self.nights_max < self.nights_min:
            raise ValueError(f"{self.label} 的天數範圍不合理")

    def options(self) -> Iterator[tuple[date, date]]:
        """(出發日, 回程日) 的每一種可能。"""
        span = (self.depart_latest - self.depart_earliest).days
        for offset in range(span + 1):
            depart = self.depart_earliest + timedelta(days=offset)
            for nights in range(self.nights_min, self.nights_max + 1):
                yield depart, depart + timedelta(days=nights)


@dataclass(frozen=True)
class Leg:
    """一段單程,以及它的快取價(沒有就是 None)。"""

    origin: str
    destination: str
    day: date
    price: float | None


@dataclass(frozen=True)
class Half:
    """一趟旅行選定的機場與日期,以及它那兩段單程的價。

    倒買法的兩張票橫跨兩趟,但**四段全拆的總價是可分離的** —— 每一段只屬於一趟。
    所以兩趟可以各自挑最便宜的,再相加,不必窮舉兩趟的乘積(那是 9 百萬種)。
    """

    trip: Trip
    out: Leg
    back: Leg

    @property
    def total(self) -> float | None:
        if self.out.price is None or self.back.price is None:
            return None
        return self.out.price + self.back.price

    @property
    def missing(self) -> tuple[Leg, ...]:
        return tuple(leg for leg in (self.out, self.back) if leg.price is None)


def best_halves(
    home_out: str,
    home_back: str,
    window: TripWindow,
    price,
    *,
    limit: int = 3,
) -> list[Half]:
    """這一趟旅行最便宜的幾種(機場 × 日期)選法,有價的排前面。

    `price(origin, destination, day)` 回傳快取價或 None。傳進來而不是直接查 DB,
    是為了讓排序邏輯可以用一個字典完整測完。
    """
    halves: list[Half] = []
    for out_airport, back_airport in _pairs(window.airports):
        for depart, back in window.options():
            out = Leg(home_out, out_airport, depart, price(home_out, out_airport, depart))
            home = Leg(back_airport, home_back, back, price(back_airport, home_back, back))
            halves.append(
                Half(
                    trip=Trip(window.label, (out_airport, back_airport), depart, back),
                    out=out,
                    back=home,
                )
            )
    # 有總價的優先,再照便宜排;完全沒價的照日期排,至少是可預期的順序。
    halves.sort(
        key=lambda h: (
            h.total is None,
            h.total if h.total is not None else 0.0,
            len(h.missing),
            h.trip.depart,
        )
    )
    return halves[:limit]


@dataclass(frozen=True)
class Combination:
    """兩趟都選定之後的一整組買法。"""

    home: tuple[str, str]
    first: Half
    second: Half
    plans: tuple[Plan, ...]

    @property
    def split_total(self) -> float | None:
        """四段全拆單程的總價。缺一段就是 None —— 絕不部分加總。"""
        a, b = self.first.total, self.second.total
        return None if a is None or b is None else a + b

    @property
    def missing(self) -> tuple[Leg, ...]:
        return self.first.missing + self.second.missing


def rank_combinations(
    home: Sequence[str],
    first: TripWindow,
    second: TripWindow,
    price,
    *,
    limit: int = 6,
) -> list[Combination]:
    """把兩個區間變成一份**排好序**的具體買法清單,有價格的在最前面。

    原本的做法是照 `itertools.product` 的順序產生 144 種組合、取前 12、前端顯示第 0 種。
    那個順序跟「哪一種有價」完全無關 —— 實測回傳的 12 組全部以 TPE→HND 開頭
    (變化的是最後一個維度),HND 沒資料就 12 組全滅,而同一批裡有 36 種是有價的。

    可分離性讓這件事很便宜:每一段單程只屬於其中一趟,所以兩趟各自挑完再相加,
    不必窮舉兩趟的乘積。
    """
    for place in (home, first.airports, second.airports):
        if len(place) > MAX_AIRPORTS_PER_PLACE:
            raise SpecTooLarge(
                f"每個地點最多選 {MAX_AIRPORTS_PER_PLACE} 個機場,目前有 {len(place)} 個。",
                offender="airports",
            )

    combos: list[Combination] = []
    for home_out, home_back in _pairs(tuple(home)):
        firsts = best_halves(home_out, home_back, first, price)
        seconds = best_halves(home_out, home_back, second, price)
        for a in firsts:
            for b in seconds:
                if b.trip.depart <= a.trip.back:
                    continue  # 第二趟必須整個在第一趟之後
                combos.append(
                    Combination(
                        home=(home_out, home_back),
                        first=a,
                        second=b,
                        plans=tuple(build_plans((home_out, home_back), a.trip, b.trip)),
                    )
                )

    combos.sort(
        key=lambda c: (
            c.split_total is None,
            c.split_total if c.split_total is not None else 0.0,
            len(c.missing),
            c.first.trip.depart,
        )
    )
    return combos[:limit]


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
        notes.append("每一趟都還是正常地去了再回來,交叉的是票,不是行程")
        notes.append("兩趟旅行綁在一起,改期或退票要同時處理兩趟")
        notes.append("傳統航空的旺季最有感;廉航促銷常常還是更便宜,一定要點開比過再決定")
    if plan.method == "hybrid":
        notes.append(
            "綁在一起的那張一票綁住兩趟:第一趟的回程沒搭或取消,同一張票上第二趟的去程會自動失效"
        )
        notes.append("兩張單程各自獨立訂票,跟那張來回票之間行李不直掛,誤點沒有人負責銜接")
    if plan.method == "split":
        notes.append("四段各自獨立訂票,行李不直掛,任一段誤點都沒有人負責下一段")
    if plan.method == "normal":
        notes.append("兩張票互不相干,最有彈性,但通常也最貴")

    # 這裡刻意**不**為票上的缺口加註「中間要自己移動」。倒買法的包覆票確實會在
    # 東京落地、下一段從大阪起飛,但那中間你是搭另一張票飛回台北再飛出去的 ——
    # 缺口是被另一張票蓋住的。照單趟旅行的邏輯套上去,會叫使用者去搭一趟根本不
    # 存在的陸路。
    return notes
