# Savepoint - Phase 4.5 Plan: Target Management (Edit / Delete / Enable-Disable)

## Context

`Docs/04-Initial-Build-Plan.md` never scoped a way to edit or delete a target once created, or to pause its automated backups without dismantling its schedule/window membership entirely. This surfaced as a genuine gap during real-world testing of Phases 1-4, not a deferred feature: a target with a typo'd container name, or one whose underlying container gets renamed, currently has no fix short of manually editing the state db; and there's no way to say "stop backing this one up for a while" short of clearing its schedule and unjoining the window (losing that configuration in the process).

This phase adds three things to the existing target detail page: editing connection details, enabling/disabling automated runs, and deleting a target outright.

## Design decisions

- **Engine is not editable, ever.** Each engine has different required fields (`db_user`/`db_name` vs `file_path`) and a different adapter entirely; "changing" it is really "delete and recreate with a different shape", especially once history/tags already exist under the old engine's assumptions. The edit form shows the engine as read-only text (matching how it's already displayed), and doesn't submit an `engine` field at all, there's no server-side path that can change it.
- **Editing reuses the exact validation used at creation time, via one shared helper.** `create_target()` in `routes/targets.py` currently inlines: sqlite requires `file_path` / other engines require `db_user`+`db_name`, then `docker_client.get_container()` (404 -> clear error), then for sqlite `docker_client.path_exists_in_container()`. This becomes a small `_validate_connection_fields(client, engine, container_name, db_user, db_name, file_path) -> str | None` helper (returns an error message or `None`), called by both `create_target` and the new `POST /targets/{id}/edit`, so add and edit can never drift apart on what "valid" means. No new validation concept is introduced, this phase only reuses Phase 1/2's "verify at save time, not at first run" pattern a second time.
- **Deleting a target hard-deletes its `backup_runs`/`backup_run_tags` rows, it is not a soft-delete/archive flag.** This looks at first like it cuts against Phase 4's "leave a pruned run's row alone, don't null `file_path`" instinct, but the two situations aren't the same: Phase 4 preserves history *because the target is still actively tracked*, a pruned run is one chapter in an ongoing, still-meaningful story ("this specific backup aged out, the target's series continues"). Deleting the target is the operator saying "stop tracking this entirely", there is no ongoing story left for orphaned rows to be chapters of, and no page in this app (`/targets/{id}`) left to view them on without building a whole separate "archived targets" browsing feature, which isn't scoped here and isn't proportionate to a homelab single-operator tool (YAGNI: nobody has asked for undo-delete or archived-target browsing). A soft-delete flag would also mean every existing query that lists targets (`index.html`, discovery's already-tracked exclusion, the window settings member list) needs new filtering logic to stay correct, real complexity for a feature nobody's asked for. So: `delete_target(conn, target_id)` deletes, in order, `backup_run_tags` rows for that target's runs, then its `backup_runs` rows, then the `targets` row itself, in one connection (SQLite's default FK behavior blocks deleting a parent row while children still reference it, so the order matters and this can't be a single statement).
- **Backup files on disk are a separate, explicit, opt-in decision, never a silent side effect of deleting a target.** This matches the project's established caution around file deletion (it's the entire reason Phase 4's retention has a confirmation gate). The delete form shows a live count of backup files associated with the target and an unchecked-by-default checkbox: "also delete N backup files from disk". Leaving it unchecked deletes the target and its db bookkeeping but leaves the actual files in place under their existing `/backup-target/<target-name>/` folder, an "orphaned but still human-legible" state (the folder name alone still says what they were), not a "context is now unrecoverable" state. Checking it deletes the files first (tolerating an already-missing file the same way `retention.py` does), then proceeds with the same db cascade.
- **The displayed/deleted file count reflects files verified present on disk at delete time, not a raw count of historical `file_path` values.** `backup_runs.file_path` is deliberately left populated even after Phase 4's retention prunes the underlying file (that phase's own audit-trail decision), and a file can also have been removed by hand outside Savepoint entirely. Either way, a stale `file_path` value that no longer points to a real file would inflate "this will delete N files" with copies that are already gone, which is exactly the kind of confusing, untrustworthy confirmation-screen number this project's established caution around file deletion is trying to avoid. So both `count_target_files()` and `list_target_file_paths()` call `os.path.exists()` against each non-null `file_path` and only count/list the ones that actually still exist. `file_path` is already an absolute path as constructed by the adapters (it already embeds `BACKUP_TARGET_DIR`), so this is a direct `os.path.exists(file_path)` check, not a re-join of `BACKUP_TARGET_DIR` with `file_path` (which would double the prefix and check the wrong location).
- **Delete requires typed-name confirmation**, matching the project's own already-documented pattern for destructive actions (`03-Proposed-Architecture.md`'s restore workflow: "explicit confirmation step, type-to-confirm or similar, this is destructive"). The operator must type the target's exact current name into a text field; a mismatch re-renders the form with a clear error and does nothing. This is enforced server-side only, no client-side JS disabling of the submit button, consistent with Phase 2's "server-side validation is authoritative" precedent.
- **A delete request is rejected outright (not queued) if that target has a run in progress.** Queuing would need a pending-delete flag, logic to actually perform the deferred delete once the in-progress run's thread finishes, and handling for a second run starting in the meantime, real complexity for an edge case the operator can trivially work around by waiting a few minutes and retrying. `jobs.py` gains a read-only `is_in_progress(target_id) -> bool` (a peek at the existing `_in_progress` set under the existing lock, no mutation), used by the delete route; a positive check re-renders the confirmation form with a clear "a backup for this target is currently running, try again once it finishes" error, no partial deletion.
- **`enabled` gates automated dispatch only, never manual "run backup now".** Disabling is framed as "pause unattended automation", not a global kill-switch: there's no correctness/collision hazard in letting an operator deliberately click "run now" on a disabled target (unlike the per-target lock, which exists because *concurrent* execution is actually dangerous), so second-guessing an explicit, in-the-moment manual action would be paternalistic without a technical justification. `run_backup_route` (manual) is unchanged and unaffected by `enabled`.
- **Disabling actually unregisters the target's APScheduler cron job, it doesn't leave a job registered that no-ops when it fires.** `scheduler.sync_target_schedule(target)` already decides whether a job should exist based on `schedule_cron`; it now also requires `target["enabled"]` to be true before registering one, so a disabled target with a cron schedule has no job in APScheduler's job store at all until re-enabled, nothing to accidentally observe firing-and-doing-nothing in the scheduler's own job list. The new `POST /targets/{id}/toggle-enabled` route calls `sync_target_schedule()` again after flipping the flag, exactly like the existing schedule/window save flow already does.
- **`window_tick()` filters out disabled targets when building its queue**, even if `in_window = 1`: `members = [t for t in db_module.list_all_targets(conn) if t["in_window"] and t["enabled"]]`. Straightforward, stated explicitly here rather than left as an assumption.
- **Retention/pruning of a disabled target's existing history is completely unaffected.** `reconcile_all()`/`tag_and_prune()`/`prune_target()` never look at `enabled`, only `retention_confirmed`; disabling only pauses the *creation* of new successful runs via automation (manual runs still create them normally, and still get tagged/pruned through the identical `_execute()` path). No code change needed here, this is a statement of what already falls out of the design, not a new mechanism.
- **Editing connection details is not blocked by an in-progress run**, unlike delete. A `target` row is read once and passed by value into the adapter at dispatch time (a `sqlite3.Row` snapshot, not a live reference), so a concurrent edit can't corrupt or retroactively alter a backup that's already running. Only delete needs the in-progress check, because delete is the one action whose completion (the row disappearing entirely) is actually incompatible with a run that expects to read and then update that same row when it finishes.

