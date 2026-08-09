"""查價金鑰的存放處。

原本這組金鑰只存在瀏覽器的 localStorage,因為站台是公開的 —— 存在伺服器上的
金鑰,任何找到設定面板的人都讀得走。站台改用 HTTP Basic auth 鎖起來之後,那個
理由就不存在了,而「在網頁上按了儲存、卻只有那一台瀏覽器算數」對使用者而言
跟壞掉沒有差別:換手機要重填、伺服器端的工具(例如 spike 腳本)也拿不到。

所以現在存在資料庫裡。取用順序是 **請求標頭 → 資料庫 → 環境變數**:
標頭讓一次性的覆寫仍然可行,環境變數留給部署時就決定好的情境。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.config import settings
from app.db import utcnow

TOKEN_KEY = "travelpayouts_token"
MARKER_KEY = "travelpayouts_marker"
# Duffel 是唯一能報**開口來回票**真價、又帶航空公司、又不受快取三個月視野限制的
# 來源(實測:長榮 BR 與華航 CI 都在它的支援清單上,星宇 JX 不在)。
# 跟 Travelpayouts 的 token 一樣存在站台上,理由也一樣 —— 只存瀏覽器等於換裝置就沒了。
DUFFEL_KEY = "duffel_token"


@dataclass(frozen=True)
class Credentials:
    token: str = ""
    marker: str = ""
    source: str = "none"

    @property
    def configured(self) -> bool:
        return bool(self.token)

    @property
    def masked_token(self) -> str:
        """Enough to recognise which key is in place, not enough to use it."""
        if not self.token:
            return ""
        return "••••" if len(self.token) <= 8 else f"{self.token[:4]}…{self.token[-4:]}"


def _read(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def store(
    conn: sqlite3.Connection,
    *,
    token: str | None = None,
    marker: str | None = None,
    duffel: str | None = None,
) -> None:
    """存金鑰。**`None` = 這次不動它,`""` = 清掉。**

    這個區別是付出代價才加上的:原本三個欄位都是 `str`,空字串一律刪除,
    所以「只存 Duffel token」的請求會把 Travelpayouts 的 token 一起洗掉 ——
    而那把金鑰是使用者親手貼上來的,刪掉就沒了(SQLite 那一頁隨即被歸零,
    救不回來)。表單只送它要改的那一格,所以沒送到的欄位必須是「不要動」,
    不能是「清空」。
    """
    now = utcnow().isoformat()
    pairs = [
        (key, value.strip())
        for key, value in ((TOKEN_KEY, token), (MARKER_KEY, marker), (DUFFEL_KEY, duffel))
        if value is not None
    ]
    for key, value in pairs:
        if value:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
        else:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    conn.commit()


def clear(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM app_settings WHERE key IN (?, ?, ?)",
        (TOKEN_KEY, MARKER_KEY, DUFFEL_KEY),
    )
    conn.commit()


def stored_marker(conn: sqlite3.Connection) -> str:
    return _read(conn, MARKER_KEY) or settings.travelpayouts_marker


def duffel_token(conn: sqlite3.Connection) -> str:
    """存在站台的 Duffel token,沒有就退回環境變數。"""
    return _read(conn, DUFFEL_KEY) or settings.duffel_token


def masked(value: str) -> str:
    if not value:
        return ""
    return "••••" if len(value) <= 8 else f"{value[:4]}…{value[-4:]}"


def resolve(
    conn: sqlite3.Connection,
    *,
    header_token: str | None = None,
    header_marker: str | None = None,
) -> Credentials:
    """Whichever key applies to this request, and where it came from.

    The source is reported rather than inferred, so the page can say
    「這台瀏覽器的」 vs 「站台設定的」 instead of leaving the user guessing
    which key a search actually used.
    """
    if header_token and header_token.strip():
        return Credentials(
            token=header_token.strip(),
            marker=(header_marker or "").strip(),
            source="request",
        )

    stored_token = _read(conn, TOKEN_KEY)
    if stored_token:
        return Credentials(
            token=stored_token, marker=_read(conn, MARKER_KEY), source="saved"
        )

    if settings.travelpayouts_token:
        return Credentials(
            token=settings.travelpayouts_token,
            marker=settings.travelpayouts_marker,
            source="env",
        )

    return Credentials(marker=(header_marker or "").strip() or settings.travelpayouts_marker)
