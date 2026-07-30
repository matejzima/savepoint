import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters import base
from app.routes import agent_api


def _make_client(agent_token="secret"):
    app = FastAPI()
    app.include_router(agent_api.router)
    app.state.settings = SimpleNamespace(agent_token=agent_token)
    return TestClient(app)


def _auth(token="secret"):
    return {"Authorization": f"Bearer {token}"}


def test_sweep_stale_staging_dirs_removes_only_matching_stale_directories(tmp_path):
    backup_leftover = tmp_path / f"{agent_api.STAGING_PREFIX}abc123"
    backup_leftover.mkdir()
    (backup_leftover / "partial.dump").write_bytes(b"leftover")

    restore_leftover = tmp_path / f"{agent_api.STAGING_PREFIX}restore-xyz789"
    restore_leftover.mkdir()

    unrelated_dir = tmp_path / "some-other-app-tmp"
    unrelated_dir.mkdir()

    unrelated_file = tmp_path / f"{agent_api.STAGING_PREFIX}not-a-dir"
    unrelated_file.write_text("not a directory, must be skipped, not raise")

    with patch("app.routes.agent_api.tempfile.gettempdir", return_value=str(tmp_path)):
        agent_api.sweep_stale_staging_dirs()

    assert not backup_leftover.exists()
    assert not restore_leftover.exists()
    assert unrelated_dir.exists()
    assert unrelated_file.exists()


def test_sweep_stale_staging_dirs_is_a_noop_when_nothing_stale(tmp_path):
    with patch("app.routes.agent_api.tempfile.gettempdir", return_value=str(tmp_path)):
        agent_api.sweep_stale_staging_dirs()  # must not raise on an empty/clean temp dir


