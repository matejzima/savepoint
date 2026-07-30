from __future__ import annotations

import logging
import os
from datetime import date, datetime

from . import db as db_module

logger = logging.getLogger("savepoint.retention")

TIERS = ("daily", "weekly", "monthly")


def _run_date(run) -> date:
    return datetime.fromisoformat(run["started_at"]).date()


def compute_tags_for_run(conn, target_id: int, run) -> list:
    """Which tier(s) `run` earns, based on its own started_at date and this target's
    existing tagged history. Always daily; weekly/monthly only if no other run for this
    target already holds that tag for the same ISO week / calendar month.
    """
    tags = ["daily"]
    run_date = _run_date(run)
    run_week_key = run_date.isocalendar()[:2]
    run_month_key = (run_date.year, run_date.month)

    existing_weekly = db_module.list_tagged_runs(conn, target_id, "weekly")
    if not any(
        _run_date(r).isocalendar()[:2] == run_week_key for r in existing_weekly if r["id"] != run["id"]
    ):
        tags.append("weekly")

    existing_monthly = db_module.list_tagged_runs(conn, target_id, "monthly")
    if not any(
        (_run_date(r).year, _run_date(r).month) == run_month_key
        for r in existing_monthly
        if r["id"] != run["id"]
    ):
        tags.append("monthly")

    return tags


def tag_and_prune(conn, target, run_id: int, backup_target_dir: str) -> None:
    """Called right after a run is recorded as `success`. Tags always happen; pruning
    only runs if this target's retention has been explicitly confirmed.
    """
    run = db_module.get_backup_run(conn, run_id)
    for tier in compute_tags_for_run(conn, target["id"], run):
        db_module.add_backup_run_tag(conn, run_id, tier)

    if target["retention_confirmed"]:
        prune_target(conn, target, backup_target_dir)


def prune_target(conn, target, backup_target_dir: str) -> None:
    """Keep the N most recent runs per tier for this target, dropping tag membership
    past that. A run whose file is deleted here only when it holds no tags at all.
    """
    touched_run_ids = set()

    for tier in TIERS:
        keep_n = target[f"retention_{tier}"]
        tagged = db_module.list_tagged_runs(conn, target["id"], tier)
        for run in tagged[keep_n:]:
            db_module.remove_backup_run_tag(conn, run["id"], tier)
            touched_run_ids.add(run["id"])

    for run_id in touched_run_ids:
        if db_module.has_any_tags(conn, run_id):
            continue
        run = db_module.get_backup_run(conn, run_id)
        file_path = run["file_path"]
        if not file_path:
            continue
        try:
            os.remove(file_path)
            logger.info("pruned backup file %s (target=%s, run=%s)", file_path, target["name"], run_id)
        except FileNotFoundError:
            logger.warning(
                "backup file %s already missing, clearing retention bookkeeping anyway "
                "(target=%s, run=%s)",
                file_path,
                target["name"],
                run_id,
            )


def reconcile_all(settings) -> None:
    """Run once at startup: backfill tags for every target's untagged successful runs
    (always, regardless of confirmation), then prune only targets whose retention has
    been explicitly confirmed. Idempotent, a no-op once a target is fully tagged and
    (if confirmed) already pruned to its configured counts.
    """
    conn = db_module.get_connection(settings.state_db_path)
    try:
        for target in db_module.list_all_targets(conn):
            for run in db_module.list_successful_runs(conn, target["id"]):
                if db_module.has_any_tags(conn, run["id"]):
                    continue
                for tier in compute_tags_for_run(conn, target["id"], run):
                    db_module.add_backup_run_tag(conn, run["id"], tier)

            if target["retention_confirmed"]:
                prune_target(conn, target, settings.backup_target_dir)
    finally:
        conn.close()
