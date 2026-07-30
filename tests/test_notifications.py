from unittest.mock import MagicMock, patch

import requests

from app import notifications


class FakeSettings:
    def __init__(self, ntfy_url=None, ntfy_topic=None, ntfy_token=None):
        self.ntfy_url = ntfy_url
        self.ntfy_topic = ntfy_topic
        self.ntfy_token = ntfy_token


def test_notify_failure_is_noop_when_not_configured():
    notifications.init(FakeSettings(None, None))
    with patch("app.notifications.requests.post") as mock_post:
        notifications.notify_failure({"name": "x", "engine": "postgres"}, MagicMock(error_message="boom"))
    mock_post.assert_not_called()


def test_notify_failure_posts_to_configured_topic():
    notifications.init(FakeSettings("https://ntfy.example.com", "savepoint"))
    with patch("app.notifications.requests.post") as mock_post:
        notifications.notify_failure({"name": "mydb", "engine": "postgres"}, MagicMock(error_message="boom"))

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://ntfy.example.com/savepoint"
    assert b"mydb" in kwargs["data"]
    assert b"boom" in kwargs["data"]


def test_notify_failure_strips_trailing_slash_from_url():
    notifications.init(FakeSettings("https://ntfy.example.com/", "savepoint"))
    with patch("app.notifications.requests.post") as mock_post:
        notifications.notify_failure({"name": "mydb", "engine": "postgres"}, MagicMock(error_message="boom"))

    args, _ = mock_post.call_args
    assert args[0] == "https://ntfy.example.com/savepoint"


def test_notify_failure_swallows_request_errors():
    notifications.init(FakeSettings("https://ntfy.example.com", "savepoint"))
    with patch("app.notifications.requests.post", side_effect=requests.RequestException("down")):
        notifications.notify_failure({"name": "mydb", "engine": "postgres"}, MagicMock(error_message="boom"))
    # no exception raised is the assertion


def test_notify_failure_swallows_non_2xx_response():
    notifications.init(FakeSettings("https://ntfy.example.com", "savepoint"))
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized")
    with patch("app.notifications.requests.post", return_value=mock_response):
        notifications.notify_failure({"name": "mydb", "engine": "postgres"}, MagicMock(error_message="boom"))
    # no exception raised is the assertion, but raise_for_status must have been checked
    mock_response.raise_for_status.assert_called_once()


def test_notify_window_summary_posts_counts_and_names():
    notifications.init(FakeSettings("https://ntfy.example.com", "savepoint"))
    with patch("app.notifications.requests.post") as mock_post:
        notifications.notify_window_summary(3, 1, 2, ["dbA"], ["dbB", "dbC"])

    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    body = kwargs["data"].decode("utf-8")
    assert "3 succeeded" in body
    assert "1 failed" in body
    assert "2 skipped" in body
    assert "dbA" in body
    assert "dbB" in body
    assert "dbC" in body


def test_notify_window_summary_noop_when_not_configured():
    notifications.init(FakeSettings(None, None))
    with patch("app.notifications.requests.post") as mock_post:
        notifications.notify_window_summary(1, 0, 0, [], [])
    mock_post.assert_not_called()


def test_notify_failure_sends_bearer_token_when_configured():
    notifications.init(FakeSettings("https://ntfy.example.com", "savepoint", ntfy_token="secret-token"))
    with patch("app.notifications.requests.post") as mock_post:
        notifications.notify_failure({"name": "mydb", "engine": "postgres"}, MagicMock(error_message="boom"))

    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer secret-token"


def test_notify_failure_omits_authorization_header_when_token_unset():
    notifications.init(FakeSettings("https://ntfy.example.com", "savepoint", ntfy_token=None))
    with patch("app.notifications.requests.post") as mock_post:
        notifications.notify_failure({"name": "mydb", "engine": "postgres"}, MagicMock(error_message="boom"))

    _, kwargs = mock_post.call_args
    assert "Authorization" not in kwargs["headers"]


def test_notify_restore_result_fires_on_success_unlike_backup():
    """Unlike notify_failure, restore notifies on both outcomes: it's rare, manual, and
    deliberate, the operator wants to know it's done either way without babysitting."""
    notifications.init(FakeSettings("https://ntfy.example.com", "savepoint"))
    backup_run = {"started_at": "2026-01-01T00:00:00+00:00"}
    with patch("app.notifications.requests.post") as mock_post:
        notifications.notify_restore_result({"name": "mydb", "engine": "postgres"}, backup_run, True, None)

    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert b"succeeded" in kwargs["data"]
    assert b"mydb" in kwargs["data"]


def test_notify_restore_result_fires_on_failure_with_error_message():
    notifications.init(FakeSettings("https://ntfy.example.com", "savepoint"))
    backup_run = {"started_at": "2026-01-01T00:00:00+00:00"}
    with patch("app.notifications.requests.post") as mock_post:
        notifications.notify_restore_result({"name": "mydb", "engine": "postgres"}, backup_run, False, "boom")

    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert b"failed" in kwargs["data"]
    assert b"boom" in kwargs["data"]


def test_notify_restore_result_noop_when_not_configured():
    notifications.init(FakeSettings(None, None))
    backup_run = {"started_at": "2026-01-01T00:00:00+00:00"}
    with patch("app.notifications.requests.post") as mock_post:
        notifications.notify_restore_result({"name": "mydb", "engine": "postgres"}, backup_run, True, None)
    mock_post.assert_not_called()
