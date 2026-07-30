from unittest.mock import MagicMock, patch

import pytest
from docker.errors import NotFound

from app.adapters.mysql import MariaDBAdapter, MySQLAdapter


def _target_row(**overrides):
    row = {
        "name": "mydb",
        "container_name": "mydb-container",
        "db_user": "app",
        "db_name": "mydb",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("adapter_cls", [MySQLAdapter, MariaDBAdapter])
@patch("app.adapters.mysql.docker_client")
def test_backup_fails_when_container_not_found(mock_docker_client, adapter_cls):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.side_effect = NotFound("no such container")

    result = adapter_cls().backup(_target_row(), "/tmp/backup-target")

    assert result.success is False
    assert "not found" in result.error_message


@patch("app.adapters.mysql.docker_client")
def test_mysql_backup_fails_when_password_env_var_missing(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {}

    result = MySQLAdapter().backup(_target_row(), "/tmp/backup-target")

    assert result.success is False
    assert "MYSQL_PASSWORD" in result.error_message


@patch("app.adapters.mysql.docker_client")
def test_mysql_backup_uses_root_password_var_for_root_user(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {}

    result = MySQLAdapter().backup(_target_row(db_user="root"), "/tmp/backup-target")

    assert result.success is False
    assert "MYSQL_ROOT_PASSWORD" in result.error_message
    assert "MYSQL_PASSWORD" not in result.error_message


@patch("app.adapters.mysql.docker_client")
def test_mariadb_backup_tries_mariadb_var_before_mysql_var(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {"MYSQL_PASSWORD": "fallback-secret"}

    written = {}

    def fake_exec_and_capture(client, container_name, cmd, environment, dest_path):
        assert environment == {"MYSQL_PWD": "fallback-secret"}
        with open(dest_path, "wb") as f:
            f.write(b"-- dump --")
        written["path"] = dest_path
        return 0, ""

    mock_docker_client.exec_and_capture.side_effect = fake_exec_and_capture

    result = MariaDBAdapter().backup(_target_row(), "/tmp/backup-target")

    assert result.success is True
    assert result.file_path == written["path"]
    assert result.method is None


@pytest.mark.parametrize(
    "adapter_cls, expected_binary",
    [(MySQLAdapter, "mysqldump"), (MariaDBAdapter, "mariadb-dump")],
)
@patch("app.adapters.mysql.docker_client")
def test_dump_command_uses_correct_binary_per_adapter(mock_docker_client, adapter_cls, expected_binary):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {
        "MYSQL_PASSWORD": "secret",
        "MARIADB_PASSWORD": "secret",
    }

    captured_cmd = {}

    def fake_exec_and_capture(client, container_name, cmd, environment, dest_path):
        captured_cmd["cmd"] = cmd
        with open(dest_path, "wb") as f:
            f.write(b"-- dump --")
        return 0, ""

    mock_docker_client.exec_and_capture.side_effect = fake_exec_and_capture

    result = adapter_cls().backup(_target_row(), "/tmp/backup-target")

    assert result.success is True
    assert captured_cmd["cmd"][0] == expected_binary


@patch("app.adapters.mysql.docker_client")
def test_backup_fails_and_removes_partial_file_on_nonzero_exit(mock_docker_client, tmp_path):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {"MYSQL_PASSWORD": "secret"}

    def fake_exec_and_capture(client, container_name, cmd, environment, dest_path):
        with open(dest_path, "wb") as f:
            f.write(b"partial")
        return 1, "mysqldump: error: connection failed"

    mock_docker_client.exec_and_capture.side_effect = fake_exec_and_capture

    result = MySQLAdapter().backup(_target_row(), str(tmp_path))

    assert result.success is False
    assert "connection failed" in result.error_message
    assert list(tmp_path.rglob("*.sql")) == []


def test_mysql_discover_matches_image_keyword():
    container = MagicMock()
    with patch("app.adapters.mysql.docker_client") as mock_docker_client:
        mock_docker_client.get_image_name.return_value = "mysql:8.0"
        assert MySQLAdapter().discover(container) is True
        mock_docker_client.get_image_name.return_value = "mariadb:11"
        assert MySQLAdapter().discover(container) is False


def test_mariadb_default_connection_info_prefers_mariadb_env_vars():
    container = MagicMock()
    with patch("app.adapters.mysql.docker_client") as mock_docker_client:
        mock_docker_client.parse_env.return_value = {
            "MARIADB_USER": "mariauser",
            "MYSQL_USER": "mysqluser",
            "MARIADB_DATABASE": "mariadb_db",
        }
        info = MariaDBAdapter().default_connection_info(container)

    assert info == {"db_user": "mariauser", "db_name": "mariadb_db"}


@pytest.mark.parametrize(
    "adapter_cls, expected_binary",
    [(MySQLAdapter, "mysql"), (MariaDBAdapter, "mariadb")],
)
@patch("app.adapters.mysql.docker_client")
def test_restore_uses_correct_client_binary_per_adapter(mock_docker_client, adapter_cls, expected_binary):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {"MYSQL_PASSWORD": "secret", "MARIADB_PASSWORD": "secret"}
    # no existing tables -> list, source, cleanup: 3 exec_simple calls total
    mock_docker_client.exec_simple.side_effect = [(0, "", ""), (0, "", ""), (0, "", "")]

    result = adapter_cls().restore(_target_row(), "/tmp/backup.sql")

    assert result.success is True
    calls = mock_docker_client.exec_simple.call_args_list
    assert calls[0][0][2][0] == expected_binary  # table-listing query
    assert calls[1][0][2][0] == expected_binary  # sourcing the dump


@patch("app.adapters.mysql.docker_client")
def test_restore_fails_when_container_not_found(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.side_effect = NotFound("no such container")

    result = MySQLAdapter().restore(_target_row(), "/tmp/backup.sql")

    assert result.success is False
    assert "not found" in result.error_message


@patch("app.adapters.mysql.docker_client")
def test_restore_with_no_existing_tables_skips_drop_and_sources_dump(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {"MYSQL_PASSWORD": "secret"}
    # empty stdout -> no existing tables, no drop/FK toggle calls, just source + cleanup
    mock_docker_client.exec_simple.side_effect = [(0, "", ""), (0, "", ""), (0, "", "")]

    result = MySQLAdapter().restore(_target_row(), "/tmp/backup.sql")

    assert result.success is True
    calls = mock_docker_client.exec_simple.call_args_list
    # list tables, then source the dump, then cleanup: 3 calls total, no drop/FK calls
    assert len(calls) == 3
    assert "source" in calls[1][0][2][-1]
    assert calls[2][0][2] == ["rm", "-f", "/tmp/savepoint-restore.sql"]


@patch("app.adapters.mysql.docker_client")
def test_restore_drops_existing_tables_with_foreign_key_checks_toggled(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {"MYSQL_PASSWORD": "secret"}
    mock_docker_client.exec_simple.side_effect = [
        (0, "orders\ncustomers\n", ""),  # list tables
        (0, "", ""),  # FK checks off
        (0, "", ""),  # drop tables
        (0, "", ""),  # FK checks on
        (0, "", ""),  # source the dump
        (0, "", ""),  # cleanup temp file
    ]

    result = MySQLAdapter().restore(_target_row(), "/tmp/backup.sql")

    assert result.success is True
    calls = [c[0][2] for c in mock_docker_client.exec_simple.call_args_list]
    assert "SET FOREIGN_KEY_CHECKS=0" in calls[1][-1]
    assert "DROP TABLE IF EXISTS" in calls[2][-1]
    assert "`orders`" in calls[2][-1] and "`customers`" in calls[2][-1]
    assert "SET FOREIGN_KEY_CHECKS=1" in calls[3][-1]


@patch("app.adapters.mysql.docker_client")
def test_restore_reenables_foreign_key_checks_even_when_drop_fails(mock_docker_client):
    """A failed DROP TABLE must never leave FOREIGN_KEY_CHECKS silently disabled, that
    would be a hazard for anything using the database afterward. FK checks are re-enabled
    via a `finally`, not by a second statement in the same string as the drop.
    """
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {"MYSQL_PASSWORD": "secret"}
    mock_docker_client.exec_simple.side_effect = [
        (0, "orders\n", ""),  # list tables
        (0, "", ""),  # FK checks off
        (1, "", "DROP TABLE: permission denied"),  # drop tables fails
        (0, "", ""),  # FK checks on, must still run
        (0, "", ""),  # cleanup temp file
    ]

    result = MySQLAdapter().restore(_target_row(), "/tmp/backup.sql")

    assert result.success is False
    assert "permission denied" in result.error_message
    calls = [c[0][2] for c in mock_docker_client.exec_simple.call_args_list]
    assert "SET FOREIGN_KEY_CHECKS=1" in calls[3][-1]


@patch("app.adapters.mysql.docker_client")
def test_restore_cleans_up_temp_file_even_on_failure(mock_docker_client):
    mock_docker_client.get_client.return_value = MagicMock()
    mock_docker_client.get_container_env.return_value = {"MYSQL_PASSWORD": "secret"}
    mock_docker_client.exec_simple.side_effect = [
        (0, "", ""),  # list tables (none)
        (1, "", "mysql: error: syntax error"),  # source the dump fails
        (0, "", ""),  # cleanup temp file
    ]

    result = MySQLAdapter().restore(_target_row(), "/tmp/backup.sql")

    assert result.success is False
    assert "syntax error" in result.error_message
    cleanup_cmd = mock_docker_client.exec_simple.call_args_list[-1][0][2]
    assert cleanup_cmd == ["rm", "-f", "/tmp/savepoint-restore.sql"]
