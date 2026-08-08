"""HTTP layer. Thin on purpose — the thinking lives in app.search."""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Literal

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import refdata, search
from app.combos import SpecTooLarge
from app.config import settings
from app.db import closing_conn, connect, init_db, source_health
from app.password import PasswordError, change_password
from app.pricing import deeplinks
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


class LegIn(BaseModel):
    origin: str
    destination: str
    date: date


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


# The site is public and has no login, so a key kept server-side would be
# readable by anyone who found the settings page. The browser holds it instead
# and sends it per request; the server uses it and forgets it. A key in .env
# still works as a fallback for everyone.
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
    return _guarded(lambda: search.warm(conn, request, token=x_travelpayouts_token))


@app.post("/api/search")
def run_search(
    body: SearchIn,
    conn: sqlite3.Connection = Depends(get_conn),
    x_travelpayouts_token: str | None = TokenHeader,
    x_travelpayouts_marker: str | None = MarkerHeader,
) -> dict[str, Any]:
    """從快取排名並回傳結果。不外呼,所以是即時的。"""
    request = _to_request(body, conn)
    return _guarded(
        lambda: search.run(
            conn,
            request,
            token=x_travelpayouts_token,
            marker=(x_travelpayouts_marker or settings.travelpayouts_marker or ""),
        )
    )


@app.post("/api/verify")
def verify(body: VerifyIn) -> dict[str, Any]:
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
    provider = get_provider()

    single = provider.price_single_ticket(combo, passengers=body.passengers, cabin=body.cabin)
    split = provider.price_split_tickets(combo, passengers=body.passengers, cabin=body.cabin)

    return {
        "provider": provider.name,
        "single_ticket": {
            "total": single.total,
            "currency": single.currency,
            "carrier": single.carrier,
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
    reference_ready = counts["airports"] > 0
    return {
        "status": "ok" if reference_ready else "degraded",
        "reference_ready": reference_ready,
        "row_counts": counts,
        "sources": sources,
        "config": {
            "cached_prices": settings.has_cached_prices,
            "live_prices": settings.has_live_prices,
            "live_provider": get_provider().name,
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
