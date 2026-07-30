import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from apscheduler.schedulers.background import BackgroundScheduler

from app import db as db_module
from app import jobs, scheduler
from app.adapters.base import BackupResult


class FakeSettings:
    def __init__(self, state_db_path, backup_target_dir="/tmp"):
        self.state_db_path = state_db_path
        self.backup_target_dir = backup_target_dir


def _make_target(conn, name):
    return db_module.create_target(conn, name, "postgres", f"{name}-c", "u", "d")


def setup_function(_):
    jobs._in_progress.clear()


def test_parse_hhmm():
    assert scheduler.parse_hhmm("02:00") == (2, 0)
    assert scheduler.parse_hhmm("23:59") == (23, 59)
    assert scheduler.parse_hhmm("24:00") is None
    assert scheduler.parse_hhmm("not-a-time") is None
    assert scheduler.parse_hhmm("2:0:0") is None


def test_next_fire_time_computes_next_occurrence_of_a_cron_trigger():
    from apscheduler.triggers.cron import CronTrigger

    # CronTrigger interprets the expression in the system's local timezone (matching
    # Phase 3's intentional wall-clock semantics, an operator-entered "3am" cron means
    # 3am local time, not UTC), so this only asserts on the wall-clock fields, not a
    # UTC-anchored instant which would vary with the test machine's TZ.
    after = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    trigger = CronTrigger.from_crontab("0 3 * * *")  # every day at 03:00

    result = scheduler.next_fire_time(trigger, after=after)

    assert result is not None
    assert result.hour == 3
    assert result.minute == 0


def test_next_fire_time_defaults_after_to_now():
    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger.from_crontab("* * * * *")  # fires every minute
    result = scheduler.next_fire_time(trigger)

    assert result is not None
    assert result >= datetime.now(timezone.utc)