## Project layout changes

```
app/
  jobs.py                # + is_in_progress(target_id), a read-only peek at _in_progress
  scheduler.py             # sync_target_schedule() also requires target['enabled']; + remove_target_job(target_id)
                            # (unconditional job unregistration, used by delete since there's no target row left
                            # afterward to re-check schedule_cron against)
  db.py                     # + enabled column + migration; update_target_connection(); update_target_enabled();
                              # count_target_files(); list_target_file_paths(); delete_target() (cascades
                              # backup_run_tags -> backup_runs -> targets)
  routes/
    targets.py                # POST /targets/{id}/edit (shares _validate_connection_fields with create_target);
                                # POST /targets/{id}/toggle-enabled; POST /targets/{id}/delete (typed-name confirm,
                                # optional file-deletion checkbox, in-progress rejection)
  templates/
    targets/detail.html         # Connection section becomes an editable form; enabled/disabled indicator + toggle
                                  # button near "Run backup now" with a one-line note on what disabling does; a
                                  # "Delete target" section (file count, checkbox, typed-name confirm)
    index.html                    # small "disabled" indicator next to paused targets
schema.sql                        # targets: + enabled
```

## Data model changes (`schema.sql`)

```sql
-- targets gains (additive, guarded ALTER TABLE ADD COLUMN, same pattern as Phases 2-4):
--   enabled INTEGER NOT NULL DEFAULT 1
```

