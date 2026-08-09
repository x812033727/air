"""HTTP layer. Thin on purpose — the thinking lives in app.search."""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field

from app import airlines, credentials, gaps, refdata, reverse, search, zh_names
from app.combos import SpecTooLarge
from app.config import settings
from app.db import closing_conn, connect, init_db, source_health, utcnow
from app.password import PasswordError, change_password
from app.pricing import cached, deeplinks
from app.pricing.live import get_provider
from app.combos import Combo, FlightLeg


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with closing_conn() as conn:
        # Reference data is token-free, so there is no reason to make the user
        # do anything before the picker works.
        if refdata.is_stale(conn):
            try:
                refdata.refresh(conn)
            except Exception as exc:  # noqa: BLE001 — startup must not die on this
                app.state.refdata_error = str(exc)
    yield


app = FastAPI(title="air — 多開口機票組合規劃", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class StopIn(BaseModel):
    codes: list[str] = Field(..., min_length=1, description="城市或機場 IATA 代碼")
    nights_min: int = Field(2, ge=0, le=30)
    nights_max: int = Field(4, ge=0, le=30)
    label: str | None = None


class SearchIn(BaseModel):
    home: list[str] = Field(..., min_length=1)
    stops: list[StopIn] = Field(..., min_length=1, max_length=4)
    depart_earliest: date
    depart_latest: date
    one_way: bool = False
    try_both_orders: bool = True
    internal_links: list[Literal["fly", "surface"]] | None = None
    passengers: int = Field(1, ge=1, le=9)
    cabin: Literal["economy", "premium_economy", "business", "first"] = "economy"
    # 想搭的航空公司。**不影響排名** —— 排名用的快取價沒有航空公司欄位,
    # 所以拿它去篩會篩掉航線卻篩不掉價格,顯示的數字根本不是那家的。
    # 它只跟著深連結出去,讓點過去的搜尋只列這幾家。
    airlines: list[str] = Field(default_factory=list, max_length=20)
    # 只要直達。這個**會**改變站內的價格 —— 轉機次數我們真的有資料。
    nonstop: bool = False


class LegIn(BaseModel):
    origin: str
    destination: str
    date: date


class TripIn(BaseModel):
    """一趟旅行。日期可以給確切兩天,也可以給**區間 + 天數**。

    區間才是預設的用法。上游快取是別人搜出來的,同一條航線某幾天有價某幾天沒有,
    而使用者無從得知是哪幾天 —— 實測同一組行程只把第二趟從 10/20 挪到 10/06,
    算得出總價的機場組合就從 0 種變成 36 種。把「猜中有資料的那天」丟給使用者,
    等於保證他看到一片空白。
    """

    codes: list[str] = Field(..., min_length=1, description="城市或機場 IATA 代碼")
    depart: date | None = None
    back: date | None = None
    depart_earliest: date | None = None
    depart_latest: date | None = None
    nights_min: int = Field(4, ge=0, le=30)
    nights_max: int = Field(6, ge=0, le=30)
    label: str | None = None

    def dates(self) -> tuple[date, date, int, int]:
        """(最早出發, 最晚出發, 最少幾晚, 最多幾晚)。

        機場不在這裡處理 —— 那要查資料庫展開,而這個型別只認得使用者填的東西。
        """
        if self.depart is not None and self.back is not None and not self.depart_earliest:
            # 舊式的「確切兩天」:視為一個寬度為 0 的區間,天數固定。
            nights = (self.back - self.depart).days
            if nights < 0:
                raise ValueError("回程日早於去程日")
            return self.depart, self.depart, nights, nights

        earliest = self.depart_earliest or self.depart
        if earliest is None:
            raise ValueError("要嘛給 depart + back,要嘛給 depart_earliest")
        latest = self.depart_latest or earliest
        return earliest, latest, self.nights_min, max(self.nights_min, self.nights_max)


class ReverseIn(BaseModel):
    home: list[str] = Field(..., min_length=1)
    first: TripIn
    second: TripIn
    try_both_orders: bool = True
    # 拆單程那一欄要價格就得先抓。航線只有四段,所以直接在同一個請求裡抓完。
    warm: bool = True
    passengers: int = Field(1, ge=1, le=9)
    cabin: Literal["economy", "premium_economy", "business", "first"] = "economy"
    # 同 SearchIn:只跟著連結出去,不參與任何排名或計價。
    airlines: list[str] = Field(default_factory=list, max_length=20)
    nonstop: bool = False


class KeysIn(BaseModel):
    """**沒帶到的欄位不會被動到,帶空字串才是清除。**

    這個區別是付出代價才加上的:原本空字串一律當成刪除,所以一個只想存 Duffel
    token 的請求把 Travelpayouts 的 token 一起洗掉了,而那把金鑰救不回來。
    """

    token: str | None = None
    marker: str | None = None
    duffel: str | None = None


class PasswordIn(BaseModel):
    current: str = Field(..., min_length=1)
    new: str = Field(..., min_length=1)


class VerifyIn(BaseModel):
    legs: list[LegIn] = Field(..., min_length=1, max_length=6)
    passengers: int = Field(1, ge=1, le=9)
    cabin: Literal["economy", "premium_economy", "business", "first"] = "economy"


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------

@app.get("/api/ref/countries")
def list_countries(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    countries = refdata.list_countries(conn)
    if not countries:
        raise HTTPException(
            status_code=503,
            detail="參考資料尚未載入完成,請稍候再試(或執行 POST /api/ref/refresh)",
        )
    return {"countries": countries}


@app.get("/api/ref/countries/{code}/airports")
def country_airports(code: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    cities = refdata.cities_in_country(conn, code)
    if not cities:
        raise HTTPException(status_code=404, detail=f"找不到 {code} 的可飛機場")
    return {
        "country": code.upper(),
        "cities": [
            {
                "code": city.code,
                "name": city.name,
                "airports": [
                    {"code": a.code, "name": a.name} for a in city.airports
                ],
            }
            for city in cities
        ],
    }


@app.get("/api/ref/places")
def search_places(
    q: str = "", limit: int = 20, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """找地方。取代原本「選國家 → 從前 12 個城市裡點」的挑法。

    `q` 留空回熱門清單 —— 那是給「還沒想好要打什麼」的人的。只有搜尋框的話,
    不知道要打什麼的人一樣沒有路走,只是這次是卡在一個空白輸入框前面。

    那個挑法是**死路**,不是不方便:日本 72 個可飛城市只列得出 12 個,岡山、函館、
    石垣點不到,而畫面上寫著「日本 (77)」—— 看起來只是排在後面,實際上沒有那個
    按鈕。美國 525 個城市只列 12 個。使用者要去的地方一旦不在前 12 名,
    這個工具對他就是完全不能用,而且畫面上沒有任何一句話說明。
    """
    cities = refdata.search_cities(conn, q, limit=max(1, min(limit, 50)))
    names = refdata.country_names(conn, (c.country_code for c in cities))
    return {
        "places": [
            {
                "code": city.code,
                "name": city.name,
                "country": names.get(city.country_code, city.country_code),
                "airports": [{"code": a.code, "name": a.name} for a in city.airports],
            }
            for city in cities
        ]
    }


FX_URL = "https://open.er-api.com/v6/latest"
FX_TTL_HOURS = 12


@app.get("/api/fx")
def exchange_rates(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """匯率,基準是站台幣別。

    反向機票**一定會踩到這個**:省錢的那張票從外站出發,就用外站的幣別賣 ——
    使用者實測的星宇例子裡,大阪出發那張報的是 JPY 84,540,而台北出發那張是
    TWD 17,095。兩個數字不換算就沒辦法比,而「不能比」正是這個功能的全部意義。

    Travelpayouts 沒有匯率端點(`/data/currency.json` 回 403),所以走外部來源,
    快取 12 小時 —— 匯率不是這個站的賣點,不值得每次搜尋都打一次外網。
    """
    import json as _json

    row = conn.execute(
        "SELECT value, updated_at FROM app_settings WHERE key = 'fx'"
    ).fetchone()
    if row:
        age = utcnow() - datetime.fromisoformat(row["updated_at"])
        if age < timedelta(hours=FX_TTL_HOURS):
            return _json.loads(row["value"])

    base = settings.default_currency.upper()
    try:
        with httpx.Client(timeout=settings.request_timeout_s) as client:
            payload = client.get(f"{FX_URL}/{base}").json()
        rates = payload.get("rates") or {}
        if not rates:
            raise ValueError("匯率來源回了空的 rates")
    except Exception as exc:  # noqa: BLE001
        # 拿不到匯率不該讓整頁掛掉 —— 但也不能默默用一個過期或編造的數字。
        if row:
            return _json.loads(row["value"])
        raise HTTPException(status_code=503, detail=f"拿不到匯率:{type(exc).__name__}") from exc

    body = {"base": base, "rates": rates, "fetched_at": utcnow().isoformat()}
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES ('fx', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (_json.dumps(body), utcnow().isoformat()),
    )
    conn.commit()
    return body


@app.get("/api/ref/airlines")
def list_airlines(
    q: str = "", limit: int = 40, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """航空公司清單,給「只想搭這幾家」那個選單用。

    ⚠️ 這是一份**全球清單**,不是「這條航線有誰飛」。route-level 的資料我們有,
    但它薄到不能當選單:免費的 routes.json 裡台北出發的日本線還列著已經停業的
    復興(GE)與捷星亞洲(3K),卻沒有星宇(JX)跟台灣虎航(IT) —— 一個把
    JX 藏起來、把 GE 端出來的選單,比沒有選單更糟。

    所以選單不受航線限制,而使用者選的東西**只會跟著連結出去**:Google Flights
    與 Kayak 那邊會真的只列這幾家(兩個都實測過),站內的排名一個字都不動。
    Aviasales 沒有驗過的篩選參數,所以那顆按鈕會照實標成「未篩選」。

    沒帶 `q` 時只回收錄過的那幾家。全球有一千多家航空公司,拿字母序去補滿版面
    只會在長榮旁邊放一家「2nd Arkhangelsk United Aviation Division」——
    那不是更完整,是更難找。要找別家就打字搜尋。
    """
    query = q.strip()
    limit = max(1, min(limit, 200))
    if query:
        like = f"%{query}%"
        # 中文名、英文名、代碼三個都比對:同一個人可能打「長榮」、「EVA」
        # 或「BR」,取決於他上一次是在哪裡看到那個名字。
        rows = conn.execute(
            """
            SELECT code, name FROM airlines
            WHERE LENGTH(code) = 2
              AND (name LIKE ? OR COALESCE(name_en, '') LIKE ? OR code LIKE ?)
            ORDER BY name LIMIT ?
            """,
            (like, like, like, limit),
        ).fetchall()
        found = [{"code": row["code"], "name": row["name"]} for row in rows]
    else:
        rows = conn.execute(
            "SELECT code, name FROM airlines WHERE LENGTH(code) = 2"
        ).fetchall()
        found = sorted(
            (
                {"code": row["code"], "name": row["name"]}
                for row in rows
                if zh_names.airline_rank(row["code"]) < len(zh_names.AIRLINE_ZH)
            ),
            key=lambda a: zh_names.airline_rank(a["code"]),
        )[:limit]
    return {
        "airlines": found,
        "note": "選了只會套用在 Google Flights 與 Kayak 的連結上,不影響站內排名。",
    }


@app.post("/api/ref/refresh")
def refresh_refdata(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return {"written": refdata.refresh(conn)}


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def _to_request(body: SearchIn, conn: sqlite3.Connection):
    try:
        return search.build_request(
            conn,
            home_codes=body.home,
            stops=[
                search.StopSelection(
                    codes=tuple(stop.codes),
                    nights_min=stop.nights_min,
                    nights_max=stop.nights_max,
                    label=stop.label,
                )
                for stop in body.stops
            ],
            depart_earliest=body.depart_earliest,
            depart_latest=body.depart_latest,
            one_way=body.one_way,
            try_both_orders=body.try_both_orders,
            internal_links=body.internal_links,
            passengers=body.passengers,
            cabin=body.cabin,
            airlines=body.airlines,
            nonstop=body.nonstop,
        )
    except search.UnknownPlace as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _guarded(call):
    try:
        return call()
    except SpecTooLarge as exc:
        # Tell the user which knob to turn, not just that they turned it too far.
        raise HTTPException(
            status_code=400,
            detail={"message": exc.message, "offender": exc.offender, "estimate": exc.estimate},
        ) from exc


# A key sent with the request wins over the saved one, and is not persisted —
# it's the one-off override. The saved key (and then .env) covers everything
# else. See app/credentials.py for why the saved key is now the default.
TokenHeader = Header(default=None, alias="X-Travelpayouts-Token")
MarkerHeader = Header(default=None, alias="X-Travelpayouts-Marker")


@app.post("/api/search/warm")
def warm_search(
    body: SearchIn,
    conn: sqlite3.Connection = Depends(get_conn),
    x_travelpayouts_token: str | None = TokenHeader,
) -> dict[str, Any]:
    """預抓這次搜尋需要的航線月份資料。

    這是慢的那一半 —— 20–40 次連續外呼。跟 `/api/search` 分開,是為了讓頁面
    能顯示進度,而不是讓使用者對著一個十幾秒沒有回應的請求乾等。
    """
    request = _to_request(body, conn)
    resolved = credentials.resolve(conn, header_token=x_travelpayouts_token)
    return _guarded(lambda: search.warm(conn, request, token=resolved.token or None))


@app.post("/api/search")
def run_search(
    body: SearchIn,
    conn: sqlite3.Connection = Depends(get_conn),
    x_travelpayouts_token: str | None = TokenHeader,
    x_travelpayouts_marker: str | None = MarkerHeader,
) -> dict[str, Any]:
    """從快取排名並回傳結果。不外呼,所以是即時的。"""
    request = _to_request(body, conn)
    resolved = credentials.resolve(
        conn, header_token=x_travelpayouts_token, header_marker=x_travelpayouts_marker
    )
    return _guarded(
        lambda: search.run(
            conn,
            request,
            token=resolved.token or None,
            marker=resolved.marker,
        )
    )


@app.post("/api/verify")
def verify(body: VerifyIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """即時實價:同一個行程,拼票買法與一張票買法並排。

    這裡是無狀態的 —— 請求帶著完整航段進來,所以不需要先跑一次搜尋、
    也不需要在伺服器上保存搜尋結果。
    """
    legs = tuple(
        FlightLeg(leg.origin.upper(), leg.destination.upper(), leg.date,
                  leg.origin.upper(), leg.destination.upper())
        for leg in body.legs
    )
    combo = Combo(legs=legs, shape_label="", is_baseline=False)
    provider = get_provider(credentials.duffel_token(conn))

    single = provider.price_single_ticket(combo, passengers=body.passengers, cabin=body.cabin)
    split = provider.price_split_tickets(combo, passengers=body.passengers, cabin=body.cabin)

    return {
        "provider": provider.name,
        "single_ticket": {
            "total": single.total,
            "currency": single.currency,
            "carrier": single.carrier,
            # 測試 token 回的是虛構航空的假價。標出來,讓畫面有辦法擋掉 ——
            # 那些數字長得跟真的一樣,混進比價就會用假價算出很像真的結論。
            "test_mode": single.test_mode,
            "offer_count": single.offer_count,
            "unavailable_reason": single.unavailable_reason,
            "fetched_at": single.fetched_at.isoformat(),
        },
        "split_tickets": {
            "total": split.total,
            "currency": split.currency,
            "unavailable_reason": split.unavailable_reason,
            "legs": [
                {
                    "total": quote.total,
                    "currency": quote.currency,
                    "carrier": quote.carrier,
                    "unavailable_reason": quote.unavailable_reason,
                }
                for quote in split.legs
            ],
        },
        "links": {
            "single_ticket": deeplinks.links_for_single_ticket(
                combo, passengers=body.passengers, cabin=body.cabin
            ),
            "split": deeplinks.links_for_split_tickets(
                combo, passengers=body.passengers, cabin=body.cabin
            ),
        },
    }


@app.get("/api/routes/airlines")
def route_airlines(
    pairs: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """這幾條航線上有哪些航空公司在飛。`pairs` 是 `TPE-NRT,ITM-TPE` 這種字串。

    刻意獨立成一支端點,由頁面在結果畫出來之後才呼叫 —— 這是加值資訊,
    不該讓它拖慢查價那條主線。

    ⚠️ 這**不是**「那個價格是誰飛的」。排名用的 month-matrix 沒有航空公司欄位
    (只有 `gate`,那是訂票網站),而有航空公司的 calendar 端點不吃日期。
    所以這裡只能誠實回答航線層級的事實。
    """
    wanted: list[tuple[str, str]] = []
    for chunk in pairs.split(","):
        origin, _, destination = chunk.strip().partition("-")
        if len(origin) == 3 and len(destination) == 3:
            wanted.append((origin.upper(), destination.upper()))
    if not wanted:
        raise HTTPException(status_code=400, detail="pairs 格式應為 TPE-NRT,ITM-TPE")
    if len(wanted) > 40:
        raise HTTPException(status_code=400, detail="一次最多查 40 條航線")

    keys = credentials.resolve(conn)
    found = airlines.for_routes(conn, wanted, token=keys.token or None)
    return {
        "routes": {
            route: [{"code": a.code, "name": a.name} for a in carriers]
            for route, carriers in found.items()
        },
        "note": "這是這條航線上有在飛的航空公司,不是左邊那個價格所屬的航空公司。",
    }


# --------------------------------------------------------------------------
# 倒買法
# --------------------------------------------------------------------------

def _window(
    conn: sqlite3.Connection, spec: TripIn, fallback_label: str
) -> reverse.TripWindow:
    airports = refdata.expand_airports(conn, spec.codes)
    if not airports:
        raise HTTPException(
            status_code=400, detail=f"找不到可飛的機場:{', '.join(spec.codes)}"
        )
    earliest, latest, nights_min, nights_max = spec.dates()
    return reverse.TripWindow(
        label=spec.label or airports[0].city_name or fallback_label,
        airports=tuple(a.code for a in airports),
        depart_earliest=earliest,
        depart_latest=latest,
        nights_min=nights_min,
        nights_max=nights_max,
    )


@app.post("/api/reverse")
def reverse_plans(
    body: ReverseIn, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """倒買法:兩趟旅行,四種買法,四段航程重新配對。

    這裡**不排名價格**,而那不是還沒做完。實測 Travelpayouts 對台灣出發的航線
    一列來回快取都沒有,而倒買法省的就是來回計價 —— 用單程價加總算出來的數字
    保證看不到那個效果,顯示它比不顯示更糟。價格只掛在單程票上;
    整個買法的總價只有「四段全拆單程」給得出來。
    """
    home = refdata.expand_airports(conn, body.home)
    if not home:
        raise HTTPException(status_code=400, detail=f"找不到可飛的機場:{body.home}")

    home_codes = tuple(a.code for a in home)
    try:
        first = _window(conn, body.first, "第一趟")
        second = _window(conn, body.second, "第二趟")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if second.depart_earliest <= first.depart_latest:
        raise HTTPException(
            status_code=400,
            detail=(
                f"「{second.label}」最早的出發日({second.depart_earliest})必須晚於"
                f"「{first.label}」最晚的出發日({first.depart_latest})。"
                "倒買法把兩趟的航段交叉綁在同一張票上,而同一張票必須依序使用。"
            ),
        )
    keys = credentials.resolve(conn)

    # 要抓的航線與月份從**區間**推導,不是從已經選好的日期 —— 排名之前還不知道
    # 會選哪幾天,而抓價本來就是整月一起抓的,所以多涵蓋幾天不多花呼叫數。
    places = [home_codes, first.airports, second.airports]
    pairs = sorted(
        {(o, d) for a, b in ((0, 1), (1, 0), (0, 2), (2, 0))
         for o in places[a] for d in places[b]}
    )
    months = cached.months_covering(
        [w.depart_earliest for w in (first, second)]
        + [w.depart_latest + timedelta(days=w.nights_max) for w in (first, second)]
    )

    warnings: list[str] = []
    if body.warm:
        try:
            outcome = cached.ensure_routes(conn, pairs, months, token=keys.token or None)
            if outcome.unauthorized:
                warnings.append("Travelpayouts 拒絕了這組 token,單程價那一欄會是空的。")
            elif outcome.failures:
                warnings.append(
                    f"以下航線沒抓成功:{', '.join(outcome.failed_routes)}"
                )
        except cached.MissingToken as exc:
            warnings.append(str(exc))

    lookup = cached.load_lookup(conn, pairs, nonstop=body.nonstop)
    fetched = cached.fetched_routes(conn)
    siblings = refdata.siblings_by_airport(
        conn, {code for pair in pairs for code in pair}
    )

    def price(origin: str, destination: str, day: date) -> float | None:
        point = lookup.get((origin, destination, day))
        return point.price if point else None

    try:
        ranked = reverse.rank_combinations(
            home_codes, first, second, price, limit=REVERSE_GROUP_LIMIT
        )
    except SpecTooLarge as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": exc.message, "offender": exc.offender},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not ranked:
        raise HTTPException(
            status_code=400,
            detail="這兩個區間排不出合法的組合(第二趟必須整個在第一趟之後)。",
        )
    if ranked[0].split_total is None:
        warnings.append(
            f"這幾個月({'、'.join(months)})的快取價不夠,湊不出完整總價。"
            "這個資料源大約只有三個月的視野 —— 太遠的日期上游一列都沒有,"
            "把行程往前挪通常就有價格了。"
        )

    return {
        "warnings": warnings,
        "currency": settings.default_currency,
        # 三個出口不一樣,而畫面上它們長得一模一樣。使用者回報過「航空公司沒篩選到」
        # ——那時候只有 Google Flights 帶篩選,另外兩顆按鈕看不出差別。
        "link_info": deeplinks.LINK_INFO,
        "months": months,
        "route_pairs": len(pairs),
        "groups": [
            {
                # 四段全拆單程的總價 —— 這是唯一整個買法都算得出來的數字,
                # 也是判斷反向票值不值得的基準線。缺一段就是 None,絕不部分加總。
                "split_total": combo.split_total,
                "missing": [
                    f"{leg.origin}→{leg.destination} {leg.day.isoformat()}"
                    for leg in combo.missing
                ],
                "plans": [
                    _serialise_plan(plan, lookup, fetched, body, conn, siblings)
                    for plan in combo.plans
                ],
            }
            for combo in ranked
        ],
        "group_count": len(ranked),
    }


REVERSE_GROUP_LIMIT = 12


def _reference_legs(plan, lookup, fetched, conn, siblings, nonstop=False) -> list[dict[str, Any]]:
    """四段各自的**單程**快取價,當作參考基準。

    ⚠️ 這**不是**這幾張票的價格,而且**不能加起來當總價** —— 倒買法省錢的機制
    就是來回計價,單程加總算出來的數字必然看不到那個效果,而且錯的方向剛好會讓
    倒買法看起來更好。這裡逐段列出、不給總和,就是為了讓它只能被當成基準用:
    「市價大概這樣,點過去看那兩張票有沒有比這個便宜」。
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for ticket in plan.tickets:
        for j, leg in enumerate(ticket.legs):
            key = (leg.origin, leg.destination, leg.depart_date.isoformat())
            if key in seen:
                continue
            seen.add(key)
            point = lookup.get((leg.origin, leg.destination, leg.depart_date))
            row: dict[str, Any] = {
                "code": ticket.leg_codes[j] if j < len(ticket.leg_codes) else "",
                "origin": leg.origin,
                "destination": leg.destination,
                "date": leg.depart_date.isoformat(),
                "price": point.price if point else None,
                "age_hours": round(point.age_hours, 1) if point else None,
                # 誰飛的。month-matrix 給不出來,v3 給得出來,合併時誰便宜誰勝出 ——
                # 所以這裡有值代表「這個價確實是這家的」,None 代表「不知道」,
                # 而不是「沒有航空公司」。
                "airline": point.airline if point else None,
            }
            if point is None and conn is not None:
                row.update(
                    gaps.serialise(
                        cached.explain_gap(
                            conn,
                            leg.origin,
                            leg.destination,
                            leg.depart_date,
                            fetched=fetched,
                            sibling_origins=(siblings or {}).get(leg.origin, ()),
                            sibling_destinations=(siblings or {}).get(leg.destination, ()),
                            nonstop=nonstop,
                        )
                    )
                )
            out.append(row)
    return out


def _serialise_plan(
    plan, lookup, fetched, body: ReverseIn, conn=None, siblings=None
) -> dict[str, Any]:
    # 單程票各自標價;plan 級的總價只在整個買法都可計價時給(即四段全拆),
    # 而且沿用「缺一段就不給總價」的規則,絕不做部分加總。
    ticket_pricing = {
        i: cached.price_combo(t.combo, lookup, fetched)
        for i, t in enumerate(plan.tickets)
        if t.priceable
    }

    pricing = None
    if plan.priceable:
        combos = list(ticket_pricing.values())
        complete = all(c.is_complete for c in combos)
        pricing = {
            "total": sum(c.total or 0 for c in combos) if complete else None,
            "source": "cache" if complete else None,
            "missing": [
                f"{leg.origin}→{leg.destination}"
                for c in combos
                for leg in c.missing_legs
            ],
        }

    return {
        "method": plan.method,
        "method_label": plan.method_label,
        "priceable": plan.priceable,
        "unavailable_reason": plan.unavailable_reason,
        "pricing": pricing,
        "risks": reverse.risks(plan),
        # 票是交叉的,但**飛的順序是正常的**。沒有這張對照表,使用者看著兩張
        # 交叉的票會以為自己得照票面順序飛。
        "sequence": [
            {
                "label": trip.label,
                "depart": trip.depart.isoformat(),
                "back": trip.back.isoformat(),
                "codes": list(trip.codes),
            }
            for trip in plan.sequence
        ],
        # 這種買法一個數字都沒有(兩張都按來回計價,台灣航線沒有來回快取),
        # 所以把四段各自的單程價當**參考基準**列出來 —— 明說它不是這兩張票的
        # 價格、也不能加起來當總價。空白的畫面沒有告訴使用者任何事;
        # 一個標錯的總價則會直接騙他。
        "reference_legs": _reference_legs(
            plan, lookup, fetched, conn, siblings, body.nonstop
        )
        if not plan.priceable
        else [],
        "tickets": [
            {
                "label": ticket.label,
                "role": ticket.role,
                "code": ticket.code,
                "note": ticket.note,
                "leg_codes": list(ticket.leg_codes),
                "open_jaw": ticket.is_open_jaw,
                "priceable": ticket.priceable,
                "pricing": (
                    {
                        "total": ticket_pricing[i].total,
                        "missing": [
                            f"{leg.origin}→{leg.destination}"
                            for leg in ticket_pricing[i].missing_legs
                        ],
                        # 「查無資料」單獨擺在那裡是條死路。這條航線通常還是有
                        # 幾天有價的(實測 OKA→TPE 在 2026-12 有 10 天,只是最早
                        # 從 12-16 開始),把那些日子講出來就變成下一步;真的一天
                        # 都沒有的時候,說出「一天都沒有」也比一句「查無資料」有用。
                        "alternatives": [
                            {
                                "leg": f"{leg.origin}→{leg.destination}",
                                **gaps.serialise(
                                    cached.explain_gap(
                                        conn,
                                        leg.origin,
                                        leg.destination,
                                        leg.depart_date,
                                        fetched=fetched,
                                        sibling_origins=(siblings or {}).get(leg.origin, ()),
                                        sibling_destinations=(siblings or {}).get(
                                            leg.destination, ()
                                        ),
                                        nonstop=body.nonstop,
                                    )
                                ),
                            }
                            for leg in ticket_pricing[i].missing_legs
                        ]
                        if conn is not None
                        else [],
                    }
                    if i in ticket_pricing
                    else None
                ),
                "legs": [
                    {
                        "origin": leg.origin,
                        "destination": leg.destination,
                        "date": leg.depart_date.isoformat(),
                        "code": (
                            ticket.leg_codes[j] if j < len(ticket.leg_codes) else ""
                        ),
                    }
                    for j, leg in enumerate(ticket.legs)
                ],
                "links": deeplinks.links_for_single_ticket(
                    ticket.combo,
                    passengers=body.passengers,
                    cabin=body.cabin,
                    airlines=body.airlines,
                    nonstop=body.nonstop,
                ),
            }
            for i, ticket in enumerate(plan.tickets)
        ],
    }


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

@app.get("/api/keys")
def read_keys(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """What key the site will use, and where it came from — never the key itself."""
    resolved = credentials.resolve(conn)
    duffel = credentials.duffel_token(conn)
    # marker 要獨立讀。`resolve()` 只有在 token 存在時才回報 marker,所以
    # 「清掉 token 但留著 marker」的狀態下,畫面會顯示 marker 不見了 ——
    # 然後使用者重填 token 時就會連 marker 一起重打。
    marker = resolved.marker or credentials.stored_marker(conn)
    return {
        "configured": resolved.configured,
        "masked_token": resolved.masked_token,
        "marker": marker,
        "source": resolved.source,
        # Duffel:唯一報得出開口票真價的來源。回遮罩過的字串,不回金鑰本身。
        "duffel_configured": bool(duffel),
        "masked_duffel": credentials.masked(duffel),
    }


@app.put("/api/keys")
def write_keys(body: KeysIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """Save the key on the server.

    It used to live only in the browser, because a key stored server-side on a
    public site is readable by anyone who finds the settings panel. The site is
    behind a password now, so that reason is gone — and "saved on the website"
    meaning "only on this one browser" is indistinguishable from broken.
    """
    credentials.store(conn, token=body.token, marker=body.marker, duffel=body.duffel)
    return read_keys(conn)


@app.delete("/api/keys")
def delete_keys(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    credentials.clear(conn)
    return read_keys(conn)


# --------------------------------------------------------------------------
# Account
# --------------------------------------------------------------------------

@app.post("/api/password")
def set_password(body: PasswordIn) -> dict[str, Any]:
    """變更 HTTP Basic auth 的密碼。

    這個端點本身沒有做認證 —— 它不需要,因為 nginx 已經把整個站擋在密碼後面,
    能打到這裡的人早就通過驗證了。不過還是要求輸入目前的密碼:那是防止有人
    借用一台沒鎖螢幕、瀏覽器還記著帳密的電腦把你鎖在外面。
    """
    if not settings.htpasswd_path:
        raise HTTPException(status_code=404, detail="這個站台沒有啟用密碼保護。")
    try:
        username = change_password(
            Path(settings.htpasswd_path),
            body.current,
            body.new,
            group=settings.htpasswd_gid,
        )
    except PasswordError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "username": username,
        # Basic auth 沒有登出的概念,瀏覽器會繼續送舊帳密直到被拒。
        "note": "密碼已更新。瀏覽器接下來會再問一次帳號密碼,請用新密碼登入。",
    }


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

@app.get("/api/health")
def health(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """Not just "is the process up".

    A flight data source fails far more often by answering 200 with an empty
    body than by erroring, so this reports, per source, when it last returned
    anything at all. A `last_success_at` that keeps advancing while
    `last_nonempty_at` stands still is the shape of a broken integration.
    """
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("countries", "cities", "airports", "price_cache")
    }  # noqa: S608 — table names are a fixed literal tuple, not user input
    sources = source_health(conn)
    keys = credentials.resolve(conn)
    reference_ready = counts["airports"] > 0
    return {
        "status": "ok" if reference_ready else "degraded",
        "reference_ready": reference_ready,
        "row_counts": counts,
        "sources": sources,
        "config": {
            "cached_prices": keys.configured,
            "key_source": keys.source,
            "live_prices": settings.has_live_prices,
            "live_provider": get_provider(credentials.duffel_token(conn)).name,
            "price_month_endpoint": settings.price_month_endpoint,
            "currency": settings.default_currency,
            "can_change_password": settings.can_change_password,
        },
    }


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------
# One page, one form, one table. A second container running a Node build would
# be pure operational cost, so FastAPI serves the page itself. Mounted last so
# it never shadows an /api route.

FRONTEND_DIR = Path(
    os.getenv("AIR_FRONTEND_DIR", Path(__file__).resolve().parents[2] / "frontend")
)

class RevalidatingStatics(StaticFiles):
    """Serve assets with `no-cache` so browsers always revalidate.

    Not "don't cache" — the ETag still short-circuits an unchanged file to a
    304. This exists because the alternative is shipping a CSS change and
    having people see the old page until they clear their cache, which is
    indistinguishable from the change not working.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


if FRONTEND_DIR.is_dir():
    app.mount("/static", RevalidatingStatics(directory=FRONTEND_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(
            FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )
