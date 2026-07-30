from __future__ import annotations

import os

from .. import agent_client
from .. import db as db_module
from . import base


class RemoteAdapter:
    """Implements the same backup()/restore() shape every local adapter does, but runs
    the actual dump/restore on a registered agent's host over HTTP instead of a local
    docker exec. Parameterized by a specific `agents` row, not a singleton in the
    ADAPTERS dict the way the per-engine adapters are, since it needs to know which
    agent. See remote_adapter_for() below for how call sites obtain one.
    """

    def __init__(self, agent, settings):
        self.agent = agent
        self._settings = settings

    def backup(self, target_row, backup_target_dir: str) -> base.BackupResult:
        """Streams the agent's /api/backup response to a temp path first, only renaming
        it into the real destination once the stream has fully and successfully
        completed. A connection drop or any error partway through (including a local
        write failure, e.g. disk full) removes the partial temp file instead of leaving
        a truncated file sitting at the path retention/restore would later treat as a
        complete, valid backup. Every failure mode, connection error, timeout, non-2xx
        response, or an unexpected exception during the write, is caught here and
        converted into a normal BackupResult, never left to propagate uncaught (the
        exact class of bug Phase 5's start_container() had, where only NotFound was
        caught and anything else silently killed the job with no notification).
        """
        target_dir = os.path.join(backup_target_dir, target_row["name"])
        os.makedirs(target_dir, exist_ok=True)
        temp_path = os.path.join(target_dir, f".{target_row['name']}.part")

        try:
            response = agent_client.open_backup_stream(self.agent, self._settings, target_row)
            try:
                filename = response.headers.get("X-Savepoint-Filename")
                if not filename:
                    raise agent_client.AgentError("agent response missing X-Savepoint-Filename header")
                method = response.headers.get("X-Savepoint-Method") or None

                with open(temp_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
            finally:
                response.close()

            dest_path = os.path.join(target_dir, filename)
            os.replace(temp_path, dest_path)
        except Exception as exc:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return base.BackupResult(
                success=False,
                file_path=None,
                file_size_bytes=None,
                error_message=f"agent '{self.agent['name']}': {exc}",
            )

        return base.BackupResult(
            success=True,
            file_path=dest_path,
            file_size_bytes=os.path.getsize(dest_path),
            error_message=None,
            method=method,
        )

    def restore_with_lifecycle(self, target_row, source_path: str, stop_container: bool) -> base.RestoreResult:
        """Used only by restore.py::perform_restore()'s agent branch, not part of the
        plain Adapter protocol: unlike a local restore (where perform_restore() stops and
        starts the container itself around calling adapter.restore()), a remote target's
        container lives on the agent's host, so the agent has to do stop-restore-start as
        one atomic request. Any transport-level failure is caught broadly here too, same
        reasoning as backup() above.
        """
        try:
            result = agent_client.run_restore(self.agent, self._settings, target_row, source_path, stop_container)
        except Exception as exc:
            return base.RestoreResult(
                success=False,
                error_message=f"agent '{self.agent['name']}': {exc}",
                stopped_container=False,
            )

        return base.RestoreResult(
            success=bool(result.get("success", False)),
            error_message=result.get("error"),
            stopped_container=bool(result.get("stopped_container", False)),
        )


def remote_adapter_for(target_row, settings) -> RemoteAdapter | None:
    """Returns a RemoteAdapter for an agent-owned target, or None for a local one
    (agent_id is NULL). The one factory jobs.py/restore.py use instead of the plain
    ADAPTERS[engine] lookup, so everything downstream of that one call stays identical
    for local and remote targets alike.
    """
    if not target_row["agent_id"]:
        return None

    conn = db_module.get_connection(settings.state_db_path)
    try:
        agent = db_module.get_agent(conn, target_row["agent_id"])
    finally:
        conn.close()

    if agent is None:
        return None

    return RemoteAdapter(agent, settings)
