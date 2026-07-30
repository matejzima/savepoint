from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import agent_client
from .. import db as db_module
from .. import discovery
from .. import docker_client
from ..deps import get_db_conn, templates

router = APIRouter()


@router.get("/discover", response_class=HTMLResponse)
def discover(request: Request, agent_id: Optional[str] = None, conn=Depends(get_db_conn)):
    # The "Local" option in the agent selector submits agent_id="" (empty string), not
    # an absent param, so this has to accept a raw string and convert manually, an
    # Optional[int] param fails FastAPI's validation on an empty string before the route
    # body ever runs.
    agent_id = int(agent_id) if agent_id else None
    agents = db_module.list_agents(conn)
    error = None

    if agent_id:
        agent = db_module.get_agent(conn, agent_id)
        if agent is None:
            return HTMLResponse("agent not found", status_code=404)
        existing_names = {t["container_name"] for t in db_module.list_targets(conn) if t["agent_id"] == agent_id}
        try:
            raw_candidates = agent_client.discover(agent, request.app.state.settings)
        except agent_client.AgentError as exc:
            raw_candidates = []
            error = str(exc)
    else:
        agent = None
        client = docker_client.get_client()
        existing_names = {t["container_name"] for t in db_module.list_targets(conn) if not t["agent_id"]}
        raw_candidates = discovery.find_candidates(client)

    candidates = [c for c in raw_candidates if c["container_name"] not in existing_names]

    return templates.TemplateResponse(
        request,
        "discover.html",
        {"candidates": candidates, "agents": agents, "selected_agent_id": agent_id, "error": error},
    )
