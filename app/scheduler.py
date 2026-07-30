from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db as db_module
from . import jobs
from . import notifications
from . import restore as restore_module

logger = logging.getLogger("savepoint.scheduler")

DEFAULT_WINDOW_START = "02:00"
DEFAULT_WINDOW_DURATION_MINUTES = "120"
DEFAULT_WINDOW_CONCURRENCY = "2"

WINDOW_JOB_ID = "window-tick"

_scheduler: BackgroundScheduler | None = None
_settings = None


def start(settings):
    global _scheduler, _settings
    _settings = settings
    jobs.init(settings)
    notifications.init(settings)
    restore_module.init(settings)

    _scheduler = BackgroundScheduler(executors={"default": {"type": "threadpool", "max_workers": 20}})
    _scheduler.start()

    conn = db_module.get_connection(settings.state_db_path)
    try:
        for target in db_module.list_all_targets(conn):
            sync_target_schedule(target)
        _register_window_job(conn)
    finally:
        conn.close()

    return _scheduler


def shutdown() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)


def sync_target_schedule(target) -> None:
    job_id = f"target-{target['id']}"
    if target["schedule_cron"] and target["enabled"] and not target["agent_offsite"]:
        trigger = CronTrigger.from_crontab(target["schedule_cron"])
        _scheduler.add_job(
            jobs.run_backup, trigger, args=[target["id"], "schedule"], id=job_id, replace_existing=True
        )
    elif _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)


def remove_target_job(target_id: int) -> None:
    """Unconditional job removal, used when deleting a target: there's no target row
    left afterward to check schedule_cron against, so this doesn't go through
    sync_target_schedule().
    """
    job_id = f"target-{target_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)


def dispatch_manual(target_id: int, run_id: int) -> None:
    _scheduler.add_job(jobs.execute_claimed, args=[target_id, run_id])


def dispatch_restore(target_id: int, restore_run_id: int, backup_run_id: int, stop_container: bool) -> None:
    _scheduler.add_job(
        jobs.execute_restore_claimed, args=[target_id, restore_run_id, backup_run_id, stop_container]
    )


def next_window_fire_time(conn):
    """The next time the shared backup window will fire, using the same trigger
    construction _register_window_job() uses to actually register it. Used by the
    dashboard's "next backup window" display.
    """
    start_str = db_module.get_setting(conn, "window_start", DEFAULT_WINDOW_START)
    parsed = parse_hhmm(start_str) or parse_hhmm(DEFAULT_WINDOW_START)
    hour, minute = parsed
    return next_fire_time(CronTrigger(hour=hour, minute=minute))


def next_fire_time(trigger, after=None):
    """Thin wrapper around APScheduler's own trigger.get_next_fire_time(), so callers
    (the target detail page's "next run" display, the dashboard's "next window" display)
    don't need to know APScheduler's specific API. `after` defaults to now (UTC); callers
    build whatever trigger applies, a target's own CronTrigger.from_crontab(...), or the
    shared window's CronTrigger(hour=h, minute=m), exactly how _register_window_job()
    already builds it.
    """
    return trigger.get_next_fire_time(None, after or datetime.now(timezone.utc))


def parse_hhmm(value: str):
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _register_window_job(conn) -> None:
    start_str = db_module.get_setting(conn, "window_start", DEFAULT_WINDOW_START)
    parsed = parse_hhmm(start_str)
    if parsed is None:
        logger.error("invalid window_start setting %r, falling back to %s", start_str, DEFAULT_WINDOW_START)
        parsed = parse_hhmm(DEFAULT_WINDOW_START)
    hour, minute = parsed
    trigger = CronTrigger(hour=hour, minute=minute)
    _scheduler.add_job(window_tick, trigger, id=WINDOW_JOB_ID, replace_existing=True)


def reload_window_job() -> None:
    conn = db_module.get_connection(_settings.state_db_path)
    try:
        _register_window_job(conn)
    finally:
        conn.close()


def window_tick(deadline=None) -> None:
    """Runs continuously from window start until every member has been backed up or the
    window's duration elapses, whichever comes first. `deadline` is exposed as a
    parameter purely for test injection, production callers always let it compute from
    the current settings.
    """
    conn = db_module.get_connection(_settings.state_db_path)
    try:
        duration_minutes = int(db_module.get_setting(conn, "window_duration_minutes", DEFAULT_WINDOW_DURATION_MINUTES))
        concurrency = int(db_module.get_setting(conn, "window_concurrency", DEFAULT_WINDOW_CONCURRENCY))

        members = [
            t for t in db_module.list_all_targets(conn) if t["in_window"] and t["enabled"] and not t["agent_offsite"]
        ]
        work_queue: queue.Queue = queue.Queue()
        run_ids = []
        target_names = {}
        for target in members:
            run_id = db_module.create_backup_run(conn, target["id"], status="queued", triggered_by="window")
            work_queue.put((target["id"], run_id))
            run_ids.append(run_id)
            target_names[run_id] = target["name"]
    finally:
        conn.close()

    if not run_ids:
        return

    if deadline is None:
        deadline = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)

    def worker():
        while datetime.now(timezone.utc) < deadline:
            try:
                target_id, run_id = work_queue.get_nowait()
            except queue.Empty:
                break
            jobs.run_backup(target_id, "window", run_id=run_id)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    conn = db_module.get_connection(_settings.state_db_path)
    try:
        success = failure = skipped = 0
        failed_names = []
        skipped_names = []
        for run_id in run_ids:
            run = db_module.get_backup_run(conn, run_id)
            name = target_names.get(run_id, f"#{run['target_id']}")
            if run["status"] == "queued":
                db_module.finish_backup_run(
                    conn, run_id, "skipped", error_message="window closed before this could run"
                )
                skipped += 1
                skipped_names.append(name)
            elif run["status"] == "success":
                success += 1
            elif run["status"] == "failure":
                failure += 1
                failed_names.append(name)
            elif run["status"] == "skipped":
                skipped += 1
                skipped_names.append(name)
    finally:
        conn.close()

    notifications.notify_window_summary(success, failure, skipped, failed_names, skipped_names)
