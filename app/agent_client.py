from __future__ import annotations

import requests

from . import db as db_module

SHORT_TIMEOUT = 10
LONG_TIMEOUT = 600  # generous bound for a real dump/restore, never unbounded


class AgentError(Exception):
    pass


def _headers(agent) -> dict:
    return {"Authorization": f"Bearer {agent['token']}"}


def _base_url(agent) -> str:
    return agent["base_url"].rstrip("/")


def _record_contact(settings, agent_id: int, success: bool, error: str | None) -> None:
    """Opens its own short-lived connection, mirroring how notifications.py manages its
    own side effects independently of whatever connection a caller happens to hold.
    """
    conn = db_module.get_connection(settings.state_db_path)
    try:
        db_module.update_agent_contact(conn, agent_id, "ok" if success else "error", error)
    finally:
        conn.close()


def _extract_error(response) -> str:
    try:
        return response.json().get("error") or f"agent returned HTTP {response.status_code}"
    except ValueError:
        return f"agent returned HTTP {response.status_code}"


def health(agent, settings) -> bool:
    try:
        response = requests.get(f"{_base_url(agent)}/api/health", headers=_headers(agent), timeout=SHORT_TIMEOUT)
    except requests.RequestException as exc:
        _record_contact(settings, agent["id"], False, str(exc))
        return False

    if response.status_code != 200:
        _record_contact(settings, agent["id"], False, _extract_error(response))
        return False

    _record_contact(settings, agent["id"], True, None)
    return True


def discover(agent, settings) -> list:
    try:
        response = requests.get(f"{_base_url(agent)}/api/discover", headers=_headers(agent), timeout=SHORT_TIMEOUT)
    except requests.RequestException as exc:
        _record_contact(settings, agent["id"], False, str(exc))
        raise AgentError(f"could not reach agent '{agent['name']}': {exc}") from exc

    if response.status_code != 200:
        error = _extract_error(response)
        _record_contact(settings, agent["id"], False, error)
        raise AgentError(error)

    _record_contact(settings, agent["id"], True, None)
    return response.json()["candidates"]


def validate(agent, settings, engine, container_name, db_user, db_name, file_path) -> str | None:
    """Returns an error message, or None if valid, mirroring validate_connection_fields()'s
    own return convention so routes/targets.py can treat a local and a remote validation
    call identically.
    """
    payload = {
        "engine": engine,
        "container_name": container_name,
        "db_user": db_user,
        "db_name": db_name,
        "file_path": file_path,
    }
    try:
        response = requests.post(
            f"{_base_url(agent)}/api/validate", json=payload, headers=_headers(agent), timeout=SHORT_TIMEOUT
        )
    except requests.RequestException as exc:
        _record_contact(settings, agent["id"], False, str(exc))
        return f"could not reach agent '{agent['name']}': {exc}"

    if response.status_code != 200:
        error = _extract_error(response)
        _record_contact(settings, agent["id"], False, error)
        return error

    _record_contact(settings, agent["id"], True, None)
    return response.json().get("error")


def open_backup_stream(agent, settings, target_row):
    """Returns an open, streaming `requests.Response` for the agent's /api/backup call,
    already validated as HTTP 200 (raises AgentError otherwise). The caller
    (RemoteAdapter.backup()) owns consuming/closing the stream and all local file
    handling, this function's job is strictly the HTTP round trip and contact bookkeeping.
    """
    payload = {
        "engine": target_row["engine"],
        "container_name": target_row["container_name"],
        "db_user": target_row["db_user"],
        "db_name": target_row["db_name"],
        "file_path": target_row["file_path"],
    }
    try:
        response = requests.post(
            f"{_base_url(agent)}/api/backup",
            json=payload,
            headers=_headers(agent),
            timeout=LONG_TIMEOUT,
            stream=True,
        )
    except requests.RequestException as exc:
        _record_contact(settings, agent["id"], False, str(exc))
        raise AgentError(f"could not reach agent '{agent['name']}': {exc}") from exc

    if response.status_code != 200:
        error = _extract_error(response)
        response.close()
        # We did reach the agent and got a coherent response, it just reported a
        # backup failure (e.g. bad credentials on its side), that's a successful
        # contact, not an unreachable agent.
        _record_contact(settings, agent["id"], True, None)
        raise AgentError(error)

    _record_contact(settings, agent["id"], True, None)
    return response


def run_restore(agent, settings, target_row, source_path: str, stop_container: bool) -> dict:
    """POSTs the backup file as a multipart upload to /api/restore. The agent always
    responds with {"success": bool, "stopped_container": bool, "error": str|None} in the
    body regardless of HTTP status, so this parses that body uniformly rather than
    treating a non-2xx as a transport failure, only a genuine RequestException (timeout,
    connection refused, DNS failure) is a transport failure here, raised for the caller
    (RemoteAdapter.restore_with_lifecycle()) to catch.
    """
    data = {
        "engine": target_row["engine"],
        "container_name": target_row["container_name"],
        "db_user": target_row["db_user"] or "",
        "db_name": target_row["db_name"] or "",
        "file_path": target_row["file_path"] or "",
        "stop_container": "true" if stop_container else "false",
    }
    try:
        with open(source_path, "rb") as f:
            response = requests.post(
                f"{_base_url(agent)}/api/restore",
                data=data,
                files={"file": f},
                headers=_headers(agent),
                timeout=LONG_TIMEOUT,
            )
    except requests.RequestException as exc:
        _record_contact(settings, agent["id"], False, str(exc))
        raise AgentError(f"could not reach agent '{agent['name']}': {exc}") from exc

    _record_contact(settings, agent["id"], True, None)
    return response.json()
