"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("AIR_DB_PATH", "/data/air.sqlite3"))
    )

    # Layer 1 — Travelpayouts (Aviasales) cached prices.
    # Reference data (countries/cities/airports) needs no token; prices do.
    travelpayouts_token: str = field(
        default_factory=lambda: os.getenv("TRAVELPAYOUTS_TOKEN", "").strip()
    )
    travelpayouts_marker: str = field(
        default_factory=lambda: os.getenv("TRAVELPAYOUTS_MARKER", "").strip()
    )
    # Which month endpoint to trust for per-day one-way prices. The spike
    # (scripts/spike_datasource.py) decides this empirically — see
    # docs/spike-datasource.md — because the published docs do not say whether
    # a call without return_date is genuinely one-way priced.
    price_month_endpoint: str = field(
        default_factory=lambda: os.getenv("AIR_PRICE_MONTH_ENDPOINT", "month-matrix")
    )
    price_cache_ttl_hours: int = field(
        default_factory=lambda: int(os.getenv("AIR_PRICE_CACHE_TTL_HOURS", "6"))
    )

    # Layer 2 — live verification. Duffel only activates with a live token;
    # without one the deep-link provider carries the whole layer, which is the
    # supported configuration, not a degraded one.
    duffel_token: str = field(default_factory=lambda: os.getenv("DUFFEL_TOKEN", "").strip())
    duffel_api_version: str = field(
        default_factory=lambda: os.getenv("DUFFEL_API_VERSION", "v2")
    )

    # HTTP Basic auth 的密碼檔,由 nginx 驗證、由本站的變更密碼端點改寫。
    # 空字串 = 沒有掛進來,變更密碼功能就不會出現。
    htpasswd_path: str = field(default_factory=lambda: os.getenv("AIR_HTPASSWD_PATH", ""))
    # nginx 以這個 gid 讀密碼檔。改寫後群組掉了就是所有人都登不進去,
    # 而容器裡的 www-data gid 不見得跟主機一樣,所以明講。
    htpasswd_gid: int | None = field(
        default_factory=lambda: int(os.environ["AIR_HTPASSWD_GID"])
        if os.getenv("AIR_HTPASSWD_GID")
        else None
    )

    default_currency: str = field(default_factory=lambda: os.getenv("AIR_CURRENCY", "TWD"))
    request_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("AIR_HTTP_TIMEOUT", "20"))
    )
    debug: bool = field(default_factory=lambda: _env_bool("AIR_DEBUG"))

    @property
    def has_cached_prices(self) -> bool:
        return bool(self.travelpayouts_token)

    @property
    def has_live_prices(self) -> bool:
        return bool(self.duffel_token)

    @property
    def can_change_password(self) -> bool:
        from pathlib import Path as _Path

        return bool(self.htpasswd_path) and _Path(self.htpasswd_path).exists()


settings = Settings()
