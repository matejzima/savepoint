from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    mode: str
    backup_target_dir: str
    state_db_path: str
    host: str
    port: int
    ntfy_url: str | None
    ntfy_topic: str | None
    ntfy_token: str | None
    agent_token: str | None
    forward_auth_header: str | None


def load_settings() -> Settings:
    return Settings(
        mode=os.environ.get("MODE", "master"),
        backup_target_dir=os.environ.get("BACKUP_TARGET_DIR", "/backup-target"),
        state_db_path=os.environ.get("STATE_DB_PATH", "/data/savepoint.db"),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        ntfy_url=os.environ.get("NTFY_URL") or None,
        ntfy_topic=os.environ.get("NTFY_TOPIC") or None,
        ntfy_token=os.environ.get("NTFY_TOKEN") or None,
        agent_token=os.environ.get("AGENT_TOKEN") or None,
        forward_auth_header=os.environ.get("FORWARD_AUTH_HEADER") or None,
    )
