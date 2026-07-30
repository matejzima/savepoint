from __future__ import annotations

import logging

import requests

logger = logging.getLogger("savepoint.notifications")

_settings = None


def init(settings) -> None:
    global _settings
    _settings = settings


def _enabled() -> bool:
    return bool(_settings and _settings.ntfy_url and _settings.ntfy_topic)


def _send(message: str, title: str) -> None:
    if not _enabled():
        return
    url = f"{_settings.ntfy_url.rstrip('/')}/{_settings.ntfy_topic}"
    headers = {"Title": title}
    if _settings.ntfy_token:
        headers["Authorization"] = f"Bearer {_settings.ntfy_token}"
    try:
        response = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("failed to send ntfy notification")


def notify_failure(target, result) -> None:
    _send(
        f"Backup failed for '{target['name']}' ({target['engine']}): {result.error_message}",
        title="Savepoint backup failed",
    )


def notify_restore_result(target, backup_run, success: bool, error_message: str | None) -> None:
    backup_label = backup_run["started_at"] if backup_run else "unknown backup"
    if success:
        message = f"Restore succeeded for '{target['name']}' ({target['engine']}) from backup taken at {backup_label}."
        if error_message:
            message += f" {error_message}"
        _send(message, title="Savepoint restore succeeded")
    else:
        _send(
            f"Restore failed for '{target['name']}' ({target['engine']}) from backup taken at {backup_label}: {error_message}",
            title="Savepoint restore failed",
        )


def notify_window_summary(
    success_count: int,
    failure_count: int,
    skipped_count: int,
    failed_names: list,
    skipped_names: list,
) -> None:
    lines = [f"Window complete: {success_count} succeeded, {failure_count} failed, {skipped_count} skipped."]
    if failed_names:
        lines.append("Failed: " + ", ".join(failed_names))
    if skipped_names:
        lines.append("Skipped (window closed): " + ", ".join(skipped_names))
    _send("\n".join(lines), title="Savepoint window summary")