No new tables. `db.py` gains:
- `update_target_connection(conn, target_id, container_name, db_user, db_name, file_path)`
- `update_target_enabled(conn, target_id, enabled: bool)`
- `count_target_files(conn, target_id) -> int`: iterates this target's `backup_runs.file_path` values and counts only those where `os.path.exists(file_path)` is true, not a raw count of non-null values.
- `list_target_file_paths(conn, target_id) -> list[str]`: same existence filter, used by delete when the file-deletion checkbox is checked, so it only ever attempts to remove files that are actually there (the `retention.py`-style `FileNotFoundError` tolerance stays as a second layer of defense for the race between checking and deleting, not the primary mechanism).
- `delete_target(conn, target_id)`: runs the three cascading deletes inside one explicit transaction, not relying on default per-statement autocommit: `BEGIN`, `DELETE FROM backup_run_tags WHERE backup_run_id IN (SELECT id FROM backup_runs WHERE target_id = ?)`, `DELETE FROM backup_runs WHERE target_id = ?`, `DELETE FROM targets WHERE id = ?`, `COMMIT`. This is the one place in the codebase where a crash between two of the three statements would otherwise leave `backup_run_tags` rows referencing an already-deleted `backup_runs` row (every other multi-statement sequence elsewhere in the app is either idempotent or safe to partially re-apply, this cascade isn't, so it gets the explicit transaction the others didn't need).

## Routes

- `POST /targets/{target_id}/edit` - validates via the shared `_validate_connection_fields()` helper (same rules as `create_target`, no `engine` field accepted at all), saves via `update_target_connection()`, redirects back to the detail page. On validation failure, re-renders the detail page with a clear error, matching the existing schedule/retention error-rendering pattern.
- `POST /targets/{target_id}/toggle-enabled` - flips `enabled`, calls `scheduler.sync_target_schedule()` with the updated target so a cron job is registered or unregistered to match, redirects back to the detail page.
- `POST /targets/{target_id}/delete` - reads `confirm_name` and an optional `delete_files` checkbox from the form. Rejects (re-renders with an error, no state change) if `jobs.is_in_progress(target_id)` or if `confirm_name` doesn't exactly match the target's current name. Otherwise: if `delete_files`, removes every file from `list_target_file_paths()` (tolerating `FileNotFoundError`, logged, same pattern as `retention.py`); calls `scheduler.remove_target_job(target_id)` unconditionally; calls `db.delete_target()`; redirects to `/` (the target's own page no longer exists).
- `GET /targets/{target_id}` (template change only) - detail page's Connection section becomes a form (mirroring Schedule/Retention's existing sections), gains the enabled/disabled toggle and the delete section.
- `GET /` (template change only) - shows a small "disabled" indicator for targets where `enabled` is false.

## Verification plan

