import os
from datetime import datetime, timezone

from docker.errors import APIError, NotFound

from .. import docker_client
from . import base

BACKUP_TMP_PATH = "/tmp/savepoint-sqlite-backup"


class SQLiteAdapter:
    def discover(self, container):
        return False

    def default_connection_info(self, container):
        return {}

    def backup(self, target_row, backup_target_dir: str) -> base.BackupResult:
        client = docker_client.get_client()
        container_name = target_row["container_name"]
        source_path = target_row["file_path"]

        dest_path = self._dest_path(backup_target_dir, target_row["name"], source_path)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        try:
            live_ok = self._try_live_backup(client, container_name, source_path, dest_path)
        except NotFound:
            return base.BackupResult(
                success=False,
                file_path=None,
                file_size_bytes=None,
                error_message=f"container '{container_name}' not found",
            )

        if live_ok:
            return base.BackupResult(
                success=True,
                file_path=dest_path,
                file_size_bytes=os.path.getsize(dest_path),
                error_message=None,
                method="live",
            )

        try:
            docker_client.get_archive_file(client, container_name, source_path, dest_path)
        except NotFound:
            return base.BackupResult(
                success=False,
                file_path=None,
                file_size_bytes=None,
                error_message=f"file '{source_path}' not found in container '{container_name}'",
            )

        return base.BackupResult(
            success=True,
            file_path=dest_path,
            file_size_bytes=os.path.getsize(dest_path),
            error_message=None,
            method="raw-copy",
        )

    def restore(self, target_row, source_path: str) -> base.RestoreResult:
        client = docker_client.get_client()
        container_name = target_row["container_name"]
        dest_path = target_row["file_path"]

        try:
            docker_client.put_archive_file(client, container_name, dest_path, source_path)
        except NotFound:
            return base.RestoreResult(success=False, error_message=f"container '{container_name}' not found")

        return base.RestoreResult(success=True, error_message=None)

    @staticmethod
    def _try_live_backup(client, container_name: str, source_path: str, dest_path: str) -> bool:
        try:
            exit_code, _stdout, _stderr = docker_client.exec_simple(
                client, container_name, ["sqlite3", source_path, f".backup {BACKUP_TMP_PATH}"]
            )
        except NotFound:
            raise
        except APIError:
            return False

        if exit_code != 0:
            return False

        try:
            docker_client.get_archive_file(client, container_name, BACKUP_TMP_PATH, dest_path)
        except NotFound:
            return False

        try:
            docker_client.exec_simple(client, container_name, ["rm", "-f", BACKUP_TMP_PATH])
        except (APIError, NotFound):
            pass

        return True

    @staticmethod
    def _dest_path(backup_target_dir: str, target_name: str, source_path: str) -> str:
        ext = os.path.splitext(source_path)[1] or ".sqlite3"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return os.path.join(backup_target_dir, target_name, f"{target_name}_{timestamp}{ext}")
