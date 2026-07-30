from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as db_module
from app import docker_client
from app import jobs
from app.deps import get_db_conn
from app.routes import history as history_routes
from app.routes import targets as targets_routes


def setup_function(_):
    jobs._in_progress.clear()


def _make_client(state_db_path, backup_target_dir="/tmp"):
    app = FastAPI()
    app.include_router(targets_routes.router)
    app.include_router(history_routes.router)
    app.state.settings = SimpleNamespace(backup_target_dir=backup_target_dir, state_db_path=state_db_path)

    def override_get_db_conn():
        conn = db_module.get_connection(state_db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db_conn] = override_get_db_conn
    return TestClient(app)


def _create_target(client, name="t1"):
    with patch.object(docker_client, "get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.containers.get.return_value = MagicMock()
        client.post(
            "/targets",
            data={"name": name, "container_name": f"{name}-c", "db_user": "u", "db_name": "d"},
        )


# --- Dashboard summary strip ---


def test_dashboard_stays_quiet_when_healthy(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)

    r = client.get("/")

    assert r.status_code == 200
    assert "Currently failing" not in r.text
    assert "unreachable" not in r.text


def test_dashboard_shows_failing_targets_with_link(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client, "flaky")
    conn = db_module.get_connection(state_db)
    run_id = db_module.create_backup_run(conn, 1, status="running")
    db_module.finish_backup_run(conn, run_id, "failure", error_message="boom")

    r = client.get("/")

    assert "Currently failing" in r.text
    assert 'href="/targets/1"' in r.text
    assert "flaky" in r.text


def test_dashboard_shows_unreachable_agent_count(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    conn = db_module.get_connection(state_db)
    db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    db_module.update_agent_contact(conn, 1, "error", "connection refused")

    r = client.get("/")

    assert "1 agent" in r.text
    assert "unreachable" in r.text
    assert "remote-host" in r.text


def test_dashboard_shows_next_window_time(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)

    r = client.get("/")

    assert "next backup window" in r.text


# --- Forward-auth topbar identity display ---


def test_topbar_identity_value_is_auto_escaped(tmp_path):
    """The header's value must render as literal escaped text (Jinja's default
    auto-escaping), never executed or unescaped markup, confirming nothing renders it
    with |safe.
    """
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    app = FastAPI()
    app.include_router(targets_routes.router)
    app.state.settings = SimpleNamespace(
        backup_target_dir="/tmp",
        state_db_path=state_db,
        mode="master",
        forward_auth_header="X-Authentik-Username",
    )

    def override_get_db_conn():
        conn = db_module.get_connection(state_db)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db_conn] = override_get_db_conn
    client = TestClient(app)

    r = client.get("/", headers={"X-Authentik-Username": "<script>alert(1)</script>"})

    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


# --- History row failure/skipped tint ---


def test_history_row_gets_failure_class_for_failure_status(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)
    conn = db_module.get_connection(state_db)
    run_id = db_module.create_backup_run(conn, 1, status="running")
    db_module.finish_backup_run(conn, run_id, "failure", error_message="boom")

    r = client.get("/targets/1")

    assert 'class="row-failure"' in r.text


def test_history_row_does_not_get_failure_class_for_skipped_status(tmp_path):
    """Skipped means the target didn't get a turn during a window, a scheduling
    outcome, not that something went wrong, must stay visually distinct from a failure.
    """
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)
    conn = db_module.get_connection(state_db)
    run_id = db_module.create_backup_run(conn, 1, status="running")
    db_module.finish_backup_run(conn, run_id, "skipped", error_message="window closed before this could run")

    r = client.get("/targets/1")

    assert 'class="row-failure"' not in r.text


def test_history_row_no_failure_class_for_success_status(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)
    conn = db_module.get_connection(state_db)
    run_id = db_module.create_backup_run(conn, 1, status="running")
    db_module.finish_backup_run(conn, run_id, "success", file_path="/tmp/x.dump", file_size_bytes=1)

    r = client.get("/targets/1")

    assert 'class="row-failure"' not in r.text


# --- Detail page next-run display ---


def test_detail_page_shows_next_run_for_active_cron_schedule(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)
    with patch("app.routes.targets.scheduler.sync_target_schedule"):
        client.post("/targets/1/schedule", data={"mode": "cron", "schedule_cron": "0 3 * * *"})

    r = client.get("/targets/1")

    assert "next run:" in r.text
    assert "not active" not in r.text


def test_detail_page_shows_inactive_reason_when_disabled(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)
    with patch("app.routes.targets.scheduler.sync_target_schedule"):
        client.post("/targets/1/schedule", data={"mode": "cron", "schedule_cron": "0 3 * * *"})
        client.post("/targets/1/toggle-enabled")

    r = client.get("/targets/1")

    assert "not active: target disabled" in r.text
    assert "next run:" not in r.text


def test_detail_page_shows_inactive_reason_when_agent_offsite(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "cottage", "http://cottage:8000", "tok", offsite=True)
    target_id = db_module.create_target(conn, "remote-db", "postgres", "c", "u", "d", agent_id=agent_id)
    db_module.update_target_schedule(conn, target_id, "0 3 * * *", in_window=False)

    r = client.get(f"/targets/{target_id}")

    assert "not active: agent flagged offsite" in r.text
    assert "next run:" not in r.text


def test_detail_page_shows_next_run_for_window_member(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)
    with patch("app.routes.targets.scheduler.sync_target_schedule"):
        client.post("/targets/1/schedule", data={"mode": "window", "schedule_cron": ""})

    r = client.get("/targets/1")

    assert "shared window member" in r.text
    assert "next run:" in r.text


def test_detail_page_manual_only_shows_no_next_run(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)

    r = client.get("/targets/1")

    assert "manual only, not scheduled" in r.text
    assert "next run:" not in r.text
