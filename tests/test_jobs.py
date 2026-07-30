from unittest.mock import MagicMock, patch

from app import db as db_module
from app import jobs
from app.adapters.base import BackupResult


class FakeSettings:
    def __init__(self, state_db_path, backup_target_dir="/tmp"):
        self.state_db_path = state_db_path
        self.backup_target_dir = backup_target_dir


def _make_target(conn, name="t1", engine="postgres"):
    return db_module.create_target(conn, name, engine, f"{name}-container", "user", "db")


def setup_function(_):
    jobs._in_progress.clear()


def test_try_claim_and_release():
    assert jobs.try_claim(101) is True
    assert jobs.try_claim(101) is False
    jobs.release(101)
    assert jobs.try_claim(101) is True
    jobs.release(101)


def test_run_backup_schedule_collision_creates_skipped_row(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_id = _make_target(conn)
    conn.close()

    jobs.init(FakeSettings(state_db))
    jobs._in_progress.add(target_id)
    try:
        jobs.run_backup(target_id, "schedule")
    finally:
        jobs._in_progress.discard(target_id)

    conn = db_module.get_connection(state_db)
    runs = db_module.list_backup_runs(conn, target_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "skipped"
    assert "already has a run in progress" in runs[0]["error_message"]


def test_run_backup_window_collision_updates_existing_row_not_a_new_one(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_id = _make_target(conn)
    run_id = db_module.create_backup_run(conn, target_id, status="queued", triggered_by="window")
    conn.close()

    jobs.init(FakeSettings(state_db))
    jobs._in_progress.add(target_id)
    try:
        jobs.run_backup(target_id, "window", run_id=run_id)
    finally:
        jobs._in_progress.discard(target_id)

    conn = db_module.get_connection(state_db)
    runs = db_module.list_backup_runs(conn, target_id)
    assert len(runs) == 1
    assert runs[0]["id"] == run_id
    assert runs[0]["status"] == "skipped"


def test_run_backup_success_records_result_and_releases_claim(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_id = _make_target(conn)
    conn.close()

    jobs.init(FakeSettings(state_db))

    fake_adapter = MagicMock()
    fake_adapter.backup.return_value = BackupResult(
        success=True, file_path="/tmp/x.dump", file_size_bytes=10, error_message=None
    )

    with patch("app.jobs.ADAPTERS", {"postgres": fake_adapter}):
        jobs.run_backup(target_id, "manual")

    conn = db_module.get_connection(state_db)
    runs = db_module.list_backup_runs(conn, target_id)
    assert runs[0]["status"] == "success"
    assert runs[0]["triggered_by"] == "manual"
    assert target_id not in jobs._in_progress


def test_run_backup_failure_notifies(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_id = _make_target(conn)
    conn.close()

    jobs.init(FakeSettings(state_db))

    fake_adapter = MagicMock()
    fake_adapter.backup.return_value = BackupResult(
        success=False, file_path=None, file_size_bytes=None, error_message="boom"
    )

    with patch("app.jobs.ADAPTERS", {"postgres": fake_adapter}), patch(
        "app.jobs.notifications.notify_failure"
    ) as mock_notify:
        jobs.run_backup(target_id, "schedule")
        mock_notify.assert_called_once()

    conn = db_module.get_connection(state_db)
    runs = db_module.list_backup_runs(conn, target_id)
    assert runs[0]["status"] == "failure"
    assert runs[0]["error_message"] == "boom"


def test_execute_claimed_used_by_manual_dispatch(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_id = _make_target(conn)
    run_id = db_module.create_backup_run(conn, target_id, status="running", triggered_by="manual")
    conn.close()

    jobs.init(FakeSettings(state_db))
    jobs.try_claim(target_id)

    fake_adapter = MagicMock()
    fake_adapter.backup.return_value = BackupResult(
        success=True, file_path="/tmp/x.dump", file_size_bytes=5, error_message=None
    )

    with patch("app.jobs.ADAPTERS", {"postgres": fake_adapter}):
        jobs.execute_claimed(target_id, run_id)

    conn = db_module.get_connection(state_db)
    runs = db_module.list_backup_runs(conn, target_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert target_id not in jobs._in_progress


def test_execute_restore_claimed_fails_cleanly_when_target_gone(tmp_path):
    """Defensive-only case, mirroring execute_claimed()'s own "target is None" check:
    the same per-target lock that protects a manual backup against a mid-run delete also
    protects a restore, so in practice delete_target_route() can't run while this claim is
    held. FK constraints make constructing that state for real impossible (restore_runs
    references both targets and backup_runs, and delete_target() cascades restore_runs
    too), so this mocks the lookup instead of the database state.
    """
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_id = _make_target(conn)
    backup_run_id = db_module.create_backup_run(conn, target_id, status="running")
    db_module.finish_backup_run(conn, backup_run_id, "success", file_path="/tmp/x.dump", file_size_bytes=1)
    restore_run_id = db_module.create_restore_run(conn, target_id, backup_run_id, status="running")
    conn.close()

    jobs.init(FakeSettings(state_db))
    jobs.try_claim(target_id)

    with patch("app.db.get_target", return_value=None):
        jobs.execute_restore_claimed(target_id, restore_run_id, backup_run_id, False)

    conn = db_module.get_connection(state_db)
    run = conn.execute("SELECT * FROM restore_runs WHERE id = ?", (restore_run_id,)).fetchone()
    assert run["status"] == "failure"
    assert "no longer exists" in run["error_message"]
    assert target_id not in jobs._in_progress


def test_execute_restore_claimed_fails_cleanly_when_backup_run_gone(tmp_path):
    """Same defensive-only reasoning as the target-gone case above, mocked for the same
    FK-constraint reason (restore_runs.backup_run_id can't reference a nonexistent row)."""
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_id = _make_target(conn)
    backup_run_id = db_module.create_backup_run(conn, target_id, status="running")
    db_module.finish_backup_run(conn, backup_run_id, "success", file_path="/tmp/x.dump", file_size_bytes=1)
    restore_run_id = db_module.create_restore_run(conn, target_id, backup_run_id, status="running")
    conn.close()

    jobs.init(FakeSettings(state_db))
    jobs.try_claim(target_id)

    with patch("app.db.get_backup_run", return_value=None):
        jobs.execute_restore_claimed(target_id, restore_run_id, backup_run_id, False)

    conn = db_module.get_connection(state_db)
    run = conn.execute("SELECT * FROM restore_runs WHERE id = ?", (restore_run_id,)).fetchone()
    assert run["status"] == "failure"
    assert "backup run no longer exists" in run["error_message"]
    assert target_id not in jobs._in_progress


def test_execute_restore_claimed_delegates_to_perform_restore_and_releases_claim(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_id = _make_target(conn)
    backup_run_id = db_module.create_backup_run(conn, target_id, status="running")
    db_module.finish_backup_run(conn, backup_run_id, "success", file_path="/tmp/x.dump", file_size_bytes=1)
    restore_run_id = db_module.create_restore_run(conn, target_id, backup_run_id, status="running")
    conn.close()

    jobs.init(FakeSettings(state_db))
    jobs.try_claim(target_id)

    with patch("app.jobs.restore_module.perform_restore") as mock_perform:
        jobs.execute_restore_claimed(target_id, restore_run_id, backup_run_id, True)
        mock_perform.assert_called_once()
        call_args = mock_perform.call_args[0]
        assert call_args[2] == restore_run_id
        assert call_args[4] is True  # stop_container passed through

    assert target_id not in jobs._in_progress


def test_execute_dispatches_to_remote_adapter_for_agent_owned_target(tmp_path):
    """_execute()'s adapter lookup resolves to a RemoteAdapter for an agent-owned target,
    not the local ADAPTERS[engine] entry, everything after that lookup (finish_backup_run,
    retention) runs exactly as it would for a local target.
    """
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    target_id = db_module.create_target(conn, "remote-db", "postgres", "remote-c", "u", "d", agent_id=agent_id)
    conn.close()

    jobs.init(FakeSettings(state_db))

    with patch("app.adapters.remote.RemoteAdapter.backup") as mock_backup:
        mock_backup.return_value = BackupResult(
            success=True, file_path="/tmp/x.dump", file_size_bytes=5, error_message=None
        )
        jobs.run_backup(target_id, "manual")
        mock_backup.assert_called_once()

    conn = db_module.get_connection(state_db)
    runs = db_module.list_backup_runs(conn, target_id)
    assert runs[0]["status"] == "success"


def test_execute_claimed_works_regardless_of_enabled_flag(tmp_path):
    """enabled only gates automated dispatch (schedule/window), the shared execution
    path itself never looks at it, manual runs succeed against a disabled target."""
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_id = _make_target(conn)
    db_module.update_target_enabled(conn, target_id, False)
    run_id = db_module.create_backup_run(conn, target_id, status="running", triggered_by="manual")
    conn.close()

    jobs.init(FakeSettings(state_db))
    jobs.try_claim(target_id)

    fake_adapter = MagicMock()
    fake_adapter.backup.return_value = BackupResult(
        success=True, file_path="/tmp/x.dump", file_size_bytes=5, error_message=None
    )

    with patch("app.jobs.ADAPTERS", {"postgres": fake_adapter}):
        jobs.execute_claimed(target_id, run_id)

    conn = db_module.get_connection(state_db)
    runs = db_module.list_backup_runs(conn, target_id)
    assert runs[0]["status"] == "success"
