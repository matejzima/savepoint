from unittest.mock import MagicMock, patch

from docker.errors import APIError, NotFound

from app.adapters.sqlite import SQLiteAdapter


def _target_row(**overrides):
    row = {
        "name": "mydb",
        "container_name": "app-container",
        "file_path": "/data/app.sqlite3",
    }
    row.update(overrides)
    return row


@patch("app.adapters.sqlite.docker_client")
def test_backup_fails_when_container_not_found(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.exec_simple.side_effect = NotFound("no such container")

    result = SQLiteAdapter().backup(_target_row(), "/tmp/backup-target")

    assert result.success is False
    assert "not found" in result.error_message
    assert result.method is None


@patch("app.adapters.sqlite.docker_client")
def test_live_backup_succeeds_when_sqlite3_available(mock_docker_client, tmp_path):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.exec_simple.return_value = (0, "", "")

    def fake_get_archive_file(client, container_name, path, dest_path):
        assert path == "/tmp/savepoint-sqlite-backup"
        with open(dest_path, "wb") as f:
            f.write(b"sqlite backup contents")

    mock_docker_client.get_archive_file.side_effect = fake_get_archive_file

    result = SQLiteAdapter().backup(_target_row(), str(tmp_path))

    assert result.success is True
    assert result.method == "live"
    assert result.file_size_bytes == len(b"sqlite backup contents")


@patch("app.adapters.sqlite.docker_client")
def test_falls_back_to_raw_copy_when_sqlite3_binary_missing(mock_docker_client, tmp_path):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.exec_simple.side_effect = APIError("exec: sqlite3: not found")

    def fake_get_archive_file(client, container_name, path, dest_path):
        assert path == "/data/app.sqlite3"
        with open(dest_path, "wb") as f:
            f.write(b"raw file contents")

    mock_docker_client.get_archive_file.side_effect = fake_get_archive_file

    result = SQLiteAdapter().backup(_target_row(), str(tmp_path))

    assert result.success is True
    assert result.method == "raw-copy"
    assert result.file_size_bytes == len(b"raw file contents")


@patch("app.adapters.sqlite.docker_client")
def test_falls_back_to_raw_copy_when_backup_command_exits_nonzero(mock_docker_client, tmp_path):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.exec_simple.return_value = (1, "", "Error: unable to open database file")

    def fake_get_archive_file(client, container_name, path, dest_path):
        with open(dest_path, "wb") as f:
            f.write(b"raw file contents")

    mock_docker_client.get_archive_file.side_effect = fake_get_archive_file

    result = SQLiteAdapter().backup(_target_row(), str(tmp_path))

    assert result.success is True
    assert result.method == "raw-copy"


@patch("app.adapters.sqlite.docker_client")
def test_raw_copy_fallback_fails_cleanly_when_file_missing(mock_docker_client, tmp_path):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.exec_simple.side_effect = APIError("exec: sqlite3: not found")
    mock_docker_client.get_archive_file.side_effect = NotFound("no such file")

    result = SQLiteAdapter().backup(_target_row(), str(tmp_path))

    assert result.success is False
    assert "not found" in result.error_message
    assert result.method is None


def test_discover_always_false():
    assert SQLiteAdapter().discover(MagicMock()) is False


@patch("app.adapters.sqlite.docker_client")
def test_restore_pushes_file_straight_onto_target_file_path(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()

    result = SQLiteAdapter().restore(_target_row(), "/tmp/backup.sqlite3")

    assert result.success is True
    put_args = mock_docker_client.put_archive_file.call_args[0]
    assert put_args[1] == "app-container"
    assert put_args[2] == "/data/app.sqlite3"
    assert put_args[3] == "/tmp/backup.sqlite3"


@patch("app.adapters.sqlite.docker_client")
def test_restore_fails_when_container_not_found(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.put_archive_file.side_effect = NotFound("no such container")

    result = SQLiteAdapter().restore(_target_row(), "/tmp/backup.sqlite3")

    assert result.success is False
    assert "not found" in result.error_message
