from __future__ import annotations

import os
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import db as db_module

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _local_time(value) -> str | None:
    """Converts a stored UTC ISO timestamp (or a real datetime, e.g. from
    scheduler.next_fire_time()) to local wall-clock time for display.

    Storage stays UTC (db.py::now_iso() is untouched, ordering/comparisons elsewhere
    depend on it), only this filter's output changes. Relies on the container's tzdata
    already being installed (Phase 3's Dockerfile fix), zoneinfo reads it directly, no
    new dependency.
    """
    if not value:
        return value
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    tz = ZoneInfo(os.environ.get("TZ", "UTC"))
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


templates.env.filters["local_time"] = _local_time


def get_db_conn(request: Request):
    conn = db_module.get_connection(request.app.state.settings.state_db_path)
    try:
        yield conn
    finally:
        conn.close()
