import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from docker.errors import NotFound
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as db_module
from app import docker_client
from app import jobs
from app import retention
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


def test_create_target_with_agent_id_validates_via_agent_client_not_local_docker(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    client = _make_client(state_db)

    with patch("app.routes.targets.agent_client.validate", return_value=None) as mock_validate, patch.object(
        docker_client, "get_client"
    ) as mock_get_client:
        r = client.post(
            "/targets",
            data={
                "name": "remote-db",
                "engine": "postgres",
                "container_name": "remote-c",
                "db_user": "u",
                "db_name": "d",
                "agent_id": str(agent_id),
            },
            follow_redirects=False,
        )
        mock_validate.assert_called_once()
        mock_get_client.assert_not_called()  # never touches the local Docker socket

    assert r.status_code == 303
    conn = db_module.get_connection(state_db)
    target = db_module.get_target(conn, 1)
    assert target["agent_id"] == agent_id
    assert target["agent_name"] == "remote-host"


def test_create_target_with_agent_id_rejects_when_agent_validation_fails(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    client = _make_client(state_db)

    with patch("app.routes.targets.agent_client.validate", return_value="container 'remote-c' not found"):
        r = client.post(
            "/targets",
            data={
                "name": "remote-db",
                "engine": "postgres",
                "container_name": "remote-c",
                "db_user": "u",
                "db_name": "d",
                "agent_id": str(agent_id),
            },
        )

    assert r.status_code == 400
    assert "not found" in r.text
    conn = db_module.get_connection(state_db)
    assert db_module.list_all_targets(conn) == []


def test_edit_connection_preserves_agent_id_and_validates_remotely(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    target_id = db_module.create_target(conn, "remote-db", "postgres", "old-c", "u", "d", agent_id=agent_id)
    client = _make_client(state_db)

    with patch("app.routes.targets.agent_client.validate", return_value=None) as mock_validate:
        r = client.post(
            f"/targets/{target_id}/edit",
            data={"container_name": "new-c", "db_user": "u2", "db_name": "d2"},
            follow_redirects=False,
        )
        mock_validate.assert_called_once()

    assert r.status_code == 303
    conn = db_module.get_connection(state_db)
    target = db_module.get_target(conn, target_id)
    assert target["container_name"] == "new-c"
    assert target["agent_id"] == agent_id  # unchanged, agent assignment is fixed at creation


def test_schedule_mode_window_with_nonempty_cron_saves_as_cron_not_window(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)

    with patch("app.routes.targets.scheduler.sync_target_schedule"):
        r = client.post(
            "/targets/1/schedule",
            data={"mode": "window", "schedule_cron": "0 3 * * *"},
            follow_redirects=False,
        )

    assert r.status_code == 303

    conn = db_module.get_connection(state_db)
    target = db_module.get_target(conn, 1)
    assert target["schedule_cron"] == "0 3 * * *"
    assert target["in_window"] == 0


def test_schedule_mode_window_with_blank_cron_still_saves_window(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)

    with patch("app.routes.targets.scheduler.sync_target_schedule"):
        r = client.post(
            "/targets/1/schedule",
            data={"mode": "window", "schedule_cron": ""},
            follow_redirects=False,
        )

    assert r.status_code == 303

    conn = db_module.get_connection(state_db)
    target = db_module.get_target(conn, 1)
    assert target["schedule_cron"] is None
    assert target["in_window"] == 1


def test_schedule_mode_cron_with_invalid_expression_rejected(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)

    r = client.post("/targets/1/schedule", data={"mode": "cron", "schedule_cron": "not a cron"})

    assert r.status_code == 400
    assert "invalid cron" in r.text


def test_retention_save_sets_confirmed_and_persists_counts(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)

    conn = db_module.get_connection(state_db)
    target = db_module.get_target(conn, 1)
    assert target["retention_confirmed"] == 0

    r = client.post(
        "/targets/1/retention",
        data={"retention_daily": "10", "retention_weekly": "5", "retention_monthly": "3"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    target = db_module.get_target(conn, 1)
    assert target["retention_daily"] == 10
    assert target["retention_weekly"] == 5
    assert target["retention_monthly"] == 3
    assert target["retention_confirmed"] == 1


def test_retention_save_with_defaults_also_confirms(tmp_path):
    """Re-submitting the unchanged defaults still flips retention_confirmed, matching the
    plan's "there is no other path to true" rule.
    """
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)

    conn = db_module.get_connection(state_db)
    target = db_module.get_target(conn, 1)
    r = client.post(
        "/targets/1/retention",
        data={
            "retention_daily": str(target["retention_daily"]),
            "retention_weekly": str(target["retention_weekly"]),
            "retention_monthly": str(target["retention_monthly"]),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    target = db_module.get_target(conn, 1)
    assert target["retention_confirmed"] == 1


def test_retention_rejects_non_positive_count(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)

    r = client.post(
        "/targets/1/retention",
        data={"retention_daily": "0", "retention_weekly": "4", "retention_monthly": "2"},
    )
    assert r.status_code == 400
    assert "positive integer" in r.text

    conn = db_module.get_connection(state_db)
    target = db_module.get_target(conn, 1)
    assert target["retention_confirmed"] == 0


def test_retention_save_prunes_existing_backfilled_history_immediately(tmp_path):
    """Saving retention must enforce the counts against whatever history already exists
    right away, not wait for the next backup or a restart to catch up.
    """
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db, backup_target_dir=str(tmp_path))
    _create_target(client)

    conn = db_module.get_connection(state_db)
    paths = []
    for i, day in enumerate(("2026-01-01", "2026-01-02", "2026-01-03")):
        path = str(tmp_path / f"run{i}.dump")
        with open(path, "wb") as f:
            f.write(b"dump")
        run_id = db_module.create_backup_run(conn, 1, status="running", triggered_by="manual")
        conn.execute(
            "UPDATE backup_runs SET started_at = ? WHERE id = ?", (f"{day}T02:00:00+00:00", run_id)
        )
        conn.commit()
        db_module.finish_backup_run(conn, run_id, "success", file_path=path, file_size_bytes=4)
        paths.append(path)

    # simulate the startup reconciliation having already backfilled tags for this
    # not-yet-confirmed target
    for run_id in (1, 2, 3):
        run = db_module.get_backup_run(conn, run_id)
        for tier in retention.compute_tags_for_run(conn, 1, run):
            db_module.add_backup_run_tag(conn, run_id, tier)

    r = client.post(
        "/targets/1/retention",
        data={"retention_daily": "1", "retention_weekly": "4", "retention_monthly": "2"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    assert os.path.exists(paths[0])  # first of week/month, keeps weekly/monthly
    assert not os.path.exists(paths[1])  # daily-only, aged out
    assert os.path.exists(paths[2])  # most recent daily


def test_edit_connection_rejects_nonexistent_container(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)

    with patch.object(docker_client, "get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.containers.get.side_effect = NotFound("nope")
        r = client.post(
            "/targets/1/edit",
            data={"container_name": "missing-c", "db_user": "u", "db_name": "d"},
        )

    assert r.status_code == 400
    assert "not found" in r.text

    conn = db_module.get_connection(state_db)
    target = db_module.get_target(conn, 1)
    assert target["container_name"] == "t1-c"  # unchanged


def test_edit_connection_saves_valid_values(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)

    with patch.object(docker_client, "get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.containers.get.return_value = MagicMock()
        r = client.post(
            "/targets/1/edit",
            data={"container_name": "new-c", "db_user": "newuser", "db_name": "newdb"},
            follow_redirects=False,
        )

    assert r.status_code == 303
    conn = db_module.get_connection(state_db)
    target = db_module.get_target(conn, 1)
    assert target["container_name"] == "new-c"
    assert target["db_user"] == "newuser"
    assert target["db_name"] == "newdb"
    assert target["engine"] == "postgres"  # unchanged, no engine field accepted at all


def test_toggle_enabled_flips_flag_and_syncs_schedule(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)

    with patch("app.routes.targets.scheduler.sync_target_schedule") as mock_sync:
        r = client.post("/targets/1/toggle-enabled", follow_redirects=False)
        assert r.status_code == 303
        mock_sync.assert_called_once()

    conn = db_module.get_connection(state_db)
    assert db_module.get_target(conn, 1)["enabled"] == 0

    with patch("app.routes.targets.scheduler.sync_target_schedule"):
        client.post("/targets/1/toggle-enabled")

    assert db_module.get_target(conn, 1)["enabled"] == 1


def test_delete_rejected_while_in_progress(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)

    jobs.try_claim(1)
    try:
        r = client.post("/targets/1/delete", data={"confirm_name": "t1"})
        assert r.status_code == 400
        assert "currently running" in r.text
    finally:
        jobs.release(1)

    conn = db_module.get_connection(state_db)
    assert db_module.get_target(conn, 1) is not None


def test_delete_rejected_when_name_does_not_match(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)
    _create_target(client)

    r = client.post("/targets/1/delete", data={"confirm_name": "wrong-name"})
    assert r.status_code == 400
    assert "does not match" in r.text

    conn = db_module.get_connection(state_db)
    assert db_module.get_target(conn, 1) is not None


def test_delete_without_file_checkbox_keeps_files_on_disk(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db, backup_target_dir=str(tmp_path))
    _create_target(client)

    path = str(tmp_path / "a.dump")
    with open(path, "wb") as f:
        f.write(b"dump")
    conn = db_module.get_connection(state_db)
    run_id = db_module.create_backup_run(conn, 1, status="running")
    db_module.finish_backup_run(conn, run_id, "success", file_path=path, file_size_bytes=4)

    with patch("app.routes.targets.scheduler.remove_target_job"):
        r = client.post("/targets/1/delete", data={"confirm_name": "t1"}, follow_redirects=False)

    assert r.status_code == 303
    assert db_module.get_target(conn, 1) is None
    assert os.path.exists(path)


def test_delete_with_file_checkbox_removes_files(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db, backup_target_dir=str(tmp_path))
    _create_target(client)

    path = str(tmp_path / "a.dump")
    with open(path, "wb") as f:
        f.write(b"dump")
    conn = db_module.get_connection(state_db)
    run_id = db_module.create_backup_run(conn, 1, status="running")
    db_module.finish_backup_run(conn, run_id, "success", file_path=path, file_size_bytes=4)

    with patch("app.routes.targets.scheduler.remove_target_job"):
        r = client.post(
            "/targets/1/delete",
            data={"confirm_name": "t1", "delete_files": "true"},
            follow_redirects=False,
        )

    assert r.status_code == 303
    assert db_module.get_target(conn, 1) is None
    assert not os.path.exists(path)


def _create_eligible_backup(conn, target_id, tmp_path, name="a.dump"):
    path = str(tmp_path / name)
    with open(path, "wb") as f:
        f.write(b"dump")
    run_id = db_module.create_backup_run(conn, target_id, status="running")
    db_module.finish_backup_run(conn, run_id, "success", file_path=path, file_size_bytes=4)
    return run_id


def test_restore_rejects_name_mismatch(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db, backup_target_dir=str(tmp_path))
    _create_target(client)
    conn = db_module.get_connection(state_db)
    run_id = _create_eligible_backup(conn, 1, tmp_path)

    r = client.post("/targets/1/restore", data={"backup_run_id": run_id, "confirm_name": "wrong-name"})

    assert r.status_code == 200
    assert "does not match" in r.text
    assert db_module.list_restore_runs(conn, 1) == []


def test_restore_rejects_ineligible_backup(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db, backup_target_dir=str(tmp_path))
    _create_target(client)
    conn = db_module.get_connection(state_db)
    _create_eligible_backup(conn, 1, tmp_path)

    r = client.post("/targets/1/restore", data={"backup_run_id": 9999, "confirm_name": "t1"})

    assert r.status_code == 200
    assert "not eligible" in r.text
    assert db_module.list_restore_runs(conn, 1) == []


def test_restore_rejects_when_target_busy(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db, backup_target_dir=str(tmp_path))
    _create_target(client)
    conn = db_module.get_connection(state_db)
    run_id = _create_eligible_backup(conn, 1, tmp_path)

    jobs.try_claim(1)
    try:
        r = client.post("/targets/1/restore", data={"backup_run_id": run_id, "confirm_name": "t1"})
        assert r.status_code == 200
        assert "already in progress" in r.text
        assert db_module.list_restore_runs(conn, 1) == []
    finally:
        jobs.release(1)


def test_restore_dispatches_and_creates_running_row(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db, backup_target_dir=str(tmp_path))
    _create_target(client)
    conn = db_module.get_connection(state_db)
    run_id = _create_eligible_backup(conn, 1, tmp_path)

    try:
        with patch("app.routes.targets.scheduler.dispatch_restore") as mock_dispatch:
            r = client.post("/targets/1/restore", data={"backup_run_id": run_id, "confirm_name": "t1"})
            mock_dispatch.assert_called_once()

        assert r.status_code == 200
        restore_runs = db_module.list_restore_runs(conn, 1)
        assert len(restore_runs) == 1
        assert restore_runs[0]["status"] == "running"
        # The route only claims and dispatches; release() lives inside
        # execute_restore_claimed(), which never actually runs here since
        # dispatch_restore was mocked out, so the claim stays held.
        assert 1 in jobs._in_progress
    finally:
        jobs.release(1)


def test_restore_ignores_stop_container_checkbox_for_non_sqlite_engine(tmp_path):
    """stop_container is a SQLite-only option, the route must not pass it through for a
    postgres target even if a client sent it anyway."""
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db, backup_target_dir=str(tmp_path))
    _create_target(client)
    conn = db_module.get_connection(state_db)
    run_id = _create_eligible_backup(conn, 1, tmp_path)

    with patch("app.routes.targets.scheduler.dispatch_restore") as mock_dispatch:
        client.post(
            "/targets/1/restore",
            data={"backup_run_id": run_id, "confirm_name": "t1", "stop_container": "true"},
        )
        call_args = mock_dispatch.call_args[0]
        assert call_args[3] is False  # effective_stop forced False for non-sqlite engine

    jobs.release(1)


def test_delete_shows_file_count_verified_against_disk(tmp_path):
    """The delete confirmation's file count must reflect files actually still present,
    not a raw count of historical file_path values (some may already be pruned)."""
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db, backup_target_dir=str(tmp_path))
    _create_target(client)

    existing_path = str(tmp_path / "exists.dump")
    with open(existing_path, "wb") as f:
        f.write(b"dump")
    pruned_path = str(tmp_path / "pruned.dump")  # never created, simulating a prior prune

    conn = db_module.get_connection(state_db)
    run1 = db_module.create_backup_run(conn, 1, status="running")
    db_module.finish_backup_run(conn, run1, "success", file_path=existing_path, file_size_bytes=4)
    run2 = db_module.create_backup_run(conn, 1, status="running")
    db_module.finish_backup_run(conn, run2, "success", file_path=pruned_path, file_size_bytes=4)

    r = client.get("/targets/1")
    assert "has 1 backup file" in r.text
    assert "Also delete 1 backup file" in r.text
