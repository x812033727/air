"""把「這一段沒有價格」變成一句使用者能照著做的話。

單趟搜尋跟倒買法都會遇到空價格,而空價格在畫面上長得都一樣 —— 一個空格。
問題是它底下有四種完全不同的處境,對應四種不同的下一步:

| 情況 | 意思 | 下一步 |
|---|---|---|
| `not_fetched` | 這條航線這個月根本沒查過 | 重跑一次 |
| `route_empty` | 查過了,整個月一天都沒有 | 換機場或換月份 |
| `nearby` | 附近幾天有價 | 挪個一兩天 |
| `far_only` | 這個月有價,但都離很遠 | 那趟旅行要整個移 |

外加一條跟日期無關的出路:**同城的其他機場那天有價**。大阪的 ITM→TPE 在
2026-12 只有 3 天有價,同城的 KIX→TPE 有 28 天 —— 同一天、同一個城市、
換一個航廈就有票。

序列化集中在這裡,是因為兩個呼叫端各寫一份的下場已經發生過一次:
「附近有價的日子」先做進單趟搜尋,倒買法漏掉,使用者看到的就是一句
沒有下文的「查無資料」。
"""

from __future__ import annotations

from typing import Any

from app.pricing.cached import Gap

# 每一種 reason 對應的說法。放在資料裡而不是散在兩個 f-string 裡,
# 因為這四句話的差別就是這個功能本身。
REASON_TEXT = {
    "not_fetched": "這條航線{month}還沒查過(不是沒有班機)",
    "route_empty": "整個 {month} 這條航線一天都沒有快取價 —— 沒人搜過,不是沒有班機",
    "nearby": "這天沒有,但附近幾天有",
    "far_only": "這天沒有,{month} 有價的日子離得比較遠",
}


def serialise(gap: Gap | None) -> dict[str, Any]:
    """`{"alternatives": [...], "gap": {...}}`,可以直接展進航段的 dict。

    `alternatives` 維持原本的形狀 —— 前端已經在讀它,而這次的重點是補上
    「為什麼」,不是把既有的東西換掉。
    """
    if gap is None:
        return {"alternatives": [], "gap": None}
    return {
        "alternatives": [
            {
                "date": point.depart_date.isoformat(),
                "price": point.price,
                "days_away": (point.depart_date - gap.target).days,
            }
            for point in gap.nearby
        ],
        "gap": {
            "reason": gap.reason,
            "text": REASON_TEXT[gap.reason].format(month=gap.month),
            "month": gap.month,
            "month_rows": gap.month_rows,
            "same_city": [
                {
                    "origin": point.origin,
                    "destination": point.destination,
                    "date": point.depart_date.isoformat(),
                    "price": point.price,
                }
                for point in gap.same_city
            ],
        },
    }
