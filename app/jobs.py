from __future__ import annotations

import logging
import threading

from . import db as db_module
from . import notifications
from . import restore as restore_module
from . import retention
from .adapters import ADAPTERS
from .adapters.remote import remote_adapter_for

logger = logging.getLogger("savepoint.jobs")

COLLISION_MESSAGE = "target already has a run in progress"

_lock = threading.Lock()
_in_progress: set = set()
_settings = None


def init(settings) -> None:
    global _settings
    _settings = settings


def try_claim(target_id: int) -> bool:
    """Atomically claim a target for a run. Returns False if it's already claimed."""
    with _lock:
        if target_id in _in_progress:
            return False
        _in_progress.add(target_id)
        return True


def release(target_id: int) -> None:
    with _lock:
        _in_progress.discard(target_id)


def is_in_progress(target_id: int) -> bool:
    """Read-only peek, unlike try_claim() this never mutates _in_progress."""
    with _lock:
        return target_id in _in_progress


def run_backup(target_id: int, triggered_by: str, run_id: int | None = None) -> None:
    """Entry point for schedule- and window-triggered runs.

    Claims the target itself. On collision, records a "skipped" row: a fresh one for
    schedule-triggered calls, or the pre-created "queued" row for window-triggered calls
    (window_tick() creates one row per member up front). Manual runs go through
    try_claim()/execute_claimed() instead, since the "already running" case needs to be
    known synchronously in the request handler, before anything reaches the scheduler.
    """
    if not try_claim(target_id):
        _record_collision(target_id, triggered_by, run_id)
        return

    try:
        conn = db_module.get_connection(_settings.state_db_path)
        try:
            target = db_module.get_target(conn, target_id)
            if target is None:
                if run_id is not None:
                    db_module.finish_backup_run(conn, run_id, "failure", error_message="target no longer exists")
                return

            if run_id is None:
                run_id = db_module.create_backup_run(conn, target_id, status="running", triggered_by=triggered_by)
            else:
                db_module.update_backup_run_status(conn, run_id, "running")

            _execute(conn, target, run_id)
        finally:
            conn.close()
    finally:
        release(target_id)


def execute_claimed(target_id: int, run_id: int) -> None:
    """For manual runs only: the route already claimed the target and created the
    "running" row synchronously before dispatching this. Just runs the adapter, finishes
    the row, and releases the claim.
    """
    conn = db_module.get_connection(_settings.state_db_path)
    try:
        target = db_module.get_target(conn, target_id)
        if target is None:
            db_module.finish_backup_run(conn, run_id, "failure", error_message="target no longer exists")
            return
        _execute(conn, target, run_id)
    finally:
        conn.close()
        release(target_id)


def execute_restore_claimed(target_id: int, restore_run_id: int, backup_run_id: int, stop_container: bool) -> None:
    """For restores only: the route already claimed the target via try_claim() and
    created the "running" restore_runs row synchronously before dispatching this,
    mirroring execute_claimed()'s manual-backup pattern. Restore shares the exact same
    per-target lock backups use, not a separate one, since the hazard is any operation
    touching the same database at once, not "two backups specifically".
    """
    conn = db_module.get_connection(_settings.state_db_path)
    try:
        target = db_module.get_target(conn, target_id)
        if target is None:
            db_module.finish_restore_run(
                conn, restore_run_id, "failure", stopped_container=False, error_message="target no longer exists"
            )
            return

        backup_run = db_module.get_backup_run(conn, backup_run_id)
        if backup_run is None:
            db_module.finish_restore_run(
                conn, restore_run_id, "failure", stopped_container=False, error_message="backup run no longer exists"
            )
            return

        restore_module.perform_restore(conn, target, restore_run_id, backup_run, stop_container)
    finally:
        conn.close()
        release(target_id)


def _record_collision(target_id: int, triggered_by: str, run_id: int | None) -> None:
    conn = db_module.get_connection(_settings.state_db_path)
    try:
        if run_id is not None:
            db_module.finish_backup_run(conn, run_id, "skipped", error_message=COLLISION_MESSAGE)
        else:
            new_run_id = db_module.create_backup_run(conn, target_id, status="skipped", triggered_by=triggered_by)
            db_module.finish_backup_run(conn, new_run_id, "skipped", error_message=COLLISION_MESSAGE)
    finally:
        conn.close()


def _execute(conn, target, run_id: int) -> None:
    """The one place that ever calls an adapter's backup(). An agent-owned target
    (target["agent_id"] set) resolves to a RemoteAdapter instead of a local ADAPTERS[engine]
    entry, everything after this lookup is identical for local and remote targets.
    """
    adapter = remote_adapter_for(target, _settings) or ADAPTERS[target["engine"]]
    result = adapter.backup(target, _settings.backup_target_dir)

    if result.success:
        db_module.finish_backup_run(
            conn,
            run_id,
            "success",
            file_path=result.file_path,
            file_size_bytes=result.file_size_bytes,
            method=result.method,
        )
        retention.tag_and_prune(conn, target, run_id, _settings.backup_target_dir)
    else:
        db_module.finish_backup_run(conn, run_id, "failure", error_message=result.error_message)
        notifications.notify_failure(target, result)
