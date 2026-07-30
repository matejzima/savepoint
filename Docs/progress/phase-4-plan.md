# Savepoint - Phase 4 Plan: Rolling (GFS) Retention

## Context

Phases 1-3 give Savepoint the ability to run, schedule, and window-orchestrate backups, but every successful backup lives forever, nothing ever gets cleaned up. Phase 4, as scoped in `Docs/04-Initial-Build-Plan.md` and detailed in `02-Requirements.md`/`03-Proposed-Architecture.md`, adds grandfather-father-son (GFS) retention:

- Tag each successful backup with the tier(s) it earns based on its date (`daily` always, `weekly`/`monthly` if it's the first success of that ISO week/calendar month for its target).
- Per-tier retention counts, configurable per target, default 7/4/2.
- A prune sweep that deletes a backup's file once it holds no tags at all (keep the N most recent per tag, drop membership past that).
- Tier membership visible in the history table.
- Only `success` rows with a real file are ever tagged or pruned, `queued`/`skipped`/`failure` rows are untouched.

## Design decisions

- **Tagging and pruning run inline, folded into the existing success path, not on a separate schedule.** `jobs.py::_execute()` is already the one place a run gets marked `success` (a Phase 3 invariant). Whether a given backup is the first of its target's ISO week/month is a fact about that one target's own history, evaluated at the moment it succeeds, it never depends on any other target. So tagging is O(1) per backup (a couple of row inserts), and pruning after it is bounded by that target's own retention counts, not a full-database scan. A periodic sweep would mean re-deriving the same per-target facts repeatedly on a timer, with a delay between a backup finishing and its tier showing up (or excess copies actually being deleted) for no benefit, since nothing else in this codebase can ever produce a `success` row except this one code path. Concretely: right after `finish_backup_run(conn, run_id, "success", ...)` inside `_execute()`, call `retention.tag_and_prune(conn, target, run_id, backup_target_dir)`.
- **ISO week/month math happens in Python, not SQLite.** SQLite's `strftime('%W', ...)` is "week of year, Monday first day", not ISO 8601, it disagrees with `date.isocalendar()` at year boundaries (e.g. Dec 29 2025 is ISO week 1 of 2026 but a different number under `%W`). Getting this wrong would silently mis-tag backups right at year-end, exactly the kind of bug that's invisible until someone notices a January backup didn't get a `weekly` tag. `retention.py` computes `(iso_year, iso_week)` and `(year, month)` via `datetime.date.isocalendar()` / `.year, .month` in Python, applied to `backup_runs.started_at`, and compares against the same computed from `started_at` for each of a target's existing tagged runs. ("Own date" throughout this plan means `started_at` specifically, not `finished_at`, stated explicitly here so it isn't left implied.)
- **Data model: a join table, not boolean columns.** A run can hold multiple tags at once, and each tag ages out independently of the others (a run might lose `weekly` while keeping `daily`). Modeling this as `backup_run_tags(backup_run_id, tier, tagged_at)` makes both "does this run have any tags left" and "keep the N most recent per tier" plain SQL (a `GROUP BY`/count and an `ORDER BY ... LIMIT`), instead of bespoke logic for clearing and checking three separate boolean columns. It also means a future tier (nobody's asked for `yearly`, but it's the obvious next one) needs zero schema change. No `target_id` column on the join table, it's reachable through `backup_runs.target_id`, no need to duplicate it.
- **Retention counts live on `targets`** (`retention_daily`, `retention_weekly`, `retention_monthly`, `NOT NULL DEFAULT` 7/4/2), not a single global setting, exactly as scoped ("configurable per target"). Configured via a new "Retention" section on the target detail page, next to the existing "Schedule" section, `POST /targets/{id}/retention` validates each as a positive integer.
- **Historical (pre-Phase-4) successful runs get backfilled, not left permanently untagged.** The alternative, only tagging runs going forward, means Phase 1-3 history never shows a tier, and never gets cleaned up, for as long as the target exists, an inconsistency that only gets worse over time and is confusing in the UI ("why does this old backup have no tier chip"). `03-Proposed-Architecture.md`'s "not derived retroactively from a flat file listing" is about not reverse-engineering tags from scanning the filesystem after the fact, it isn't an objection to using the accurate `started_at`/`finished_at` timestamps Savepoint already recorded for these runs. So: on every startup, `retention.reconcile_all(settings)` walks each target's successful runs in chronological order and tags any that don't have tags yet, using the exact same logic that would have run live. This is naturally idempotent, once a target's history is fully tagged, the reconciliation query finds nothing to do and it's a no-op on every subsequent boot, no separate "have I already backfilled" flag is needed. Tagging always runs for every target, confirmed or not, so tier chips are correct in the UI immediately regardless of the confirmation gate below.
- **Pruning is gated behind an explicit, per-target confirmation, replacing the earlier "prune automatically on first boot" approach.** The original plan had `reconcile_all()` tag *and* prune every target on the very first startup after deploying this phase. The risk there: combining default retention counts nobody had explicitly chosen yet with immediate bulk deletion of real, already-accumulated history, on the very first boot, with no operator in the loop at all. A target with 40 existing daily backups and the untouched default of 7 would lose 33 files before anyone had looked at a settings page. Per-target confirmation removes that risk while keeping tagging immediate and universal: a new `targets.retention_confirmed` column (`INTEGER NOT NULL DEFAULT 0`, same guarded `ALTER TABLE ADD COLUMN` pattern as the other Phase 4 columns) gates `prune_target()`, not `compute_tags_for_run()`/tagging. `reconcile_all()` backfills tags for every target's untagged successful runs unconditionally, but only calls `prune_target()` for targets where `retention_confirmed = 1`. The *only* thing that ever sets `retention_confirmed = 1` is saving the Retention form via `POST /targets/{id}/retention`, whether the operator changes the numbers or just re-submits the defaults, there is no other path to true. Until then, the operator can see exactly what retention *would* do (which runs hold which tags right now) with zero risk of a file disappearing, and pruning only ever starts for a target once someone has actually looked at its settings and hit save.
- **A tagged run whose file has already gone missing from disk does not block pruning.** If a backup's tags age out and it's due for physical deletion, `os.remove()` is wrapped to tolerate `FileNotFoundError` (log a warning, still clear the tag bookkeeping). The tag row is the source of truth for "is this still retained", not a stat() of the filesystem, so a file an operator deleted by hand doesn't leave that run stuck un-prunable forever.
- **A pruned run's `backup_runs` row and `file_path` are left alone, not nulled out.** The audit trail ("this ran, succeeded, here's what it produced") stays intact; "currently retained" is answered by whether any `backup_run_tags` rows still reference that run, computed via a join, not by mutating the original record of what happened.

## Project layout changes

```
app/
  retention.py               # NEW: compute_tags_for_run(), tag_and_prune(), prune_target(), reconcile_all()
  jobs.py                     # _execute()'s success branch calls retention.tag_and_prune() after finish_backup_run()
  main.py                      # calls retention.reconcile_all(settings) once at startup, after db.init_db()
  db.py                         # + retention_daily/weekly/monthly/retention_confirmed columns on targets, + backup_run_tags table,
                                 # + get_tags_for_run()/list_tag_counts()-style helpers used by retention.py
  routes/
    targets.py                  # POST /targets/{id}/retention (validate + save 3 counts, sets retention_confirmed = 1)
  templates/
    targets/detail.html           # new "Retention" section (3 number inputs), mirroring the existing "Schedule" section;
                                   # shows an "not yet active" notice when retention_confirmed is false
    partials/history_row.html       # tag chips (daily/weekly/monthly) rendered next to the status pill for tagged runs
schema.sql                          # targets: + retention_daily/weekly/monthly/retention_confirmed; + backup_run_tags table
```

## Data model changes (`schema.sql`)

```sql
-- targets gains (additive, guarded ALTER TABLE ADD COLUMN, same pattern as Phases 2-3):
--   retention_daily INTEGER NOT NULL DEFAULT 7
--   retention_weekly INTEGER NOT NULL DEFAULT 4
--   retention_monthly INTEGER NOT NULL DEFAULT 2
--   retention_confirmed INTEGER NOT NULL DEFAULT 0

CREATE TABLE IF NOT EXISTS backup_run_tags (
    backup_run_id INTEGER NOT NULL REFERENCES backup_runs(id),
    tier TEXT NOT NULL,
    tagged_at TEXT NOT NULL,
    PRIMARY KEY (backup_run_id, tier)
);
```

`db.py` gains: `update_target_retention(conn, target_id, daily, weekly, monthly)` (also sets `retention_confirmed = 1`, the only place that ever does), `add_backup_run_tag(conn, run_id, tier)`, `remove_backup_run_tag(conn, run_id, tier)`, `list_tagged_runs(conn, target_id, tier)` (ordered most-recent-first, for prune's "keep N" logic), `get_tags_for_run(conn, run_id)` / a batched equivalent for rendering the history table without an N+1 query per row, `list_successful_runs(conn, target_id)` (chronological, for both live tagging comparisons and startup backfill).

## Retention logic (`app/retention.py`)

- `compute_tags_for_run(conn, target_id, run) -> list[str]`: always includes `"daily"`. Includes `"weekly"` if no other run for this target already holds a `"weekly"` tag whose own `started_at` date falls in the same `(iso_year, iso_week)`. Includes `"monthly"` under the same logic for `(year, month)`. Comparison is against `started_at` specifically (see the note in Design decisions), computed in Python per the decision above.
- `tag_and_prune(conn, target, run_id, backup_target_dir)`: inserts the computed tags into `backup_run_tags`, then calls `prune_target(conn, target, backup_target_dir)` only if `target["retention_confirmed"]` is true. Called from `jobs.py::_execute()` on every live success regardless of confirmation, tagging always happens live, pruning is what's gated.
- `prune_target(conn, target, backup_target_dir)`: for each tier, fetch that target's tagged runs for the tier ordered newest-first, keep up to `retention_<tier>`, remove the tag row for the rest. After all three tiers are processed, for every run touched this pass, check whether it holds any tags at all, if none: delete its file (tolerating `FileNotFoundError`), log it. The `backup_runs` row and `file_path` are left as-is either way. Callers are responsible for the `retention_confirmed` check, `prune_target()` itself doesn't re-check it, so it stays a plain "prune this target now" primitive usable directly if a manual "prune now" action is ever added later.
- `reconcile_all(settings)`: opens its own connection (same pattern as `jobs.py`, this runs outside a request), for each target, walks `list_successful_runs()` in chronological order, calls `compute_tags_for_run`/inserts tags for any run with none yet (always, every target), then calls `prune_target()` once per target after its backfill, but only for targets where `retention_confirmed = 1`. Called once from `main.py` at startup, right after `db.init_db()`. Idempotent either way: a fully-tagged, unconfirmed target is a tag-backfill no-op every subsequent boot; a fully-tagged, confirmed target is both a tag no-op and a prune no-op once it's already down to its configured counts.

## Routes

- `POST /targets/{id}/retention` - new: validates `retention_daily`/`retention_weekly`/`retention_monthly` as positive integers, saves via `update_target_retention()`, which also sets `retention_confirmed = 1` unconditionally on every successful save, whether or not the submitted numbers differ from the defaults. No live scheduler interaction needed (unlike the schedule/window settings), retention counts and the confirmation flag are only read at prune time.
- `GET /targets/{id}` (existing route, template change only): the new Retention section reads `target["retention_confirmed"]`. While false, the three counts are still shown and editable (pre-filled with the current values, which are the defaults until changed) alongside a clear notice: "not yet active for this target, save to enable pruning (using these defaults if left unchanged)". Once true, the section is just the normal editable counts with no notice, saving again keeps it confirmed (there's no way to un-confirm from the UI, matching "there's no other path that sets it" for becoming true, and no scoped requirement to ever set it back to false).
- All other routes unchanged. The history partial's rendering gains tag chips but the route contract (what's passed into the template) only grows, it doesn't change shape.

## Verification plan

1. Run a backup for a target with no history yet, confirm it's tagged `daily` (and `weekly`/`monthly` too, being first-of-everything), and the history row shows all three tier chips.
2. Run a second backup for the same target later the same ISO week, confirm it's tagged `daily` only (not `weekly`, since the earlier run already holds it that week).
3. Manually backdate a couple of `backup_runs` rows (or wait across a real day/week boundary in the test LXC) to simulate several days/weeks of history, confirm only the most recent `retention_daily` runs keep the `daily` tag and the rest lose it, and confirm a run that loses its last tag actually has its file removed from the bind-mounted target directory.
4. Confirm a backup whose tags all age out but whose file was already deleted by hand doesn't error the prune sweep, just logs a warning and proceeds.
5. Set a target's retention counts to something other than the default via the new UI section, confirm the next prune sweep respects the new counts, and confirm the Retention section no longer shows the "not yet active" notice after saving.
6. Restart the Savepoint container against a state db carrying real Phase 1-3 history with no tags yet, for targets that have never had their Retention section saved. Confirm the startup reconciliation tags all of that history correctly in chronological order (tier chips show up in the UI immediately), but confirm nothing gets deleted and no files disappear, since `retention_confirmed` is false for all of them. Restart again immediately after, confirm tagging is still a no-op the second time and still nothing is pruned.
7. With more than one target carrying untagged, unconfirmed history from step 6, save the Retention form (accepting the defaults) for exactly one of them. Confirm only that target's excess history gets pruned, its `backup_run_tags`/files reduced to the configured counts, while every other target's history (tags already backfilled, but unconfirmed) remains fully intact, byte for byte, with no files removed.
8. Confirm `queued`/`skipped`/`failure` rows never appear in `backup_run_tags` and are never touched by pruning.
9. Unit tests (mocked, no live Docker): ISO week/month boundary correctness (in particular a run dated the last few days of December, confirming it's compared against ISO week 1 of the *next* year, not SQLite's non-ISO week numbering), "first of week/month" detection with existing tagged history present, `prune_target()` keeping exactly N per tier and only deleting a file once every tag is gone, the missing-file-tolerance path, `reconcile_all()`'s idempotency (running it twice produces no duplicate tags and no second round of deletions), and specifically that `reconcile_all()`/`tag_and_prune()` never call `prune_target()` for a target where `retention_confirmed` is false, only tagging does.

## Status

Plan drafted 2026-07-27, awaiting review and approval before build.
