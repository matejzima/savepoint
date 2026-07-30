from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import db as db_module
from ..deps import get_db_conn, templates
from .targets import _detail_context

router = APIRouter()


def _render_history_partial(request: Request, target, runs, conn):
    tags_by_run = db_module.get_tags_for_runs(conn, [r["id"] for r in runs])
    return templates.TemplateResponse(
        request,
        "partials/history_row.html",
        {"target": target, "runs": runs, "notice": None, "tags_by_run": tags_by_run},
    )


@router.get("/targets/{target_id}", response_class=HTMLResponse)
def target_detail(request: Request, target_id: int, conn=Depends(get_db_conn)):
    target = db_module.get_target(conn, target_id)
    if target is None:
        return HTMLResponse("target not found", status_code=404)

    return templates.TemplateResponse(
        request, "targets/detail.html", _detail_context(conn, target, target_id)
    )


@router.get("/targets/{target_id}/history", response_class=HTMLResponse)
def target_history(request: Request, target_id: int, conn=Depends(get_db_conn)):
    target = db_module.get_target(conn, target_id)
    if target is None:
        return HTMLResponse("target not found", status_code=404)

    runs = db_module.list_backup_runs(conn, target_id)
    return _render_history_partial(request, target, runs, conn)


@router.get("/targets/{target_id}/restore-history", response_class=HTMLResponse)
def target_restore_history(request: Request, target_id: int, conn=Depends(get_db_conn)):
    target = db_module.get_target(conn, target_id)
    if target is None:
        return HTMLResponse("target not found", status_code=404)

    restore_runs = db_module.list_restore_runs(conn, target_id)
    return templates.TemplateResponse(
        request,
        "partials/restore_history.html",
        {"target": target, "restore_runs": restore_runs, "notice": None},
    )
