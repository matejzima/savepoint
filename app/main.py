import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db, retention, scheduler
from .config import load_settings
from .forward_auth import ForwardAuthMiddleware
from .routes import agent_api, agents, discover, history, targets
from .routes import settings as settings_routes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("savepoint")

settings = load_settings()

if settings.mode == "agent":
    if not settings.agent_token:
        logger.error("MODE=agent requires AGENT_TOKEN to be set, exiting")
        sys.exit(1)
    # A hard crash (SIGKILL) mid-transfer gives no chance for the normal cleanup code
    # to run, sweep whatever staging directories are left over from that before serving
    # any requests, so repeated crashes don't accumulate them indefinitely.
    agent_api.sweep_stale_staging_dirs()
elif settings.mode == "master":
    db.init_db(settings.state_db_path)
    retention.reconcile_all(settings)
else:
    logger.error("Unknown MODE=%s, expected 'master' or 'agent'", settings.mode)
    sys.exit(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.mode == "master":
        scheduler.start(settings)
    yield
    if settings.mode == "master":
        scheduler.shutdown()


app = FastAPI(title="Savepoint", lifespan=lifespan)
app.state.settings = settings

if settings.mode == "master" and settings.forward_auth_header:
    # Forward-auth protects the human-facing UI, agent mode has no UI and already has
    # its own separate AGENT_TOKEN bearer-auth for its API, unrelated concern, never
    # mounted there.
    app.add_middleware(ForwardAuthMiddleware, header_name=settings.forward_auth_header)

if settings.mode == "master":
    # Agent mode is a headless remote executor: no web UI, no state db of its own,
    # master's state db is the single source of truth for schedules/history/retention.
    app.include_router(targets.router)
    app.include_router(discover.router)
    app.include_router(history.router)
    app.include_router(settings_routes.router)
    app.include_router(agents.router)
else:
    app.include_router(agent_api.router)


def main():
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
