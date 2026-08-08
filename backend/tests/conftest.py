"""Point the whole test run at a throwaway database.

`app.config.settings` is frozen at import time, so the environment has to be
set before anything under `app.` is imported — hence doing it here at module
scope rather than in a fixture.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="air-test-")) / "air.sqlite3"
os.environ.setdefault("AIR_DB_PATH", str(_TMP_DB))
os.environ.setdefault("AIR_CURRENCY", "TWD")
