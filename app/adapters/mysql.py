import os
from datetime import datetime, timezone

from docker.errors import APIError, NotFound

from .. import docker_client
from . import base

RESTORE_TMP_PATH = "/tmp/savepoint-restore.sql"


class _MySQLFamilyAdapter:
    image_keyword: str = ""
    dump_binary: str = ""
    restore_client_binary: str = ""
    user_env_candidates: list = []
    database_env_candidates: list = []
    password_env_candidates: list = []
    root_password_env_candidates: list = []

    def discover(self, container):
        return self.image_keyword in docker_client.get_image_name(container)

    def default_connection_info(self, container):
        env = docker_client.parse_env(container)
        return {
            "db_user": self._first_present(env, self.user_env_candidates) or "",
            "db_name": self._first_present(env, self.database_env_candidates) or "",
        }

    def backup(self, target_row, backup_target_dir: str) -> base.BackupResult:
        client = docker_client.get_client()
        container_name = target_row["container_name"]
        db_user = target_row["db_user"]

        try:
            env = docker_client.get_container_env(client, container_name)
        except NotFound:
            return base.BackupResult(
                success=False,
                file_path=None,
                file_size_bytes=None,
                error_message=f"container '{container_name}' not found",
            )

        candidates = self.root_password_env_candidates if db_user == "root" else self.password_env_candidates
        password = self._first_present(env, candidates)
        if not password:
            return base.BackupResult(
                success=False,
                file_path=None,
                file_size_bytes=None,
                error_message=(
                    f"container '{container_name}' has none of the expected password "
                    f"environment variables set ({', '.join(candidates)}), cannot authenticate"
                ),
            )

        dest_path = self._dest_path(backup_target_dir, target_row["name"])
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        cmd = [self.dump_binary, "--user", db_user, target_row["db_name"]]
        exit_code, stderr_text = docker_client.exec_and_capture(
            client, container_name, cmd, {"MYSQL_PWD": password}, dest_path
        )

        if exit_code != 0:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            return base.BackupResult(
                success=False,
                file_path=None,
                file_size_bytes=None,
                error_message=stderr_text.strip() or f"{self.dump_binary} exited with code {exit_code}",
            )

        return base.BackupResult(
            success=True,
            file_path=dest_path,
            file_size_bytes=os.path.getsize(dest_path),
            error_message=None,
        )

    def restore(self, target_row, source_path: str) -> base.RestoreResult:
        client = docker_client.get_client()
        container_name = target_row["container_name"]
        db_user = target_row["db_user"]
        db_name = target_row["db_name"]

        try:
            env = docker_client.get_container_env(client, container_name)
        except NotFound:
            return base.RestoreResult(success=False, error_message=f"container '{container_name}' not found")

        candidates = self.root_password_env_candidates if db_user == "root" else self.password_env_candidates
        password = self._first_present(env, candidates)
        if not password:
            return base.RestoreResult(
                success=False,
                error_message=(
                    f"container '{container_name}' has none of the expected password "
                    f"environment variables set ({', '.join(candidates)}), cannot authenticate"
                ),
            )

        try:
            docker_client.put_archive_file(client, container_name, RESTORE_TMP_PATH, source_path)
        except NotFound:
            return base.RestoreResult(success=False, error_message=f"container '{container_name}' not found")

        try:
            error = self._drop_existing_tables(client, container_name, db_user, db_name, password)
            if error:
                return base.RestoreResult(success=False, error_message=error)

            cmd = [
                self.restore_client_binary,
                "--user", db_user,
                db_name,
                f"--execute=source {RESTORE_TMP_PATH}",
            ]
            exit_code, _stdout_text, stderr_text = docker_client.exec_simple(
                client, container_name, cmd, {"MYSQL_PWD": password}
            )
        finally:
            self._cleanup_remote_file(client, container_name, RESTORE_TMP_PATH)

        if exit_code != 0:
            return base.RestoreResult(
                success=False,
                error_message=stderr_text.strip() or f"{self.restore_client_binary} exited with code {exit_code}",
            )

        return base.RestoreResult(success=True, error_message=None)

    def _drop_existing_tables(self, client, container_name: str, db_user: str, db_name: str, password: str):
        """Drop every table currently in db_name so sourcing the dump replaces rather than
        merges with whatever's already there. Deliberately does NOT use DROP DATABASE /
        CREATE DATABASE: the official images' MYSQL_USER grant (GRANT ALL PRIVILEGES ON
        <db>.*) is scoped to this one database, not the instance-level CREATE/DROP
        privilege dropping a database itself requires, so that approach would fail using
        the same credentials backups already rely on. FOREIGN_KEY_CHECKS is disabled and
        re-enabled as two separate statements (not one string with the drop in between),
        with re-enabling in a `finally`, so a failed drop can never leave it silently
        disabled for whatever uses this database afterward.

        Returns an error message, or None on success.
        """
        list_cmd = [
            self.restore_client_binary,
            "--user", db_user,
            "--batch", "--skip-column-names",
            f"--execute=SELECT table_name FROM information_schema.tables WHERE table_schema='{db_name}'",
        ]
        exit_code, stdout_text, stderr_text = docker_client.exec_simple(
            client, container_name, list_cmd, {"MYSQL_PWD": password}
        )
        if exit_code != 0:
            return stderr_text.strip() or f"{self.restore_client_binary} exited with code {exit_code} while listing existing tables"

        tables = [line.strip() for line in stdout_text.splitlines() if line.strip()]
        if not tables:
            return None

        off_cmd = [
            self.restore_client_binary, "--user", db_user, db_name, "--execute=SET FOREIGN_KEY_CHECKS=0",
        ]
        exit_code, _stdout, stderr_text = docker_client.exec_simple(
            client, container_name, off_cmd, {"MYSQL_PWD": password}
        )
        if exit_code != 0:
            return stderr_text.strip() or f"{self.restore_client_binary} exited with code {exit_code} while disabling foreign key checks"

        try:
            drop_list = ", ".join(f"`{t}`" for t in tables)
            drop_cmd = [
                self.restore_client_binary, "--user", db_user, db_name,
                f"--execute=DROP TABLE IF EXISTS {drop_list}",
            ]
            exit_code, _stdout, stderr_text = docker_client.exec_simple(
                client, container_name, drop_cmd, {"MYSQL_PWD": password}
            )
            if exit_code != 0:
                return stderr_text.strip() or f"{self.restore_client_binary} exited with code {exit_code} while dropping existing tables"
            return None
        finally:
            on_cmd = [
                self.restore_client_binary, "--user", db_user, db_name, "--execute=SET FOREIGN_KEY_CHECKS=1",
            ]
            docker_client.exec_simple(client, container_name, on_cmd, {"MYSQL_PWD": password})

    @staticmethod
    def _cleanup_remote_file(client, container_name: str, remote_path: str) -> None:
        try:
            docker_client.exec_simple(client, container_name, ["rm", "-f", remote_path])
        except (APIError, NotFound):
            pass

    @staticmethod
    def _first_present(env: dict, names: list):
        for name in names:
            if env.get(name):
                return env[name]
        return None

    @staticmethod
    def _dest_path(backup_target_dir: str, target_name: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return os.path.join(backup_target_dir, target_name, f"{target_name}_{timestamp}.sql")


class MySQLAdapter(_MySQLFamilyAdapter):
    image_keyword = "mysql"
    dump_binary = "mysqldump"
    restore_client_binary = "mysql"
    user_env_candidates = ["MYSQL_USER"]
    database_env_candidates = ["MYSQL_DATABASE"]
    password_env_candidates = ["MYSQL_PASSWORD"]
    root_password_env_candidates = ["MYSQL_ROOT_PASSWORD"]


class MariaDBAdapter(_MySQLFamilyAdapter):
    image_keyword = "mariadb"
    # Official MariaDB images (10.6+) deprecate, and in newer versions drop, the
    # mysqldump/mysql compat symlinks in favor of mariadb-dump/mariadb.
    dump_binary = "mariadb-dump"
    restore_client_binary = "mariadb"
    user_env_candidates = ["MARIADB_USER", "MYSQL_USER"]
    database_env_candidates = ["MARIADB_DATABASE", "MYSQL_DATABASE"]
    password_env_candidates = ["MARIADB_PASSWORD", "MYSQL_PASSWORD"]
    root_password_env_candidates = ["MARIADB_ROOT_PASSWORD", "MYSQL_ROOT_PASSWORD"]
