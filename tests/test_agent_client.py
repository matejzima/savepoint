from unittest.mock import MagicMock, patch

import pytest
import requests

from app import agent_client
from app import db as db_module


class FakeSettings:
    def __init__(self, state_db_path):
        self.state_db_path = state_db_path


def _agent(agent_id):
    return {"id": agent_id, "name": "remote-host", "base_url": "http://remote-host:8000", "token": "secret"}


def _make_agent(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "secret")
    return state_db, agent_id


def test_health_success_records_contact_ok(tmp_path):
    state_db, agent_id = _make_agent(tmp_path)
    settings = FakeSettings(state_db)
    agent = _agent(agent_id)

    mock_response = MagicMock(status_code=200)
    with patch("app.agent_client.requests.get", return_value=mock_response) as mock_get:
        assert agent_client.health(agent, settings) is True
        assert mock_get.call_args.kwargs["timeout"] == agent_client.SHORT_TIMEOUT
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"

    conn = db_module.get_connection(state_db)
    assert db_module.get_agent(conn, agent_id)["last_contact_status"] == "ok"


def test_health_connection_error_records_failure(tmp_path):
    state_db, agent_id = _make_agent(tmp_path)
    settings = FakeSettings(state_db)
    agent = _agent(agent_id)

    with patch("app.agent_client.requests.get", side_effect=requests.ConnectionError("refused")):
        assert agent_client.health(agent, settings) is False

    conn = db_module.get_connection(state_db)
    row = db_module.get_agent(conn, agent_id)
    assert row["last_contact_status"] == "error"
    assert "refused" in row["last_contact_error"]


def test_discover_returns_candidates_with_short_timeout(tmp_path):
    state_db, agent_id = _make_agent(tmp_path)
    settings = FakeSettings(state_db)
    agent = _agent(agent_id)

    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"candidates": [{"engine": "postgres", "container_name": "c"}]}
    with patch("app.agent_client.requests.get", return_value=mock_response) as mock_get:
        candidates = agent_client.discover(agent, settings)
        assert candidates == [{"engine": "postgres", "container_name": "c"}]
        assert mock_get.call_args.kwargs["timeout"] == agent_client.SHORT_TIMEOUT


def test_discover_raises_agent_error_on_connection_failure(tmp_path):
    state_db, agent_id = _make_agent(tmp_path)
    settings = FakeSettings(state_db)
    agent = _agent(agent_id)

    with patch("app.agent_client.requests.get", side_effect=requests.Timeout("slow")):
        with pytest.raises(agent_client.AgentError):
            agent_client.discover(agent, settings)

    conn = db_module.get_connection(state_db)
    assert db_module.get_agent(conn, agent_id)["last_contact_status"] == "error"


def test_validate_returns_error_string_from_agent_response(tmp_path):
    state_db, agent_id = _make_agent(tmp_path)
    settings = FakeSettings(state_db)
    agent = _agent(agent_id)

    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"error": "container 'x' not found"}
    with patch("app.agent_client.requests.post", return_value=mock_response) as mock_post:
        error = agent_client.validate(agent, settings, "postgres", "x", "u", "d", None)
        assert error == "container 'x' not found"
        assert mock_post.call_args.kwargs["timeout"] == agent_client.SHORT_TIMEOUT


def test_validate_returns_none_when_valid(tmp_path):
    state_db, agent_id = _make_agent(tmp_path)
    settings = FakeSettings(state_db)
    agent = _agent(agent_id)

    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"error": None}
    with patch("app.agent_client.requests.post", return_value=mock_response):
        assert agent_client.validate(agent, settings, "postgres", "x", "u", "d", None) is None


def test_validate_returns_message_on_connection_failure(tmp_path):
    state_db, agent_id = _make_agent(tmp_path)
    settings = FakeSettings(state_db)
    agent = _agent(agent_id)

    with patch("app.agent_client.requests.post", side_effect=requests.ConnectionError("down")):
        error = agent_client.validate(agent, settings, "postgres", "x", "u", "d", None)
        assert "down" in error