1. Edit a target's container name to one that doesn't exist, confirm it's rejected with a clear error and the target's stored value is unchanged.
2. Edit a target's `db_user`/`db_name` (or `file_path` for a sqlite target) to valid new values, confirm they're saved and reflected on the detail page, and confirm there is no form field anywhere capable of changing the engine.
3. Disable a target that has a cron schedule, confirm its APScheduler job is gone from the scheduler's job list (not just inert), confirm a manual "run backup now" still works while disabled.
4. Confirm a disabled target that's also a window member is excluded from the next window's queue.
5. Confirm a disabled target's existing tagged history still ages out and prunes normally on schedule, unaffected by being disabled.
6. Re-enable the target from step 3, confirm its cron job is registered again and fires.
7. Start a backup for a target (or simulate one in progress), attempt to delete it, confirm rejection with a clear message and that nothing was removed; retry after it finishes, confirm the delete now succeeds.
8. Attempt to delete a target typing the wrong name, confirm rejection with no state change. Delete a target with the correct name and the file-deletion checkbox unchecked, confirm its `backup_runs`/`backup_run_tags` rows and the `targets` row are gone but its backup files remain on disk under their existing folder. Delete a second target with the checkbox checked, confirm both the db rows and its files are gone. Separately, against a target that's had at least one retention prune (so some of its `backup_runs.file_path` values point at already-deleted files), confirm the delete confirmation screen's file count matches the number of files actually still present on disk, not the raw count of historical `file_path` values, i.e. already-pruned files are correctly excluded from the count.
9. Confirm a deleted target's container name is immediately eligible for re-discovery (no longer excluded by `/discover`'s already-tracked check).
10. Unit tests (mocked, no live Docker): `_validate_connection_fields()` covering both the create and edit call sites; `count_target_files()`/`list_target_file_paths()` correctly excluding a `file_path` that's set but points at a non-existent file (simulating a prior retention prune) alongside one that's genuinely still on disk; `delete_target()`'s cascade ordering (tags, then runs, then the target row, verified via direct queries) and that it's wrapped in a single transaction; the in-progress rejection path; the file-deletion checkbox's on/off behavior; `sync_target_schedule()` unregistering a job when `enabled` flips false and re-registering when it flips true; `window_tick()` excluding a disabled member; a manual run succeeding against a disabled target.

## Real-world verification results

The verification plan above was run on the throwaway test LXC, not just the mocked route-level tests recorded in `phase-4.5-build-summary.md`:

- Edit correctly rejects a container name that doesn't exist, accepts a valid rename, and a subsequent manual run against the corrected target succeeds: confirmed, tested against a real `docker rename` of the MariaDB test container. A manual run against the stale name failed cleanly first, proving the fix was genuinely needed rather than validated against a case that could never actually occur.
- Disabling a target with an active cron schedule genuinely prevents it from firing at its scheduled time, not just removing the job from an in-memory list immediately after toggling: confirmed by waiting past a real ~2 minute cron fire time while disabled, nothing ran.
- Re-enabling restores the cron job and it fires again as expected: confirmed.
- The delete confirmation's file count matches files genuinely present on disk: confirmed, tested against a real target with prior retention prunes (8 files shown, 8 files actually present on disk). The file-existence filtering from the plan amendment holds correctly against a real prune history, not just synthetic test data.
- Deleting with the file-deletion checkbox checked removes every file from the target's backup-target folder: confirmed, folder left empty.
- A deleted target's container is immediately eligible for re-discovery, and recreating a target under the same name correctly lands new backups in the same existing folder: confirmed, expected since storage paths are name-derived, not id-derived.

### Known follow-up, not yet fixed

`backup_runs` timestamps (`started_at`, `finished_at`) are correctly stored as UTC via `db.py`'s `now_iso()`, storage is correct and should not change. But templates (`partials/history_row.html` and any other template printing these fields) render the raw stored UTC string with no conversion to local time, so the UI currently shows times 2 hours behind the container's actual local time (Europe/Prague, CEST in summer). This is a display-only gap: a Jinja filter needs to convert stored UTC timestamps to local time (via the `TZ` env var and `zoneinfo`) before rendering. Not fixed in this phase, flagged here so it isn't lost, to be picked up as its own small fix.

## Status

Plan approved 2026-07-27, cleared to build. Fully verified against real Docker containers on the test LXC and closed 2026-07-27.
