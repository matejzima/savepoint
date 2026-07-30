from types import SimpleNamespace
from unittest.mock import patch

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as db_module
from app import scheduler
from app.deps import get_db_conn
from app.routes import agents as agents_routes


def _make_client(state_db_path):
    app = FastAPI()
    app.include_router(agents_routes.router)
    app.state.settings = SimpleNamespace(state_db_path=state_db_path)

    def override_get_db_conn():
        conn = db_module.get_connection(state_db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db_conn] = override_get_db_conn
    return TestClient(app)


def test_create_agent_persists_row(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)

    r = client.post(
        "/agents",
        data={"name": "remote-host", "base_url": "http://remote-host:8000", "token": "tok"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    conn = db_module.get_connection(state_db)
    agents = db_module.list_agents(conn)
    assert len(agents) == 1
    assert agents[0]["name"] == "remote-host"
    assert agents[0]["offsite"] == 0


def test_create_agent_offsite_checkbox_persists(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)

    client.post(
        "/agents",
        data={"name": "cottage", "base_url": "http://cottage:8000", "token": "tok", "offsite": "true"},
    )

    conn = db_module.get_connection(state_db)
    assert db_module.list_agents(conn)[0]["offsite"] == 1


def test_edit_agent_updates_fields_and_targets_keep_working(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://old:8000", "old-token")
    target_id = db_module.create_target(conn, "remote-db", "postgres", "c", "u", "d", agent_id=agent_id)

    client = _make_client(state_db)
    with patch("app.routes.agents.scheduler.sync_target_schedule"):
        r = client.post(
            f"/agents/{agent_id}/edit",
            data={"name": "remote-host", "base_url": "http://new:8000", "token": "new-token", "offsite": "true"},
            follow_redirects=False,
        )
    assert r.status_code == 303

    conn = db_module.get_connection(state_db)
    agent = db_module.get_agent(conn, agent_id)
    assert agent["base_url"] == "http://new:8000"
    assert agent["token"] == "new-token"
    assert agent["offsite"] == 1

    # the target that references this agent is untouched, no need to re-add it
    target = db_module.get_target(conn, target_id)
    assert target["agent_id"] == agent_id
    assert target["name"] == "remote-db"


def test_edit_agent_offsite_flip_immediately_unregisters_and_reregisters_cron_jobs(tmp_path):
    """window_tick() re-queries agent_offsite fresh on every fire, but
    sync_target_schedule() only ran when a target's own schedule was saved or at app
    startup, so flipping an agent's offsite flag alone left an already-registered cron
    job in place until something unrelated touched that target again. edit_agent_route()
    must re-sync every target on the agent right away.
    """
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok", offsite=False)
    target_id = db_module.create_target(conn, "remote-db", "postgres", "c", "u", "d", agent_id=agent_id)
    db_module.update_target_schedule(conn, target_id, "0 3 * * *", in_window=False)

    sched = BackgroundScheduler()
    sched.start()
    scheduler._scheduler = sched
    try:
        # registered up front, matching what scheduler.start() would have done at boot
        scheduler.sync_target_schedule(db_module.get_target(conn, target_id))
        assert sched.get_job(f"target-{target_id}") is not None

        client = _make_client(state_db)
        r = client.post(
            f"/agents/{agent_id}/edit",
            data={"name": "remote-host", "base_url": "http://remote-host:8000", "token": "tok", "offsite": "true"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert sched.get_job(f"target-{target_id}") is None

        r = client.post(
            f"/agents/{agent_id}/edit",
            data={"name": "remote-host", "base_url": "http://remote-host:8000", "token": "tok"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert sched.get_job(f"target-{target_id}") is not None
    finally:
        sched.shutdown(wait=False)


def test_delete_agent_blocked_while_targets_reference_it(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    db_module.create_target(conn, "remote-db", "postgres", "c", "u", "d", agent_id=agent_id)

    client = _make_client(state_db)
    r = client.post(f"/agents/{agent_id}/delete")

    assert r.status_code == 400
    assert "1 target" in r.text

    conn = db_module.get_connection(state_db)
    assert db_module.get_agent(conn, agent_id) is not None


def test_delete_agent_succeeds_when_unreferenced(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")

    client = _make_client(state_db)
    r = client.post(f"/agents/{agent_id}/delete", follow_redirects=False)

    assert r.status_code == 303
    conn = db_module.get_connection(state_db)
    assert db_module.get_agent(conn, agent_id) is None


def test_health_check_route_shows_error_on_failure(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")

    client = _make_client(state_db)
    with patch("app.routes.agents.agent_client.health", return_value=False):
        r = client.post(f"/agents/{agent_id}/health-check")

    assert r.status_code == 200
    assert "could not reach" in r.text


def test_health_check_route_shows_no_error_on_success(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")

    client = _make_client(state_db)
    with patch("app.routes.agents.agent_client.health", return_value=True):
        r = client.post(f"/agents/{agent_id}/health-check")

    assert r.status_code == 200
    assert "could not reach" not in r.text