def test_health_requires_correct_token():
    """Also covers require_agent_token()'s constant-time comparison (hmac.compare_digest
    instead of !=): a same-length wrong token and a different-length wrong token must
    both still be rejected, no behavior change from the caller's perspective versus a
    plain string comparison.
    """
    client = _make_client()

    assert client.get("/api/health").status_code == 401  # missing header
    assert client.get("/api/health", headers=_auth("wrong")).status_code == 401  # different length
    assert client.get("/api/health", headers=_auth("secre1")).status_code == 401  # same length as "secret"

    r = client.get("/api/health", headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_discover_route_returns_candidates():
    client = _make_client()
    with patch("app.routes.agent_api.docker_client") as mock_docker_client, patch(
        "app.routes.agent_api.discovery"
    ) as mock_discovery:
        mock_docker_client.get_client.return_value = MagicMock()
        mock_discovery.find_candidates.return_value = [{"engine": "postgres", "container_name": "c"}]
        r = client.get("/api/discover", headers=_auth())

    assert r.status_code == 200
    assert r.json() == {"candidates": [{"engine": "postgres", "container_name": "c"}]}


def test_validate_route_delegates_to_validation_module():
    client = _make_client()
    with patch("app.routes.agent_api.docker_client") as mock_docker_client, patch(
        "app.routes.agent_api.validation"
    ) as mock_validation:
        mock_docker_client.get_client.return_value = MagicMock()
        mock_validation.validate_connection_fields.return_value = "container not found"
        r = client.post(
            "/api/validate",
            json={"engine": "postgres", "container_name": "c", "db_user": "u", "db_name": "d", "file_path": None},
            headers=_auth(),
        )

    assert r.status_code == 200
    assert r.json() == {"error": "container not found"}


def test_backup_route_streams_file_and_cleans_up_staging_dir():
    client = _make_client()
    captured = {}

    def fake_backup(target_row, backup_target_dir):
        path = os.path.join(backup_target_dir, "out.dump")
        with open(path, "wb") as f:
            f.write(b"dump-bytes")
        captured["staging_dir"] = backup_target_dir
        return base.BackupResult(success=True, file_path=path, file_size_bytes=10, error_message=None, method="live")

    fake_adapter = MagicMock()
    fake_adapter.backup.side_effect = fake_backup

    with patch("app.routes.agent_api.ADAPTERS", {"postgres": fake_adapter}):
        r = client.post(
            "/api/backup",
            json={"engine": "postgres", "container_name": "c", "db_user": "u", "db_name": "d", "file_path": None},
            headers=_auth(),
        )

    assert r.status_code == 200
    assert r.content == b"dump-bytes"
    assert r.headers["x-savepoint-filename"] == "out.dump"
    assert r.headers["x-savepoint-method"] == "live"
    assert not os.path.exists(captured["staging_dir"])


def test_backup_route_failure_returns_json_error_and_cleans_up_staging_dir():
    client = _make_client()
    captured = {}

    def fake_backup(target_row, backup_target_dir):
        captured["staging_dir"] = backup_target_dir
        return base.BackupResult(success=False, file_path=None, file_size_bytes=None, error_message="boom")

    fake_adapter = MagicMock()
    fake_adapter.backup.side_effect = fake_backup

    with patch("app.routes.agent_api.ADAPTERS", {"postgres": fake_adapter}):
        r = client.post(
            "/api/backup",
            json={"engine": "postgres", "container_name": "c", "db_user": "u", "db_name": "d", "file_path": None},
            headers=_auth(),
        )

    assert r.status_code == 500
    assert r.json() == {"error": "boom"}
    assert not os.path.exists(captured["staging_dir"])


def test_backup_route_rejects_unknown_engine():
    client = _make_client()
    r = client.post(
        "/api/backup",
        json={"engine": "oracle", "container_name": "c", "db_user": "u", "db_name": "d", "file_path": None},
        headers=_auth(),
    )
    assert r.status_code == 400


def test_restore_route_success_without_stop():
    client = _make_client()
    fake_adapter = MagicMock()
    fake_adapter.restore.return_value = base.RestoreResult(success=True, error_message=None)

    with patch("app.routes.agent_api.ADAPTERS", {"postgres": fake_adapter}):
        r = client.post(
            "/api/restore",
            data={
                "engine": "postgres",
                "container_name": "c",
                "db_user": "u",
                "db_name": "d",
                "file_path": "",
                "stop_container": "false",
            },
            files={"file": ("backup.dump", b"dump-data")},
            headers=_auth(),
        )

    assert r.status_code == 200
    assert r.json() == {"success": True, "stopped_container": False, "error": None}
    fake_adapter.restore.assert_called_once()


def test_restore_route_stops_and_starts_container_when_requested():
    client = _make_client()
    fake_adapter = MagicMock()
    fake_adapter.restore.return_value = base.RestoreResult(success=True, error_message=None)

    with patch("app.routes.agent_api.ADAPTERS", {"sqlite": fake_adapter}), patch(
        "app.routes.agent_api.docker_client"
    ) as mock_docker_client:
        mock_docker_client.get_client.return_value = MagicMock()
        r = client.post(
            "/api/restore",
            data={
                "engine": "sqlite",
                "container_name": "c",
                "db_user": "",
                "db_name": "",
                "file_path": "/data/app.db",
                "stop_container": "true",
            },
            files={"file": ("backup.db", b"data")},
            headers=_auth(),
        )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["stopped_container"] is True
    mock_docker_client.stop_container.assert_called_once()
    mock_docker_client.start_container.assert_called_once()


def test_restore_route_stop_failure_skips_restore_entirely():
    client = _make_client()
    fake_adapter = MagicMock()

    with patch("app.routes.agent_api.ADAPTERS", {"sqlite": fake_adapter}), patch(
        "app.routes.agent_api.docker_client"
    ) as mock_docker_client:
        mock_docker_client.get_client.return_value = MagicMock()
        mock_docker_client.stop_container.side_effect = Exception("cannot stop")
        r = client.post(
            "/api/restore",
            data={
                "engine": "sqlite",
                "container_name": "c",
                "db_user": "",
                "db_name": "",
                "file_path": "/data/app.db",
                "stop_container": "true",
            },
            files={"file": ("backup.db", b"data")},
            headers=_auth(),
        )

    body = r.json()
    assert body["success"] is False
    assert body["stopped_container"] is False
    fake_adapter.restore.assert_not_called()


def test_restore_route_start_failure_after_success_notes_manual_start_needed():
    client = _make_client()
    fake_adapter = MagicMock()
    fake_adapter.restore.return_value = base.RestoreResult(success=True, error_message=None)

    with patch("app.routes.agent_api.ADAPTERS", {"sqlite": fake_adapter}), patch(
        "app.routes.agent_api.docker_client"
    ) as mock_docker_client:
        mock_docker_client.get_client.return_value = MagicMock()
        mock_docker_client.start_container.side_effect = Exception("cannot start")
        r = client.post(
            "/api/restore",
            data={
                "engine": "sqlite",
                "container_name": "c",
                "db_user": "",
                "db_name": "",
                "file_path": "/data/app.db",
                "stop_container": "true",
            },
            files={"file": ("backup.db", b"data")},
            headers=_auth(),
        )

    body = r.json()
    assert body["success"] is True
    assert body["stopped_container"] is True
    assert "start the container manually" in body["error"]


def test_restore_route_rejects_unknown_engine():
    client = _make_client()
    r = client.post(
        "/api/restore",
        data={
            "engine": "oracle",
            "container_name": "c",
            "db_user": "",
            "db_name": "",
            "file_path": "",
            "stop_container": "false",
        },
        files={"file": ("backup", b"data")},
        headers=_auth(),
    )
    assert r.status_code == 400
    assert r.json()["success"] is False