def test_open_backup_stream_returns_response_with_long_timeout_when_reachable(tmp_path):
    state_db, agent_id = _make_agent(tmp_path)
    settings = FakeSettings(state_db)
    agent = _agent(agent_id)
    target_row = {"engine": "postgres", "container_name": "c", "db_user": "u", "db_name": "d", "file_path": None}

    mock_response = MagicMock(status_code=200)
    with patch("app.agent_client.requests.post", return_value=mock_response) as mock_post:
        response = agent_client.open_backup_stream(agent, settings, target_row)
        assert response is mock_response
        assert mock_post.call_args.kwargs["timeout"] == agent_client.LONG_TIMEOUT
        assert mock_post.call_args.kwargs["stream"] is True


def test_open_backup_stream_non_200_raises_but_still_counts_as_reached(tmp_path):
    """A well-formed non-2xx response means we successfully talked to the agent, it just
    reported a backup failure, that's a contact success, not an unreachable agent."""
    state_db, agent_id = _make_agent(tmp_path)
    settings = FakeSettings(state_db)
    agent = _agent(agent_id)
    target_row = {"engine": "postgres", "container_name": "c", "db_user": "u", "db_name": "d", "file_path": None}

    mock_response = MagicMock(status_code=500)
    mock_response.json.return_value = {"error": "boom"}
    with patch("app.agent_client.requests.post", return_value=mock_response):
        with pytest.raises(agent_client.AgentError, match="boom"):
            agent_client.open_backup_stream(agent, settings, target_row)

    conn = db_module.get_connection(state_db)
    assert db_module.get_agent(conn, agent_id)["last_contact_status"] == "ok"


def test_open_backup_stream_connection_failure_records_error(tmp_path):
    state_db, agent_id = _make_agent(tmp_path)
    settings = FakeSettings(state_db)
    agent = _agent(agent_id)
    target_row = {"engine": "postgres", "container_name": "c", "db_user": "u", "db_name": "d", "file_path": None}

    with patch("app.agent_client.requests.post", side_effect=requests.ConnectionError("refused")):
        with pytest.raises(agent_client.AgentError):
            agent_client.open_backup_stream(agent, settings, target_row)

    conn = db_module.get_connection(state_db)
    assert db_module.get_agent(conn, agent_id)["last_contact_status"] == "error"


def test_run_restore_parses_json_body_regardless_of_status(tmp_path):
    state_db, agent_id = _make_agent(tmp_path)
    settings = FakeSettings(state_db)
    agent = _agent(agent_id)

    source_path = str(tmp_path / "backup.dump")
    with open(source_path, "wb") as f:
        f.write(b"data")

    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"success": True, "stopped_container": True, "error": None}
    target_row = {"engine": "sqlite", "container_name": "c", "db_user": "", "db_name": "", "file_path": "/data/app.db"}
    with patch("app.agent_client.requests.post", return_value=mock_response) as mock_post:
        result = agent_client.run_restore(agent, settings, target_row, source_path, True)
        assert result == {"success": True, "stopped_container": True, "error": None}
        assert mock_post.call_args.kwargs["timeout"] == agent_client.LONG_TIMEOUT


def test_run_restore_raises_on_connection_failure(tmp_path):
    state_db, agent_id = _make_agent(tmp_path)
    settings = FakeSettings(state_db)
    agent = _agent(agent_id)

    source_path = str(tmp_path / "backup.dump")
    with open(source_path, "wb") as f:
        f.write(b"data")

    target_row = {"engine": "postgres", "container_name": "c", "db_user": "u", "db_name": "d", "file_path": None}
    with patch("app.agent_client.requests.post", side_effect=requests.ConnectionError("down")):
        with pytest.raises(agent_client.AgentError):
            agent_client.run_restore(agent, settings, target_row, source_path, False)

    conn = db_module.get_connection(state_db)
    assert db_module.get_agent(conn, agent_id)["last_contact_status"] == "error"
