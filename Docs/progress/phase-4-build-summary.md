# Savepoint - Phase 4 Build Summary

Built against [phase-4-plan.md](phase-4-plan.md) (approved as amended: per-target `retention_confirmed` gate replacing auto-prune-on-first-boot).

## What was built

- **Schema**: `targets` gained `retention_daily`/`retention_weekly`/`retention_monthly` (default 7/4/2) and `retention_confirmed` (default 0); a new `backup_run_tags` join table (`backup_run_id`, `tier`, `tagged_at`). All migrated in place via the same guarded `ALTER TABLE ADD COLUMN` pattern from Phases 2-3.
- **`app/retention.py`**: `compute_tags_for_run()` (always `daily`; `weekly`/`monthly` only if no other run for that target already holds the tag for the same ISO `(year, week)` / calendar `(year, month)`, computed via `datetime.date.isocalendar()` in Python, never SQLite's non-ISO `strftime('%W')`, against `started_at` specifically), `tag_and_prune()` (tags always, prunes only if `retention_confirmed`), `prune_target()` (keep-N-per-tier, deletes a run's file only once every tag is gone, tolerates an already-missing file), `reconcile_all()` (startup backfill + confirmed-only prune, idempotent).
- **`jobs.py`**: `_execute()`'s success branch now calls `retention.tag_and_prune()` right after `finish_backup_run(..., "success", ...)`, the one and only place a run becomes tagged.
- **`main.py`**: calls `retention.reconcile_all(settings)` once at startup, right after `db.init_db()`.
- **`POST /targets/{id}/retention`**: validates the three counts as positive integers, saves via `update_target_retention()` (which also sets `retention_confirmed = 1`, the only path that ever does), and — see bug below — now also runs an immediate `prune_target()` against whatever history already exists for that target.
- **UI**: target detail page gained a "Retention" section (three number inputs) showing a "not yet active for this target..." notice while unconfirmed; history rows show tier chips (`daily`/`weekly`/`monthly`) next to the status pill for tagged (`success`) runs, styled as neutral/muted badges distinct from the colored status pill since several can appear on one row.
- Tests: `tests/test_retention.py` (16 cases: ISO week/month tagging including an explicit Dec 2025/Jan 2026 ISO-year-boundary case, keep-N-per-tier, file-survives-while-any-tag-remains, file-deleted-once-last-tag-gone, missing-file tolerance, the confirmation gate on both `tag_and_prune` and `reconcile_all`, and `reconcile_all` idempotency), plus new cases in `tests/test_targets_routes.py` for the retention route (save persists + confirms, re-saving unchanged defaults still confirms, non-positive counts rejected, and the immediate-prune-on-confirm regression case below).

## Bug caught during testing

The route for `POST /targets/{id}/retention` originally only saved the three counts and set `retention_confirmed = 1`. It did not prune anything itself, pruning would only have happened on the target's *next* successful backup or the *next* app restart, both of which fold in `retention.prune_target()`. That's a real gap against the plan's own verification item 7 ("save the Retention form... confirm only that target's excess history gets pruned"), it means confirming retention for a target with months of pre-existing, already-backfilled history would silently do nothing visible until the next scheduled run or restart, exactly the kind of "nothing happened, is this broken?" gap a manual sanity pass exists to catch. Caught by running the exact scenario from verification item 7 by hand: created a Phase-3-shaped db with 3 pre-existing successful runs, confirmed the reconciliation backfilled tags without pruning (correct), then saved the target's Retention form and found none of the excess history was removed. Fixed by having the route call `retention.prune_target()` immediately after `update_target_retention()`, using the just-updated target row and `request.app.state.settings.backup_target_dir`.

## Testing performed

- `pytest tests/` - 59/59 pass (43 from Phases 1-3, 16 new in `test_retention.py`, plus 4 new retention-route cases in `test_targets_routes.py`).
- Booted the app against a hand-built Phase-3-shaped state db (no retention columns, no `backup_run_tags`, one target with 3 pre-existing successful runs spanning the same ISO week/month) and confirmed:
  - Both retention columns and the new table appear via the guarded migration.
  - Startup reconciliation correctly tags all 3 pre-existing runs (first run: daily+weekly+monthly; the other two, same week/month: daily only) and prunes nothing, since the target starts unconfirmed. A second startup produced no duplicate tags.
  - Confirming retention via the UI (defaults 1/4/2 for this test) immediately pruned the one daily-only, non-most-recent run while the week/month-holder and the most-recent daily survived, exactly the intended GFS behavior, and the "not yet active" notice disappeared from the detail page.
- Ran a full live manual backup against a fresh target with mocked Docker end to end and confirmed the resulting `success` row picked up a `daily` tag chip automatically via the inline success path, no restart or manual reconciliation needed.

## Not tested here (needs the real homelab Docker host)

No Docker daemon or multi-day/multi-week real timeline was exercised in this dev environment beyond backdating `started_at` values directly in the state db. Before considering Phase 4 done, run the plan's verification steps against real containers, in particular:
1. Let real backups accumulate over actual days/weeks (not backdated rows) and confirm tagging and pruning behave the same way against real wall-clock time and the container's `Europe/Prague` timezone (Phase 3's timezone fix).
2. Confirm pruning a real, large backlog of pre-existing files (not the 3-row synthetic case here) completes in reasonable time and the logged deletions are legible for an actual audit.
3. Confirm the "not yet active" retention notice and tier chips read clearly in an actual browser, not just in rendered HTML.
