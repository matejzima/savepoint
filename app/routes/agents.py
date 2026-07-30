from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import agent_client
from .. import db as db_module
from .. import scheduler
from ..deps import get_db_conn, templates

router = APIRouter()


def _context(conn, error=None):
    return {"agents": db_module.list_agents(conn), "error": error}


@router.get("/agents", response_class=HTMLResponse)
def list_agents_route(request: Request, conn=Depends(get_db_conn)):
    return templates.TemplateResponse(request, "agents.html", _context(conn))


@router.post("/agents", response_class=HTMLResponse)
def create_agent_route(
    request: Request,
    name: str = Form(...),
    base_url: str = Form(...),
    token: str = Form(...),
    offsite: bool = Form(False),
    conn=Depends(get_db_conn),
):
    db_module.create_agent(conn, name, base_url, token, offsite=offsite)
    return RedirectResponse(url="/agents", status_code=303)


@router.post("/agents/{agent_id}/edit", response_class=HTMLResponse)
def edit_agent_route(
    request: Request,
    agent_id: int,
    name: str = Form(...),
    base_url: str = Form(...),
    token: str = Form(...),
    offsite: bool = Form(False),
    conn=Depends(get_db_conn),
):
    agent = db_module.get_agent(conn, agent_id)
    if agent is None:
        return HTMLResponse("agent not found", status_code=404)

    db_module.update_agent(conn, agent_id, name, base_url, token, offsite)

    # offsite gates automatic scheduling (window_tick() already re-queries this live on
    # every fire, but sync_target_schedule() only runs when a target's own schedule is
    # saved or at app startup), re-sync every target on this agent now so a flip takes
    # effect immediately rather than on the next unrelated schedule save or restart.
    for target in db_module.list_targets_for_agent(conn, agent_id):
        scheduler.sync_target_schedule(target)

    return RedirectResponse(url="/agents", status_code=303)


@router.post("/agents/{agent_id}/delete", response_class=HTMLResponse)
def delete_agent_route(request: Request, agent_id: int, conn=Depends(get_db_conn)):
    agent = db_module.get_agent(conn, agent_id)
    if agent is None:
        return HTMLResponse("agent not found", status_code=404)

    target_count = db_module.count_targets_for_agent(conn, agent_id)
    if target_count:
        return templates.TemplateResponse(
            request,
            "agents.html",
            _context(
                conn,
                error=(
                    f"cannot delete '{agent['name']}', {target_count} target(s) still reference it, "
                    "delete or reassign those targets first"
                ),
            ),
            status_code=400,
        )

    db_module.delete_agent(conn, agent_id)
    return RedirectResponse(url="/agents", status_code=303)


@router.post("/agents/{agent_id}/health-check", response_class=HTMLResponse)
def health_check_agent_route(request: Request, agent_id: int, conn=Depends(get_db_conn)):
    agent = db_module.get_agent(conn, agent_id)
    if agent is None:
        return HTMLResponse("agent not found", status_code=404)

    ok = agent_client.health(agent, request.app.state.settings)
    error = None if ok else f"could not reach '{agent['name']}'"
    return templates.TemplateResponse(request, "agents.html", _context(conn, error=error))
