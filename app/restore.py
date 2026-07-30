from __future__ import annotations

import logging

from docker.errors import NotFound

from . import db as db_module
from . import docker_client
from . import notifications
from .adapters import ADAPTERS
from .adapters.remote import remote_adapter_for

logger = logging.getLogger("savepoint.restore")

_settings = None


def init(settings) -> None:
    global _settings
    _settings = settings


def perform_restore(conn, target, restore_run_id: int, backup_run, stop_container: bool) -> None:
    """The one place that ever calls an adapter's restore(). Records the restore_runs
    outcome, orchestrates the optional stop/start (sqlite only, the route only ever
    passes stop_container=True for that engine), and fires ntfy on both success and
    failure, unlike backup's failure-only notifications: restore is rare, manual, and
    deliberate, the operator wants to know it's done either way without babysitting the
    tab for a multi-minute run.

    An agent-owned target has no local container to stop/start, the agent does
    stop-restore-start as one atomic remote call instead, so that case branches out here
    before any of the local orchestration below runs.
    """
    remote_adapter = remote_adapter_for(target, _settings)
    if remote_adapter is not None:
        result = remote_adapter.restore_with_lifecycle(target, backup_run["file_path"], stop_container)
        status = "success" if result.success else "failure"
        db_module.finish_restore_run(
            conn, restore_run_id, status, stopped_container=result.stopped_container, error_message=result.error_message
        )
        notifications.notify_restore_result(target, backup_run, result.success, result.error_message)
        return

    container_name = target["container_name"]
    client = docker_client.get_client()
    did_stop = False

    if stop_container:
        try:
            docker_client.stop_container(client, container_name)
            did_stop = True
        except NotFound:
            message = f"container '{container_name}' not found, could not stop it before restoring"
            db_module.finish_restore_run(
                conn, restore_run_id, "failure", stopped_container=False, error_message=message
            )
            notifications.notify_restore_result(target, backup_run, False, message)
            return

    adapter = ADAPTERS[target["engine"]]
    result = adapter.restore(target, backup_run["file_path"])

    start_error = None
    if did_stop:
        try:
            docker_client.start_container(client, container_name)
        except NotFound:
            start_error = f"container '{container_name}' not found, could not start it back up after restoring"

    if result.success:
        error_message = None
        if start_error:
            error_message = f"{start_error} (restore itself succeeded, start the container manually)"
        db_module.finish_restore_run(
            conn, restore_run_id, "success", stopped_container=did_stop, error_message=error_message
        )
        notifications.notify_restore_result(target, backup_run, True, error_message)
    else:
        db_module.finish_restore_run(
            conn, restore_run_id, "failure", stopped_container=did_stop, error_message=result.error_message
        )
        notifications.notify_restore_result(target, backup_run, False, result.error_message)
