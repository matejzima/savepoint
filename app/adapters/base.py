from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class BackupResult:
    success: bool
    file_path: str | None
    file_size_bytes: int | None
    error_message: str | None
    method: str | None = None


@dataclass
class RestoreResult:
    success: bool
    error_message: str | None
    stopped_container: bool = False


class Adapter(Protocol):
    def discover(self, container: Any) -> bool: ...

    def default_connection_info(self, container: Any) -> dict: ...

    def backup(self, target_row: Any, backup_target_dir: str) -> BackupResult: ...

    def restore(self, target_row: Any, source_path: str) -> RestoreResult: ...
