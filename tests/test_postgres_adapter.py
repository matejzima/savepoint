from unittest.mock import MagicMock, patch

from docker.errors import NotFound

from app.adapters.postgres import PASSWORD_ENV_VAR, PostgresAdapter


def _target_row(**overrides):
    row = {
        "name": "mydb",
        "container_name": "mydb-container",
        "db_user": "postgres",
        "db_name": "mydb",
    }
    row.update(overrides)
    return row


@patch("app.adapters.postgres.docker_client")
def test_backup_fails_when_container_not_found(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.side_effect = NotFound("no such container")

    result = PostgresAdapter().backup(_target_row(), "/tmp/backup-target")

    assert result.success is False
    assert "not found" in result.error_message


@patch("app.adapters.postgres.docker_client")
def test_backup_fails_when_password_env_var_missing(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {}

    result = PostgresAdapter().backup(_target_row(), "/tmp/backup-target")

    assert result.success is False
    assert PASSWORD_ENV_VAR in result.error_message


@patch("app.adapters.postgres.docker_client")
def test_backup_succeeds_and_records_file(mock_docker_client, tmp_path):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {PASSWORD_ENV_VAR: "secret"}

    written = {}

    def fake_exec_pg_dump(client, container_name, user, db_name, password, dest_path):
        with open(dest_path, "wb") as f:
            f.write(b"fake dump contents")
        written["path"] = dest_path
        return 0, ""

    mock_docker_client.exec_pg_dump.side_effect = fake_exec_pg_dump

    result = PostgresAdapter().backup(_target_row(), str(tmp_path))

    assert result.success is True
    assert result.file_path == written["path"]
    assert result.file_size_bytes == len(b"fake dump contents")


@patch("app.adapters.postgres.docker_client")
def test_backup_fails_and_removes_partial_file_on_nonzero_exit(mock_docker_client, tmp_path):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {PASSWORD_ENV_VAR: "secret"}

    def fake_exec_pg_dump(client, container_name, user, db_name, password, dest_path):
        with open(dest_path, "wb") as f:
            f.write(b"partial")
        return 1, "pg_dump: error: connection failed"

    mock_docker_client.exec_pg_dump.side_effect = fake_exec_pg_dump

    result = PostgresAdapter().backup(_target_row(), str(tmp_path))

    assert result.success is False
    assert "connection failed" in result.error_message
    assert list(tmp_path.rglob("*.dump")) == []


@patch("app.adapters.postgres.docker_client")
def test_restore_fails_when_container_not_found(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.side_effect = NotFound("no such container")

    result = PostgresAdapter().restore(_target_row(), "/tmp/backup.dump")

    assert result.success is False
    assert "not found" in result.error_message


@patch("app.adapters.postgres.docker_client")
def test_restore_fails_when_password_env_var_missing(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {}

    result = PostgresAdapter().restore(_target_row(), "/tmp/backup.dump")

    assert result.success is False
    assert PASSWORD_ENV_VAR in result.error_message


@patch("app.adapters.postgres.docker_client")
def test_restore_pushes_file_and_runs_pg_restore_with_clean_if_exists(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {PASSWORD_ENV_VAR: "secret"}
    mock_docker_client.exec_simple.return_value = (0, "", "")

    result = PostgresAdapter().restore(_target_row(), "/tmp/backup.dump")

    assert result.success is True
    put_args = mock_docker_client.put_archive_file.call_args[0]
    assert put_args[1] == "mydb-container"
    assert put_args[3] == "/tmp/backup.dump"
    restore_cmd = mock_docker_client.exec_simple.call_args_list[0][0][2]
    assert restore_cmd[0] == "pg_restore"
    assert "--clean" in restore_cmd
    assert "--if-exists" in restore_cmd


@patch("app.adapters.postgres.docker_client")
def test_restore_fails_on_nonzero_exit_and_still_cleans_up_temp_file(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {PASSWORD_ENV_VAR: "secret"}
    mock_docker_client.exec_simple.return_value = (1, "", "pg_restore: error: connection failed")

    result = PostgresAdapter().restore(_target_row(), "/tmp/backup.dump")

    assert result.success is False
    assert "connection failed" in result.error_message
    cleanup_cmd = mock_docker_client.exec_simple.call_args_list[-1][0][2]
    assert cleanup_cmd == ["rm", "-f", "/tmp/savepoint-restore.dump"]