def test_next_window_fire_time_uses_configured_window_start(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    db_module.set_setting(conn, "window_start", "02:30")

    result = scheduler.next_window_fire_time(conn)

    assert result.hour == 2
    assert result.minute == 30


def test_next_window_fire_time_falls_back_to_default_when_unset(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)

    result = scheduler.next_window_fire_time(conn)

    default_hour, default_minute = scheduler.parse_hhmm(scheduler.DEFAULT_WINDOW_START)
    assert result.hour == default_hour
    assert result.minute == default_minute


def test_next_window_fire_time_falls_back_to_default_when_invalid(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    db_module.set_setting(conn, "window_start", "not-a-time")

    result = scheduler.next_window_fire_time(conn)

    default_hour, default_minute = scheduler.parse_hhmm(scheduler.DEFAULT_WINDOW_START)
    assert result.hour == default_hour
    assert result.minute == default_minute


def test_sync_target_schedule_adds_updates_and_removes_job():
    sched = BackgroundScheduler()
    sched.start()
    scheduler._scheduler = sched
    try:
        target = {"id": 999, "schedule_cron": "0 3 * * *", "enabled": 1, "agent_offsite": 0}
        scheduler.sync_target_schedule(target)
        job = sched.get_job("target-999")
        assert job is not None
        first_trigger = str(job.trigger)

        target_updated = {"id": 999, "schedule_cron": "30 4 * * *", "enabled": 1, "agent_offsite": 0}
        scheduler.sync_target_schedule(target_updated)
        job = sched.get_job("target-999")
        assert job is not None
        assert str(job.trigger) != first_trigger

        target_cleared = {"id": 999, "schedule_cron": None, "enabled": 1, "agent_offsite": 0}
        scheduler.sync_target_schedule(target_cleared)
        assert sched.get_job("target-999") is None
    finally:
        sched.shutdown(wait=False)


def test_sync_target_schedule_does_not_register_job_when_disabled():
    sched = BackgroundScheduler()
    sched.start()
    scheduler._scheduler = sched
    try:
        target_disabled = {"id": 777, "schedule_cron": "0 3 * * *", "enabled": 0, "agent_offsite": 0}
        scheduler.sync_target_schedule(target_disabled)
        assert sched.get_job("target-777") is None

        target_enabled = {"id": 777, "schedule_cron": "0 3 * * *", "enabled": 1, "agent_offsite": 0}
        scheduler.sync_target_schedule(target_enabled)
        assert sched.get_job("target-777") is not None

        scheduler.sync_target_schedule(target_disabled)
        assert sched.get_job("target-777") is None
    finally:
        sched.shutdown(wait=False)


def test_remove_target_job_unregisters_unconditionally():
    sched = BackgroundScheduler()
    sched.start()
    scheduler._scheduler = sched
    try:
        target = {"id": 888, "schedule_cron": "0 3 * * *", "enabled": 1, "agent_offsite": 0}
        scheduler.sync_target_schedule(target)
        assert sched.get_job("target-888") is not None

        scheduler.remove_target_job(888)
        assert sched.get_job("target-888") is None

        scheduler.remove_target_job(888)  # calling again on an already-gone job must not raise
    finally:
        sched.shutdown(wait=False)


def test_sync_target_schedule_skips_offsite_agent_targets():
    sched = BackgroundScheduler()
    sched.start()
    scheduler._scheduler = sched
    try:
        target = {"id": 555, "schedule_cron": "0 3 * * *", "enabled": 1, "agent_offsite": 1}
        scheduler.sync_target_schedule(target)
        assert sched.get_job("target-555") is None
    finally:
        sched.shutdown(wait=False)


def test_window_tick_excludes_offsite_agent_targets(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "cottage", "http://cottage:8000", "tok", offsite=True)
    local_id = _make_target(conn, "local-target")
    remote_id = db_module.create_target(conn, "offsite-target", "postgres", "remote-c", "u", "d", agent_id=agent_id)
    db_module.update_target_schedule(conn, local_id, None, in_window=True)
    db_module.update_target_schedule(conn, remote_id, None, in_window=True)
    conn.close()

    settings = FakeSettings(state_db)
    jobs.init(settings)
    scheduler._settings = settings

    fake_adapter = MagicMock()
    fake_adapter.backup.return_value = BackupResult(
        success=True, file_path="/tmp/x", file_size_bytes=1, error_message=None
    )

    with patch("app.jobs.ADAPTERS", {"postgres": fake_adapter}):
        scheduler.window_tick()

    conn = db_module.get_connection(state_db)
    assert len(db_module.list_backup_runs(conn, local_id)) == 1
    assert len(db_module.list_backup_runs(conn, remote_id)) == 0


def test_window_tick_excludes_disabled_members(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    enabled_id = _make_target(conn, "enabled-target")
    disabled_id = _make_target(conn, "disabled-target")
    db_module.update_target_schedule(conn, enabled_id, None, in_window=True)
    db_module.update_target_schedule(conn, disabled_id, None, in_window=True)
    db_module.update_target_enabled(conn, disabled_id, False)
    conn.close()

    settings = FakeSettings(state_db)
    jobs.init(settings)
    scheduler._settings = settings

    fake_adapter = MagicMock()
    fake_adapter.backup.return_value = BackupResult(
        success=True, file_path="/tmp/x", file_size_bytes=1, error_message=None
    )

    with patch("app.jobs.ADAPTERS", {"postgres": fake_adapter}):
        scheduler.window_tick()

    conn = db_module.get_connection(state_db)
    assert len(db_module.list_backup_runs(conn, enabled_id)) == 1
    assert len(db_module.list_backup_runs(conn, disabled_id)) == 0


def test_window_tick_respects_concurrency_cap_and_processes_all(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_ids = [_make_target(conn, f"t{i}") for i in range(5)]
    for tid in target_ids:
        db_module.update_target_schedule(conn, tid, None, in_window=True)
    conn.close()

    settings = FakeSettings(state_db)
    jobs.init(settings)
    scheduler._settings = settings

    current = {"n": 0}
    peak = {"n": 0}
    guard = threading.Lock()

    def fake_backup(target_row, backup_target_dir):
        with guard:
            current["n"] += 1
            peak["n"] = max(peak["n"], current["n"])
        time.sleep(0.05)
        with guard:
            current["n"] -= 1
        return BackupResult(success=True, file_path="/tmp/x", file_size_bytes=1, error_message=None)

    fake_adapter = MagicMock()
    fake_adapter.backup.side_effect = fake_backup

    conn = db_module.get_connection(state_db)
    db_module.set_setting(conn, "window_concurrency", "2")
    conn.close()

    with patch("app.jobs.ADAPTERS", {"postgres": fake_adapter}):
        scheduler.window_tick()

    assert peak["n"] == 2  # ran concurrently, not sequentially, but never exceeded the cap

    conn = db_module.get_connection(state_db)
    for tid in target_ids:
        run = db_module.list_backup_runs(conn, tid)[0]
        assert run["status"] == "success"
        assert run["triggered_by"] == "window"


def test_window_tick_skips_targets_left_when_duration_elapses(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_ids = [_make_target(conn, f"t{i}") for i in range(4)]
    for tid in target_ids:
        db_module.update_target_schedule(conn, tid, None, in_window=True)
    db_module.set_setting(conn, "window_concurrency", "1")
    conn.close()

    settings = FakeSettings(state_db)
    jobs.init(settings)
    scheduler._settings = settings

    def fake_backup(target_row, backup_target_dir):
        time.sleep(0.08)
        return BackupResult(success=True, file_path="/tmp/x", file_size_bytes=1, error_message=None)

    fake_adapter = MagicMock()
    fake_adapter.backup.side_effect = fake_backup

    # concurrency 1, ~0.08s per job -> room for roughly 2 of the 4 before the deadline
    deadline = datetime.now(timezone.utc) + timedelta(seconds=0.18)

    with patch("app.jobs.ADAPTERS", {"postgres": fake_adapter}), patch(
        "app.scheduler.notifications.notify_window_summary"
    ) as mock_summary:
        scheduler.window_tick(deadline=deadline)

    conn = db_module.get_connection(state_db)
    statuses = []
    for tid in target_ids:
        run = db_module.list_backup_runs(conn, tid)[0]
        statuses.append(run["status"])
        if run["status"] == "skipped":
            assert "window closed" in run["error_message"]

    assert set(statuses) <= {"success", "skipped"}
    assert statuses.count("success") >= 1
    assert statuses.count("skipped") >= 1

    mock_summary.assert_called_once()
    args = mock_summary.call_args.args
    success_count, failure_count, skipped_count = args[0], args[1], args[2]
    assert success_count == statuses.count("success")
    assert skipped_count == statuses.count("skipped")
    assert failure_count == 0


def test_window_tick_with_no_members_does_not_notify(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    settings = FakeSettings(state_db)
    jobs.init(settings)
    scheduler._settings = settings

    with patch("app.scheduler.notifications.notify_window_summary") as mock_summary:
        scheduler.window_tick()

    mock_summary.assert_not_called()
