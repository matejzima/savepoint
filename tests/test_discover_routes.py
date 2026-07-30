from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as db_module
from app.deps import get_db_conn
from app.routes import discover as discover_routes


def _make_client(state_db_path):
    app = FastAPI()
    app.include_router(discover_routes.router)
    app.state.settings = SimpleNamespace(state_db_path=state_db_path)

    def override_get_db_conn():
        conn = db_module.get_connection(state_db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db_conn] = override_get_db_conn
    return TestClient(app)


def test_local_discover_filters_out_already_added_containers(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    db_module.create_target(conn, "existing", "postgres", "existing-c", "u", "d")

    client = _make_client(state_db)
    with patch("app.routes.discover.docker_client") as mock_docker_client, patch(
        "app.routes.discover.discovery"
    ) as mock_discovery:
        mock_docker_client.get_client.return_value = MagicMock()
        mock_discovery.find_candidates.return_value = [
            {"engine": "postgres", "container_name": "existing-c", "image": "postgres", "db_user": "u", "db_name": "d"},
            {"engine": "postgres", "container_name": "new-c", "image": "postgres", "db_user": "u", "db_name": "d"},
        ]
        r = client.get("/discover")

    assert r.status_code == 200
    assert "new-c" in r.text
    assert "existing-c" not in r.text


def test_agent_discover_filters_only_that_agents_existing_targets(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    db_module.create_target(conn, "remote-existing", "postgres", "remote-c", "u", "d", agent_id=agent_id)

    client = _make_client(state_db)
    with patch("app.routes.discover.agent_client") as mock_agent_client:
        mock_agent_client.discover.return_value = [
            {"engine": "postgres", "container_name": "remote-c", "image": "postgres", "db_user": "u", "db_name": "d"},
            {"engine": "postgres", "container_name": "remote-new", "image": "postgres", "db_user": "u", "db_name": "d"},
        ]
        r = client.get(f"/discover?agent_id={agent_id}")

    assert r.status_code == 200
    assert "remote-new" in r.text
    assert "remote-existing" not in r.text


def test_agent_discover_shows_error_when_agent_unreachable(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")

    client = _make_client(state_db)
    with patch("app.routes.discover.agent_client") as mock_agent_client:
        from app.agent_client import AgentError

        mock_agent_client.AgentError = AgentError
        mock_agent_client.discover.side_effect = AgentError("could not reach agent 'remote-host': timeout")
        r = client.get(f"/discover?agent_id={agent_id}")

    assert r.status_code == 200
    assert "could not reach" in r.text


def test_discover_with_empty_agent_id_falls_back_to_local(tmp_path):
    """The agent selector's "Local" option submits agent_id="" (empty string, not an
    absent param), switching back to Local after having selected an agent must not 422.
    """
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)

    with patch("app.routes.discover.docker_client") as mock_docker_client, patch(
        "app.routes.discover.discovery"
    ) as mock_discovery:
        mock_docker_client.get_client.return_value = MagicMock()
        mock_discovery.find_candidates.return_value = [
            {"engine": "postgres", "container_name": "local-c", "image": "postgres", "db_user": "u", "db_name": "d"}
        ]
        r = client.get("/discover?agent_id=")

    assert r.status_code == 200
    assert "local-c" in r.text


def test_discover_unknown_agent_404s(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    client = _make_client(state_db)

    r = client.get("/discover?agent_id=999")
    assert r.status_code == 404
