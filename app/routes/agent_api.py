import hmac
import os
import shutil
import tempfile
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from .. import discovery
from .. import docker_client
from .. import validation
from ..adapters import ADAPTERS

router = APIRouter()

STAGING_PREFIX = "savepoint-agent-"


def sweep_stale_staging_dirs() -> None:
    """Removes any staging directories left behind by a hard crash (SIGKILL) mid-transfer.
    Normal cleanup (FileResponse's background task for /api/backup, the `finally` in
    /api/restore) never runs in that case, there's no way to clean up after being
    forcibly killed. Called once at agent startup, before serving any requests, so a
    crash mid-transfer gets swept up on the next start rather than accumulating
    indefinitely across repeated crashes.
    """
    tmp_root = tempfile.gettempdir()
    for name in os.listdir(tmp_root):
        if name.startswith(STAGING_PREFIX):
            path = os.path.join(tmp_root, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)


def require_agent_token(request: Request, authorization: Optional[str] = Header(None)) -> None:
    # hmac.compare_digest() instead of != : a plain string comparison is not
    # constant-time and is theoretically vulnerable to a timing attack. compare_digest
    # handles differing lengths correctly on its own (returns False without leaking
    # length via timing), no separate length check needed first.
    expected = f"Bearer {request.app.state.settings.agent_token}"
    provided = authorization or ""
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid or missing token")


@router.get("/api/health")
def health(_: None = Depends(require_agent_token)):
    return {"status": "ok"}


@router.get("/api/discover")
def discover_route(_: None = Depends(require_agent_token)):
    client = docker_client.get_client()
    return {"candidates": discovery.find_candidates(client)}


@router.post("/api/validate")
def validate_route(payload: dict, _: None = Depends(require_agent_token)):
    client = docker_client.get_client()
    error = validation.validate_connection_fields(
        client,
        payload.get("engine"),
        payload.get("container_name"),
        payload.get("db_user"),
        payload.get("db_name"),
        payload.get("file_path"),
    )
    return {"error": error}


@router.post("/api/backup")
def backup_route(payload: dict, _: None = Depends(require_agent_token)):
    """Runs the matching local adapter's backup() against a small local temp directory,
    then streams the resulting file straight back as the response body (X-Savepoint-Filename
    carries the basename to use, X-Savepoint-Method carries the SQLite live-vs-raw-copy
    distinction). The staging directory is removed once the response has been fully sent
    (FileResponse's background task), or immediately on a failed backup.
    """
    engine = payload.get("engine")
    if engine not in ADAPTERS:
        return JSONResponse({"error": f"unknown engine '{engine}'"}, status_code=400)

    staging_dir = tempfile.mkdtemp(prefix=STAGING_PREFIX)
    target_row = {
        "name": payload.get("container_name"),
        "container_name": payload.get("container_name"),
        "db_user": payload.get("db_user"),
        "db_name": payload.get("db_name"),
        "file_path": payload.get("file_path"),
    }

    result = ADAPTERS[engine].backup(target_row, staging_dir)

    if not result.success:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return JSONResponse({"error": result.error_message}, status_code=500)

    headers = {"X-Savepoint-Filename": os.path.basename(result.file_path)}
    if result.method:
        headers["X-Savepoint-Method"] = result.method

    return FileResponse(
        result.file_path,
        headers=headers,
        background=BackgroundTask(shutil.rmtree, staging_dir, ignore_errors=True),
    )


@router.post("/api/restore")
def restore_route(
    file: UploadFile = File(...),
    engine: str = Form(...),
    container_name: str = Form(...),
    db_user: str = Form(""),
    db_name: str = Form(""),
    file_path: str = Form(""),
    stop_container: str = Form("false"),
    _: None = Depends(require_agent_token),
):
    """Stages the uploaded backup file locally, runs the matching adapter's restore(),
    optionally stopping/starting the container around it, and always removes its own
    staged files afterward. Always returns the {success, stopped_container, error} shape,
    the HTTP status code is secondary, agent_client.run_restore() parses the body either
    way.
    """
    if engine not in ADAPTERS:
        return JSONResponse(
            {"success": False, "stopped_container": False, "error": f"unknown engine '{engine}'"},
            status_code=400,
        )

    staging_dir = tempfile.mkdtemp(prefix=f"{STAGING_PREFIX}restore-")
    try:
        source_path = os.path.join(staging_dir, "restore-source")
        with open(source_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        target_row = {
            "container_name": container_name,
            "db_user": db_user,
            "db_name": db_name,
            "file_path": file_path or None,
        }

        did_stop = False
        if stop_container.lower() == "true":
            try:
                docker_client.stop_container(docker_client.get_client(), container_name)
                did_stop = True
            except Exception as exc:
                return {"success": False, "stopped_container": False, "error": f"could not stop container: {exc}"}

        try:
            result = ADAPTERS[engine].restore(target_row, source_path)
        except Exception as exc:
            result = None
            error_message = str(exc)
            success = False
        else:
            error_message = result.error_message
            success = result.success

        start_error = None
        if did_stop:
            try:
                docker_client.start_container(docker_client.get_client(), container_name)
            except Exception as exc:
                start_error = str(exc)

        if start_error:
            if success:
                error_message = f"{start_error} (restore itself succeeded, start the container manually)"
            else:
                error_message = f"{error_message}; also failed to start container back up: {start_error}"

        return {"success": success, "stopped_container": did_stop, "error": error_message}
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
