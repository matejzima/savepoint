from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db as db_module
from .. import scheduler
from ..deps import get_db_conn, templates

router = APIRouter()


def _context(conn, error=None):
    members = [t for t in db_module.list_all_targets(conn) if t["in_window"]]
    return {
        "error": error,
        "window_start": db_module.get_setting(conn, "window_start", scheduler.DEFAULT_WINDOW_START),
        "window_duration_minutes": db_module.get_setting(
            conn, "window_duration_minutes", scheduler.DEFAULT_WINDOW_DURATION_MINUTES
        ),
        "window_concurrency": db_module.get_setting(
            conn, "window_concurrency", scheduler.DEFAULT_WINDOW_CONCURRENCY
        ),
        "members": members,
    }


@router.get("/settings", response_class=HTMLResponse)
def view_settings(request: Request, conn=Depends(get_db_conn)):
    return templates.TemplateResponse(request, "settings.html", _context(conn))


@router.post("/settings", response_class=HTMLResponse)
def save_settings(
    request: Request,
    window_start: str = Form(...),
    window_duration_minutes: str = Form(...),
    window_concurrency: str = Form(...),
    conn=Depends(get_db_conn),
):
    if scheduler.parse_hhmm(window_start) is None:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _context(conn, error=f"invalid time '{window_start}', expected HH:MM"),
            status_code=400,
        )

    if not window_duration_minutes.isdigit() or int(window_duration_minutes) <= 0:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _context(conn, error="duration must be a positive number of minutes"),
            status_code=400,
        )

    if not window_concurrency.isdigit() or int(window_concurrency) <= 0:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _context(conn, error="concurrency must be a positive number"),
            status_code=400,
        )

    db_module.set_setting(conn, "window_start", window_start)
    db_module.set_setting(conn, "window_duration_minutes", window_duration_minutes)
    db_module.set_setting(conn, "window_concurrency", window_concurrency)
    scheduler.reload_window_job()
    return RedirectResponse(url="/settings", status_code=303)
