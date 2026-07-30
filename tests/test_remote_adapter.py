import os
from unittest.mock import MagicMock, patch

from app import agent_client
from app import db as db_module
from app.adapters.remote import RemoteAdapter, remote_adapter_for


class FakeSettings:
    def __init__(self, state_db_path=None):
        self.state_db_path = state_db_path


def _agent(**overrides):
    row = {"id": 1, "name": "remote-host", "base_url": "http://remote-host:8000", "token": "tok"}
    row.update(overrides)
    return row


def _target_row(**overrides):
    row = {
        "name": "mydb",
        "engine": "postgres",
        "container_name": "mydb-c",
        "db_user": "u",
        "db_name": "d",
        "file_path": None,
        "agent_id": 1,
    }
    row.update(overrides)
    return row


def _fake_response(chunks, status_code=200, filename="mydb_20260101T000000Z.dump", method=None):
    response = MagicMock()
    response.status_code = status_code
    response.headers = {"X-Savepoint-Filename": filename} if filename else {}
    if method:
        response.headers["X-Savepoint-Method"] = method
    response.iter_content.return_value = iter(chunks)
    return response


def test_backup_writes_temp_then_renames_on_success(tmp_path):
    adapter = RemoteAdapter(_agent(), FakeSettings())
    response = _fake_response([b"hello ", b"world"])

    with patch("app.adapters.remote.agent_client.open_backup_stream", return_value=response):
        result = adapter.backup(_target_row(), str(tmp_path))

    assert result.success is True
    expected_path = os.path.join(str(tmp_path), "mydb", "mydb_20260101T000000Z.dump")
    assert result.file_path == expected_path
    with open(expected_path, "rb") as f:
        assert f.read() == b"hello world"
    assert not os.path.exists(os.path.join(str(tmp_path), "mydb", ".mydb.part"))
    response.close.assert_called_once()


def test_backup_carries_method_header_through(tmp_path):
    adapter = RemoteAdapter(_agent(), FakeSettings())
    response = _fake_response([b"data"], filename="mydb_x.sqlite3", method="raw-copy")

    with patch("app.adapters.remote.agent_client.open_backup_stream", return_value=response):
        result = adapter.backup(_target_row(engine="sqlite"), str(tmp_path))

    assert result.success is True
    assert result.method == "raw-copy"


def test_backup_mid_stream_failure_leaves_no_partial_file_at_destination(tmp_path):
    """A connection drop or local write failure partway through must never leave a
    truncated file sitting at the path retention/restore would later treat as a
    complete, valid backup.
    """
    adapter = RemoteAdapter(_agent(), FakeSettings())

    def broken_iter():
        yield b"partial-data"
        raise OSError("disk full")

    response = MagicMock()
    response.status_code = 200
    response.headers = {"X-Savepoint-Filename": "mydb_20260101T000000Z.dump"}
    response.iter_content.return_value = broken_iter()

    with patch("app.adapters.remote.agent_client.open_backup_stream", return_value=response):
        result = adapter.backup(_target_row(), str(tmp_path))

    assert result.success is False
    dest_path = os.path.join(str(tmp_path), "mydb", "mydb_20260101T000000Z.dump")
    assert not os.path.exists(dest_path)
    assert not os.path.exists(os.path.join(str(tmp_path), "mydb", ".mydb.part"))


def test_backup_connection_error_surfaces_as_normal_failure_result(tmp_path):
    adapter = RemoteAdapter(_agent(), FakeSettings())

    with patch(
        "app.adapters.remote.agent_client.open_backup_stream",
        side_effect=agent_client.AgentError("could not reach agent 'remote-host': connection refused"),
    ):
        result = adapter.backup(_target_row(), str(tmp_path))

    assert result.success is False
    assert "connection refused" in result.error_message
    # the target's directory may exist (created up front for the temp path), but no
    # backup file was ever produced inside it
    assert os.listdir(os.path.join(str(tmp_path), "mydb")) == []


def test_backup_missing_filename_header_is_a_failure_not_a_crash(tmp_path):
    adapter = RemoteAdapter(_agent(), FakeSettings())
    response = _fake_response([b"data"], filename=None)

    with patch("app.adapters.remote.agent_client.open_backup_stream", return_value=response):
        result = adapter.backup(_target_row(), str(tmp_path))

    assert result.success is False


def test_restore_with_lifecycle_success_reports_stopped_container():
    adapter = RemoteAdapter(_agent(), FakeSettings())
    with patch(
        "app.adapters.remote.agent_client.run_restore",
        return_value={"success": True, "stopped_container": True, "error": None},
    ):
        result = adapter.restore_with_lifecycle(_target_row(), "/tmp/backup.dump", True)

    assert result.success is True
    assert result.stopped_container is True
    assert result.error_message is None


def test_restore_with_lifecycle_failure_result_from_agent():
    adapter = RemoteAdapter(_agent(), FakeSettings())
    with patch(
        "app.adapters.remote.agent_client.run_restore",
        return_value={"success": False, "stopped_container": False, "error": "restore failed"},
    ):
        result = adapter.restore_with_lifecycle(_target_row(), "/tmp/backup.dump", False)

    assert result.success is False
    assert result.error_message == "restore failed"


def test_restore_with_lifecycle_connection_failure_surfaces_as_normal_failure_result():
    adapter = RemoteAdapter(_agent(), FakeSettings())
    with patch(
        "app.adapters.remote.agent_client.run_restore",
        side_effect=agent_client.AgentError("could not reach agent 'remote-host': timeout"),
    ):
        result = adapter.restore_with_lifecycle(_target_row(), "/tmp/backup.dump", True)

    assert result.success is False
    assert result.stopped_container is False
    assert "timeout" in result.error_message


def test_remote_adapter_for_returns_none_for_local_target(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    settings = FakeSettings(state_db)
    assert remote_adapter_for(_target_row(agent_id=None), settings) is None


def test_remote_adapter_for_returns_instance_for_agent_owned_target(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    settings = FakeSettings(state_db)

    adapter = remote_adapter_for(_target_row(agent_id=agent_id), settings)

    assert isinstance(adapter, RemoteAdapter)
    assert adapter.agent["name"] == "remote-host"
