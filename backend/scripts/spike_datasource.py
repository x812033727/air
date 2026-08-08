#!/usr/bin/env python3
"""Spike gate:在寫產品程式碼之前(以及每次懷疑資料源時)證明資料能用。

三個關卡,對應 docs/spike-datasource.md:

S1  單程語義 —— Aviasales 的快取是真實使用者搜尋餵出來的,而多數人搜來回。
    如果不帶 return_date 拿回來的每日價其實錨在來回票上,那把各段加總算「拼票」
    就整個是錯的,而且錯得很像真的。順便掃冷門機場的覆蓋率:一列都沒有的航線
    會靜默消失,看起來像「不便宜」而不是「不知道」。

S2  排名一致性 —— **不是**價格準確度。快取價本來就會漂,30% 誤差不影響產品
    價值,因為這工具的工作是排序組合而不是報價。真正會殺死產品的是:快取把
    A 排在 B 前面、實價卻相反。所以量的是序,不是值。

S3  速率限制 —— Travelpayouts 文件沒寫 rate limit。連續打完一次真實搜尋所需的
    呼叫量,記錄 status code 與延遲,盯 429。

用法:
    python scripts/spike_datasource.py --out docs/spike-datasource.md
    python scripts/spike_datasource.py --origin TPE --month 2026-10
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app import credentials  # noqa: E402
from app.db import closing_conn, init_db  # noqa: E402
from app.pricing import deeplinks  # noqa: E402
from app.pricing.cached import BASE_URL, parse_month_payload  # noqa: E402
from app.combos import FlightLeg  # noqa: E402

# 台北 → 日本兩城,就是這個專案的驗證標的行程。
HOME = ["TPE", "TSA"]
JAPAN = ["NRT", "HND", "KIX", "ITM", "NGO", "FUK", "CTS", "OKA"]


class Probe:
    """Every call goes through here so S3 gets its numbers for free."""

    def __init__(self, token: str, currency: str) -> None:
        self.token = token
        self.currency = currency
        self.client = httpx.Client(timeout=settings.request_timeout_s)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any]) -> tuple[int, dict[str, Any] | None, float]:
        started = time.perf_counter()
        status = 0
        payload: dict[str, Any] | None = None
        error = None
        try:
            response = self.client.get(
                url,
                params=params,
                headers={"X-Access-Token": self.token, "Accept-Encoding": "gzip, deflate"},
            )
            status = response.status_code
            if status == 200:
                payload = response.json()
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        elapsed = (time.perf_counter() - started) * 1000
        self.calls.append(
            {"url": url, "params": params, "status": status, "ms": elapsed, "error": error}
        )
        return status, payload, elapsed

    def month_matrix(self, origin: str, destination: str, month: str, **extra: Any):
        return self.get(
            f"{BASE_URL}/v2/prices/month-matrix",
            {
                "origin": origin,
                "destination": destination,
                "month": f"{month}-01",
                "currency": self.currency.lower(),
                "show_to_affiliates": "true",
                **extra,
            },
        )

    def calendar(self, origin: str, destination: str, month: str, **extra: Any):
        return self.get(
            f"{BASE_URL}/v1/prices/calendar",
            {
                "origin": origin,
                "destination": destination,
                "depart_date": month,
                "calendar_type": "departure_date",
                "currency": self.currency.lower(),
                **extra,
            },
        )

    def close(self) -> None:
        self.client.close()


# --------------------------------------------------------------------------
# S1 — one-way semantics and coverage
# --------------------------------------------------------------------------

def s1_one_way_semantics(probe: Probe, origin: str, month: str) -> list[str]:
    """回傳的每日價到底是不是單程價。

    先前這裡是用「帶 return_date vs 不帶」的價差來判斷,那是錯的方法:實測兩者
    回傳完全相同(30 列、最低價一模一樣),而正確的解讀是**這個參數被忽略了**,
    不是「不帶參數時回的是來回價」。舊版邏輯會從相同的數字推出相反的結論。

    真正的證據在每一列裡:`return_date` 是不是空的。空的就是單程。
    """
    lines = [
        "## S1a — 單程語義",
        "",
        "問題:回傳的每日價是單程價,還是錨在來回票上?若是後者,拼票總價會系統性",
        "高估,而且看起來完全合理、不會有任何地方報錯。",
        "",
        "判準看的是**每一列自己帶的 `return_date`**,不是「帶不帶參數的價差」——",
        "後者測不出東西,因為這個端點根本忽略該參數(帶與不帶回傳完全相同)。",
        "",
        "| 端點 | 航線 | 總列數 | 空 return_date | 帶回程日期 | 最低價 | 中位價 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    verdict: list[str] = []

    for endpoint_name, call in (
        ("month-matrix", probe.month_matrix),
        ("calendar", probe.calendar),
    ):
        status, payload, _ = call(origin, "KIX", month)
        if status != 200 or not payload:
            lines.append(f"| {endpoint_name} | {origin}→KIX | HTTP {status} | – | – | – | – |")
            verdict.append(f"- ❓ **{endpoint_name}**:沒有回傳資料,無法判斷。")
            continue

        raw = payload.get("data") or []
        rows = list(raw.values()) if isinstance(raw, dict) else raw
        one_way_rows = [r for r in rows if not (r.get("return_date") or r.get("return_at"))]
        round_trip_rows = len(rows) - len(one_way_rows)

        points = parse_month_payload(endpoint_name, payload, origin, "KIX", probe.currency)
        prices = sorted(p.price for p in points)
        low = prices[0] if prices else None
        mid = prices[len(prices) // 2] if prices else None

        lines.append(
            f"| {endpoint_name} | {origin}→KIX | {len(rows)} | {len(one_way_rows)} "
            f"| {round_trip_rows} | {low if low is not None else '–'} "
            f"| {mid if mid is not None else '–'} |"
        )

        if not rows:
            verdict.append(f"- ❓ **{endpoint_name}**:零列,無法判斷。")
        elif round_trip_rows == 0:
            verdict.append(
                f"- ✅ **{endpoint_name}**:{len(rows)} 列全部沒有回程日期,是單程價。"
                "拼票加總成立。"
            )
        elif len(one_way_rows) == 0:
            verdict.append(
                f"- ⚠️ **{endpoint_name}**:{len(rows)} 列全部帶回程日期,這是來回票價。"
                "拿去加總會把回家那段算兩次,必須換端點或改演算法。"
            )
        else:
            verdict.append(
                f"- ⚠️ **{endpoint_name}**:單程與來回混在一起"
                f"({len(one_way_rows)} vs {round_trip_rows} 列)。解析器必須濾掉帶"
                "回程日期的列,否則同一條航線會有兩種量綱的價格混用。"
            )

    return lines + ["", *verdict, ""]


def s1_coverage(probe: Probe, origin: str, month: str, endpoint: str) -> list[str]:
    lines = [
        "## S1b — 冷門機場覆蓋率",
        "",
        "查無資料必須能跟「不便宜」分開。列數為 0 的航線,在產品裡要顯示成",
        "「此航段查無資料」,不能讓組合靜默消失。",
        "",
        "| 航線 | 回傳列數 | HTTP | 延遲(ms) |",
        "| --- | ---: | ---: | ---: |",
    ]
    call = probe.month_matrix if endpoint == "month-matrix" else probe.calendar
    empty: list[str] = []
    for destination in JAPAN:
        status, payload, ms = call(origin, destination, month)
        count = (
            len(parse_month_payload(endpoint, payload, origin, destination, probe.currency))
            if status == 200 and payload
            else 0
        )
        if count == 0:
            empty.append(f"{origin}→{destination}")
        lines.append(f"| {origin}→{destination} | {count} | {status} | {ms:.0f} |")

    lines.append("")
    if empty:
        lines.append(
            f"- ⚠️ 共 {len(empty)} 條航線零資料:{', '.join(empty)}。"
            "這些在 UI 上必須明確標示為「查無資料」。"
        )
    else:
        lines.append("- ✅ 所有測試航線都有資料。")
    return lines + [""]


# --------------------------------------------------------------------------
# S2 — ranking agreement
# --------------------------------------------------------------------------

def s2_ranking(probe: Probe, month: str, endpoint: str) -> list[str]:
    """列出 5 組候選、算快取總價並排序,附上 Google Flights 連結供人工對序。"""
    outbound_day = date.fromisoformat(f"{month}-05")
    return_day = date.fromisoformat(f"{month}-11")
    candidates = [
        ("TPE", "NRT", "ITM", "TPE"),
        ("TPE", "NRT", "KIX", "TPE"),
        ("TPE", "HND", "ITM", "TPE"),
        ("TPE", "KIX", "NRT", "TPE"),
        ("TPE", "KIX", "HND", "TPE"),
    ]

    call = probe.month_matrix if endpoint == "month-matrix" else probe.calendar
    prices: dict[tuple[str, str], float | None] = {}
    for a, b, c, d in candidates:
        for origin, destination, day in ((a, b, outbound_day), (c, d, return_day)):
            if (origin, destination) in prices:
                continue
            status, payload, _ = call(origin, destination, month)
            points = (
                parse_month_payload(endpoint, payload, origin, destination, probe.currency)
                if status == 200 and payload
                else []
            )
            match = next((p.price for p in points if p.depart_date == day), None)
            prices[(origin, destination)] = match

    rows = []
    for a, b, c, d in candidates:
        out_price = prices.get((a, b))
        back_price = prices.get((c, d))
        total = out_price + back_price if out_price and back_price else None
        link = deeplinks.google_flights_url(
            [
                FlightLeg(a, b, outbound_day, a, b),
                FlightLeg(c, d, return_day, c, d),
            ]
        )
        rows.append((f"{a}→{b} / {c}→{d}", out_price, back_price, total, link))

    rows.sort(key=lambda r: r[3] if r[3] is not None else float("inf"))

    lines = [
        "## S2 — 排名一致性(不是價格準確度)",
        "",
        "**判準:快取價排出來的序,和實價排出來的序是否一致。**",
        "快取價穩定偏移 25% 但序不變 → 可出貨,標「參考價」即可。",
        "價差只有 5% 但序翻掉 → 產品是死的,要回頭修層一。",
        "",
        f"出發 {outbound_day} / 回程 {return_day}",
        "",
        "| # | 組合 | 去程快取價 | 回程快取價 | 快取總價 | 實價(人工填) | Google Flights |",
        "| ---: | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for index, (label, out_price, back_price, total, link) in enumerate(rows, start=1):
        lines.append(
            f"| {index} | {label} | {_fmt(out_price)} | {_fmt(back_price)} | "
            f"{_fmt(total)} | | [開啟]({link}) |"
        )
    lines += [
        "",
        "> 逐一開啟上面的連結,把實價填進「實價」欄,然後比較兩欄的**名次**。",
        "> 名次一致 → 通過;名次翻掉 → 不通過,記錄翻了哪幾組。",
        "",
    ]
    return lines


def _fmt(value: float | None) -> str:
    return f"{value:,.0f}" if value is not None else "查無資料"


# --------------------------------------------------------------------------
# S3 — rate limits
# --------------------------------------------------------------------------

def s3_rate_limits(probe: Probe) -> list[str]:
    statuses: dict[int, int] = {}
    for call in probe.calls:
        statuses[call["status"]] = statuses.get(call["status"], 0) + 1
    latencies = sorted(call["ms"] for call in probe.calls)
    throttled = statuses.get(429, 0)

    lines = [
        "## S3 — 速率限制實測",
        "",
        f"- 本次共送出 **{len(probe.calls)}** 次呼叫",
        f"- status code 分布:{statuses}",
    ]
    if latencies:
        lines += [
            f"- 延遲中位數 **{latencies[len(latencies) // 2]:.0f} ms**、"
            f"最慢 **{latencies[-1]:.0f} ms**",
        ]
    if throttled:
        lines.append(
            f"- ⚠️ 出現 **{throttled} 次 429**,必須加退避與呼叫節流才能上線。"
        )
    else:
        lines.append("- ✅ 未出現 429。這個量級的連續呼叫沒有被限流。")
    errors = [call for call in probe.calls if call["error"]]
    if errors:
        lines.append(f"- ⚠️ {len(errors)} 次呼叫拋出例外,例:{errors[0]['error']}")
    return lines + [""]


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="TPE")
    parser.add_argument(
        "--month",
        default=None,
        help="YYYY-MM,預設為兩個月後(避開近期票價的雜訊)",
    )
    parser.add_argument("--endpoint", default=None, choices=["month-matrix", "calendar"])
    parser.add_argument("--out", default=None, help="把報告寫成 markdown 檔")
    args = parser.parse_args()

    # 金鑰現在存在資料庫裡(網頁上填的),環境變數只是備援。這個腳本從 shell 跑,
    # 所以兩邊都要看,否則「網頁上明明填好了」卻跑不動。
    init_db()
    with closing_conn() as conn:
        resolved = credentials.resolve(conn)

    if not resolved.token:
        print(
            "缺少 TRAVELPAYOUTS_TOKEN。\n"
            "  1. 到 https://www.travelpayouts.com 註冊 affiliate 帳號\n"
            "  2. 取得 API token\n"
            "  3. 填進網頁的「查價金鑰」面板(或 /opt/air/.env),再重跑本腳本\n\n"
            "在拿到 token 之前,層一(快取價排名)無法運作;"
            "深連結與參考資料層則不受影響。",
            file=sys.stderr,
        )
        return 2

    month = args.month or _default_month()
    endpoint = args.endpoint or settings.price_month_endpoint
    probe = Probe(resolved.token, settings.default_currency)

    try:
        report = [
            "# Spike:資料源實測報告",
            "",
            f"- 執行時間:{datetime.now(timezone.utc).isoformat()}",
            f"- 目標月份:{month}",
            f"- 主要端點:`{endpoint}`",
            f"- 幣別:{settings.default_currency}",
            "",
            "驗證標的行程:**台北 → 日本兩城開口**。",
            "",
        ]
        report += s1_one_way_semantics(probe, args.origin, month)
        report += s1_coverage(probe, args.origin, month, endpoint)
        report += s2_ranking(probe, month, endpoint)
        report += s3_rate_limits(probe)
    finally:
        probe.close()

    text = "\n".join(report)
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"報告已寫入 {target}")
    else:
        print(text)
    return 0


def _default_month() -> str:
    today = date.today()
    month = today.month + 2
    year = today.year + (month - 1) // 12
    return f"{year:04d}-{(month - 1) % 12 + 1:02d}"


if __name__ == "__main__":
    raise SystemExit(main())
