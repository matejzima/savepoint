from unittest.mock import MagicMock, patch

from docker.errors import NotFound

from app import db as db_module
from app import restore as restore_module
from app.adapters.base import RestoreResult


def _target_row(**overrides):
    row = {"id": 1, "name": "mydb", "engine": "postgres", "container_name": "mydb-c"}
    row.update(overrides)
    return row


def _backup_run(**overrides):
    row = {"id": 5, "started_at": "2026-01-01T00:00:00+00:00", "file_path": "/tmp/backup.dump"}
    row.update(overrides)
    return row


def _make_restore_run(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_id = db_module.create_target(conn, "mydb", "postgres", "mydb-c", "u", "d")
    backup_run_id = db_module.create_backup_run(conn, target_id, status="running")
    db_module.finish_backup_run(conn, backup_run_id, "success", file_path="/tmp/backup.dump", file_size_bytes=1)
    restore_run_id = db_module.create_restore_run(conn, target_id, backup_run_id, status="running")
    return conn, target_id, backup_run_id, restore_run_id


@patch("app.restore.notifications")
@patch("app.restore.docker_client")
@patch("app.restore.ADAPTERS", {"postgres": MagicMock()})
def test_perform_restore_success_records_run_and_notifies(mock_docker_client, mock_notifications, tmp_path):
    from app.restore import ADAPTERS

    ADAPTERS["postgres"].restore.return_value = RestoreResult(success=True, error_message=None)
    mock_docker_client.get_client.return_value = MagicMock()

    conn, target_id, backup_run_id, restore_run_id = _make_restore_run(tmp_path)
    target = db_module.get_target(conn, target_id)
    backup_run = db_module.get_backup_run(conn, backup_run_id)

    restore_module.perform_restore(conn, target, restore_run_id, backup_run, stop_container=False)

    run = conn.execute("SELECT * FROM restore_runs WHERE id = ?", (restore_run_id,)).fetchone()
    assert run["status"] == "success"
    assert run["stopped_container"] == 0
    mock_notifications.notify_restore_result.assert_called_once()
    args = mock_notifications.notify_restore_result.call_args[0]
    assert args[2] is True  # success


@patch("app.restore.notifications")
@patch("app.restore.docker_client")
@patch("app.restore.ADAPTERS", {"postgres": MagicMock()})
def test_perform_restore_failure_records_run_and_notifies(mock_docker_client, mock_notifications, tmp_path):
    from app.restore import ADAPTERS

    ADAPTERS["postgres"].restore.return_value = RestoreResult(success=False, error_message="boom")
    mock_docker_client.get_client.return_value = MagicMock()

    conn, target_id, backup_run_id, restore_run_id = _make_restore_run(tmp_path)
    target = db_module.get_target(conn, target_id)
    backup_run = db_module.get_backup_run(conn, backup_run_id)

    restore_module.perform_restore(conn, target, restore_run_id, backup_run, stop_container=False)

    run = conn.execute("SELECT * FROM restore_runs WHERE id = ?", (restore_run_id,)).fetchone()
    assert run["status"] == "failure"
    assert run["error_message"] == "boom"
    args = mock_notifications.notify_restore_result.call_args[0]
    assert args[2] is False


@patch("app.restore.notifications")
@patch("app.restore.docker_client")
@patch("app.restore.ADAPTERS", {"sqlite": MagicMock()})
def test_perform_restore_stops_and_starts_container_for_sqlite(mock_docker_client, mock_notifications, tmp_path):
    from app.restore import ADAPTERS

    ADAPTERS["sqlite"].restore.return_value = RestoreResult(success=True, error_message=None)
    mock_docker_client.get_client.return_value = MagicMock()

    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_id = db_module.create_target(conn, "mydb", "sqlite", "mydb-c", "", "", file_path="/data/app.db")
    backup_run_id = db_module.create_backup_run(conn, target_id, status="running")
    db_module.finish_backup_run(conn, backup_run_id, "success", file_path="/tmp/backup.db", file_size_bytes=1)
    restore_run_id = db_module.create_restore_run(conn, target_id, backup_run_id, status="running")
    target = db_module.get_target(conn, target_id)
    backup_run = db_module.get_backup_run(conn, backup_run_id)

    restore_module.perform_restore(conn, target, restore_run_id, backup_run, stop_container=True)

    mock_docker_client.stop_container.assert_called_once()
    mock_docker_client.start_container.assert_called_once()
    run = conn.execute("SELECT * FROM restore_runs WHERE id = ?", (restore_run_id,)).fetchone()
    assert run["status"] == "success"
    assert run["stopped_container"] == 1


@patch("app.restore.notifications")
@patch("app.restore.docker_client")
@patch("app.restore.ADAPTERS", {"postgres": MagicMock()})
def test_perform_restore_fails_without_touching_file_if_stop_fails(mock_docker_client, mock_notifications, tmp_path):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.stop_container.side_effect = NotFound("no such container")

    from app.restore import ADAPTERS

    conn, target_id, backup_run_id, restore_run_id = _make_restore_run(tmp_path)
    target = db_module.get_target(conn, target_id)
    backup_run = db_module.get_backup_run(conn, backup_run_id)

    restore_module.perform_restore(conn, target, restore_run_id, backup_run, stop_container=True)

    ADAPTERS["postgres"].restore.assert_not_called()
    run = conn.execute("SELECT * FROM restore_runs WHERE id = ?", (restore_run_id,)).fetchone()
    assert run["status"] == "failure"
    assert "not found" in run["error_message"]


@patch("app.restore.notifications")
@patch("app.restore.docker_client")
@patch("app.restore.ADAPTERS", {"sqlite": MagicMock()})
def test_perform_restore_success_notes_manual_start_needed_if_start_fails(mock_docker_client, mock_notifications, tmp_path):
    from app.restore import ADAPTERS

    ADAPTERS["sqlite"].restore.return_value = RestoreResult(success=True, error_message=None)
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.start_container.side_effect = NotFound("no such container")

    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_id = db_module.create_target(conn, "mydb", "sqlite", "mydb-c", "", "", file_path="/data/app.db")
    backup_run_id = db_module.create_backup_run(conn, target_id, status="running")
    db_module.finish_backup_run(conn, backup_run_id, "success", file_path="/tmp/backup.db", file_size_bytes=1)
    restore_run_id = db_module.create_restore_run(conn, target_id, backup_run_id, status="running")
    target = db_module.get_target(conn, target_id)
    backup_run = db_module.get_backup_run(conn, backup_run_id)

    restore_module.perform_restore(conn, target, restore_run_id, backup_run, stop_container=True)

    run = conn.execute("SELECT * FROM restore_runs WHERE id = ?", (restore_run_id,)).fetchone()
    assert run["status"] == "success"  # restore itself succeeded
    assert "start it back up" in run["error_message"]


class FakeSettings:
    def __init__(self, state_db_path):
        self.state_db_path = state_db_path


def test_perform_restore_agent_owned_target_uses_remote_lifecycle_not_local_stop_start(tmp_path):
    """An agent-owned target has no local container for perform_restore() to stop/start
    itself, that branches out to RemoteAdapter.restore_with_lifecycle() before any of the
    local docker_client orchestration runs.
    """
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    target_id = db_module.create_target(conn, "remote-db", "postgres", "remote-c", "u", "d", agent_id=agent_id)
    backup_run_id = db_module.create_backup_run(conn, target_id, status="running")
    db_module.finish_backup_run(conn, backup_run_id, "success", file_path="/tmp/backup.dump", file_size_bytes=1)
    restore_run_id = db_module.create_restore_run(conn, target_id, backup_run_id, status="running")
    target = db_module.get_target(conn, target_id)
    backup_run = db_module.get_backup_run(conn, backup_run_id)

    restore_module.init(FakeSettings(state_db))

    with patch("app.adapters.remote.RemoteAdapter.restore_with_lifecycle") as mock_restore, patch(
        "app.restore.docker_client"
    ) as mock_docker_client:
        mock_restore.return_value = RestoreResult(success=True, error_message=None, stopped_container=True)
        restore_module.perform_restore(conn, target, restore_run_id, backup_run, stop_container=True)
        mock_restore.assert_called_once()
        mock_docker_client.stop_container.assert_not_called()
        mock_docker_client.start_container.assert_not_called()

    run = conn.execute("SELECT * FROM restore_runs WHERE id = ?", (restore_run_id,)).fetchone()
    assert run["status"] == "success"
    assert run["stopped_container"] == 1


def test_perform_restore_agent_owned_target_failure_records_and_notifies(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    target_id = db_module.create_target(conn, "remote-db", "postgres", "remote-c", "u", "d", agent_id=agent_id)
    backup_run_id = db_module.create_backup_run(conn, target_id, status="running")
    db_module.finish_backup_run(conn, backup_run_id, "success", file_path="/tmp/backup.dump", file_size_bytes=1)
    restore_run_id = db_module.create_restore_run(conn, target_id, backup_run_id, status="running")
    target = db_module.get_target(conn, target_id)
    backup_run = db_module.get_backup_run(conn, backup_run_id)

    restore_module.init(FakeSettings(state_db))

    with patch("app.adapters.remote.RemoteAdapter.restore_with_lifecycle") as mock_restore, patch(
        "app.restore.notifications"
    ) as mock_notifications:
        mock_restore.return_value = RestoreResult(success=False, error_message="agent unreachable", stopped_container=False)
        restore_module.perform_restore(conn, target, restore_run_id, backup_run, stop_container=False)
        mock_notifications.notify_restore_result.assert_called_once()

    run = conn.execute("SELECT * FROM restore_runs WHERE id = ?", (restore_run_id,)).fetchone()
    assert run["status"] == "failure"
    assert run["error_message"] == "agent unreachable"
