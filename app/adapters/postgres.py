import os
from datetime import datetime, timezone

from docker.errors import APIError, NotFound

from .. import docker_client
from . import base

PASSWORD_ENV_VAR = "POSTGRES_PASSWORD"
RESTORE_TMP_PATH = "/tmp/savepoint-restore.dump"


class PostgresAdapter:
    def discover(self, container):
        return "postgres" in docker_client.get_image_name(container)

    def default_connection_info(self, container):
        env = docker_client.parse_env(container)
        user = env.get("POSTGRES_USER") or "postgres"
        db_name = env.get("POSTGRES_DB") or user
        return {"db_user": user, "db_name": db_name}

    def backup(self, target_row, backup_target_dir: str) -> base.BackupResult:
        client = docker_client.get_client()
        container_name = target_row["container_name"]

        try:
            env = docker_client.get_container_env(client, container_name)
        except NotFound:
            return base.BackupResult(
                success=False,
                file_path=None,
                file_size_bytes=None,
                error_message=f"container '{container_name}' not found",
            )

        password = env.get(PASSWORD_ENV_VAR)
        if not password:
            return base.BackupResult(
                success=False,
                file_path=None,
                file_size_bytes=None,
                error_message=(
                    f"container '{container_name}' has no {PASSWORD_ENV_VAR} "
                    "environment variable, cannot authenticate"
                ),
            )

        dest_path = self._dest_path(backup_target_dir, target_row["name"])
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        exit_code, stderr_text = docker_client.exec_pg_dump(
            client,
            container_name,
            target_row["db_user"],
            target_row["db_name"],
            password,
            dest_path,
        )

        if exit_code != 0:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            return base.BackupResult(
                success=False,
                file_path=None,
                file_size_bytes=None,
                error_message=stderr_text.strip() or f"pg_dump exited with code {exit_code}",
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

        try:
            env = docker_client.get_container_env(client, container_name)
        except NotFound:
            return base.RestoreResult(success=False, error_message=f"container '{container_name}' not found")

        password = env.get(PASSWORD_ENV_VAR)
        if not password:
            return base.RestoreResult(
                success=False,
                error_message=(
                    f"container '{container_name}' has no {PASSWORD_ENV_VAR} "
                    "environment variable, cannot authenticate"
                ),
            )

        try:
            docker_client.put_archive_file(client, container_name, RESTORE_TMP_PATH, source_path)
        except NotFound:
            return base.RestoreResult(success=False, error_message=f"container '{container_name}' not found")

        try:
            cmd = [
                "pg_restore",
                "-U", target_row["db_user"],
                "-d", target_row["db_name"],
                "--clean", "--if-exists",
                RESTORE_TMP_PATH,
            ]
            exit_code, _stdout_text, stderr_text = docker_client.exec_simple(
                client, container_name, cmd, {"PGPASSWORD": password}
            )
        finally:
            self._cleanup_remote_file(client, container_name, RESTORE_TMP_PATH)

        if exit_code != 0:
            return base.RestoreResult(
                success=False,
                error_message=stderr_text.strip() or f"pg_restore exited with code {exit_code}",
            )

        return base.RestoreResult(success=True, error_message=None)

    @staticmethod
    def _cleanup_remote_file(client, container_name: str, remote_path: str) -> None:
        try:
            docker_client.exec_simple(client, container_name, ["rm", "-f", remote_path])
        except (APIError, NotFound):
            pass

    @staticmethod
    def _dest_path(backup_target_dir: str, target_name: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return os.path.join(backup_target_dir, target_name, f"{target_name}_{timestamp}.dump")
