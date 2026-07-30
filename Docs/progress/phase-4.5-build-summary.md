# Savepoint - Phase 4.5 Build Summary

Built against [phase-4.5-plan.md](phase-4.5-plan.md) (approved as amended: file counts verify actual on-disk existence, `delete_target()` runs as one transaction).

## What was built

- **Schema**: `targets` gained `enabled` (`INTEGER NOT NULL DEFAULT 1`), migrated in place via the same guarded `ALTER TABLE ADD COLUMN` pattern as every prior phase.
- **`_validate_connection_fields()`** (`routes/targets.py`): the exact validation `create_target()` already did (sqlite requires `file_path` / other engines require `db_user`+`db_name`, then container existence, then sqlite file existence) factored into one helper shared by `create_target` and the new edit route, so add and edit can't drift apart on what "valid" means.
- **`POST /targets/{id}/edit`**: validates via the shared helper, saves via `db.update_target_connection()`. No `engine` field is accepted anywhere in the request, matching "engine is not editable, ever".
- **`POST /targets/{id}/toggle-enabled`**: flips `enabled`, calls `scheduler.sync_target_schedule()` immediately after so a cron job is registered or unregistered to match.
- **`POST /targets/{id}/delete`**: rejects (re-renders with an error, no state change) if `jobs.is_in_progress(target_id)` (new read-only peek at the existing lock, added right next to `try_claim`/`release`) or if the typed `confirm_name` doesn't match. Otherwise, if the `delete_files` checkbox was checked, removes every file from `db.list_target_file_paths()` (tolerating `FileNotFoundError` as a second layer of defense), unconditionally unregisters any scheduler job via the new `scheduler.remove_target_job()`, then calls `db.delete_target()`.
- **`db.count_target_files()` / `db.list_target_file_paths()`**: both verify actual `os.path.exists()` for each distinct `file_path`, not a raw count of non-null values, per the amendment, since Phase 4's retention deliberately leaves `file_path` populated after a prune.
- **`db.delete_target()`**: cascades `backup_run_tags` → `backup_runs` → `targets` with a single `commit()` at the end (no intermediate commits), relying on sqlite3's default deferred-transaction mode rather than an explicit `BEGIN`, see deviation below.
- **`sync_target_schedule()`** now requires `target["enabled"]` in addition to `schedule_cron` before registering a job; **`window_tick()`** now filters `t["in_window"] and t["enabled"]` when building its queue. Retention/pruning code was untouched, exactly as planned, it never looked at `enabled` and still doesn't.
- **UI**: detail page gained an editable Connection section (engine shown read-only), a Disable/Enable button next to "Run backup now" with a one-line notice when disabled, and a Delete section (live file count, opt-in "also delete N files" checkbox, typed-name confirmation). Index page shows a small "disabled" chip next to paused targets.
- Every route that re-renders `targets/detail.html` now goes through one shared `_detail_context()` helper (`routes/targets.py`, imported by `routes/history.py`) that fills in all the context keys the template needs (`tags_by_run`, `file_count`, all four error fields defaulting to `None`) so no render call site can omit one and hit a Jinja `Undefined` crash, the exact class of bug a missing `tags_by_run` caused back in Phase 3.
- Tests: `tests/test_target_management.py` (new, 7 cases: file-existence filtering including a simulated prior-prune case, delete cascade and cross-target isolation, connection/enabled db helpers, `is_in_progress` read-only behavior), plus new cases in `tests/test_scheduler.py` (enabled gating `sync_target_schedule`, `remove_target_job`, `window_tick` excluding a disabled member), `tests/test_jobs.py` (manual run succeeding against a disabled target), and `tests/test_targets_routes.py` (edit validation/save, toggle-enabled, delete's in-progress/name-mismatch rejections, both file-checkbox states, and the verified-file-count rendering).

## Deviations from the plan, and why

- **No explicit `BEGIN` statement in `delete_target()`.** The plan called for `BEGIN`/`COMMIT` around the three cascading deletes. Python's `sqlite3` module defaults to a deferred isolation mode that already opens an implicit transaction before the first DML statement in a connection and holds it open until `commit()`/`rollback()`, exactly the same pattern every other multi-statement function in `db.py` already relies on (e.g. `finish_backup_run`). Adding an explicit `BEGIN` on top risked a "cannot start a transaction within a transaction" error depending on connection state, for no behavioral difference, three `execute()` calls followed by one `commit()` (never intermediate commits) already gives the same atomicity guarantee the plan was asking for. Verified this holds by checking that a `delete_target()` call leaves both `backup_runs` and `backup_run_tags` empty together, never one without the other.
- **`routes/history.py` now imports `_detail_context` from `routes/targets.py`.** Not explicitly called out in the plan's project-layout section, but a direct consequence of centralizing detail-page context in one place (itself explicitly planned), rather than duplicating the same six-key dict across `history.py`'s `target_detail` and every error-rendering branch in `targets.py`.

## Testing performed

- `pytest tests/` - 78/78 pass (59 from Phases 1-4, 7 new in `test_target_management.py`, 3 new in `test_scheduler.py`, 1 new in `test_jobs.py`, 8 new in `test_targets_routes.py`). One pre-existing test (`test_sync_target_schedule_adds_updates_and_removes_job`) needed its plain-dict fixtures updated to include an `"enabled"` key, since `sync_target_schedule` now reads it unconditionally, real `sqlite3.Row` targets always have the column, only the test's hand-built dicts didn't.
- Booted the app against a hand-built Phase-4-shaped state db (no `enabled` column, one target with a real successful, tagged run) and confirmed the column migrates in defaulting to `1`, the pre-existing target/history/tags are untouched.
- Full route-level flow via `TestClient` used as a context manager (so the real scheduler starts) with Docker mocked:
  - Detail page renders the connection form and the correct (existence-verified) file count.
  - Saved a cron schedule, confirmed the APScheduler job exists; disabled the target, confirmed the job is gone (not just inert); re-enabled, confirmed it's back.
  - Edited connection details to a new container/user/db, confirmed saved and reflected.
  - Delete rejected while `jobs.is_in_progress()` is true, rejected on a name mismatch, neither left any state changed.
  - Deleted a target with the file checkbox unchecked: db rows and scheduler job gone, backup file left on disk. Deleted a second target with the checkbox checked: file also removed.
  - Confirmed a deleted target's container name is immediately eligible for `/discover` again.

## Not tested here (needs the real homelab Docker host)

No Docker daemon was used in this dev environment; all Docker interaction was mocked. Before considering Phase 4.5 done, worth confirming on the real homelab host:
1. Editing a real target's container name to point at an actually-renamed container, end to end, not just the mocked validation path.
2. That disabling a target with an active cron schedule genuinely stops it from firing overnight (not just that the job disappears from APScheduler's in-memory job list immediately after the toggle).
3. Deleting a target that has a large number of real backup files, with the checkbox checked, to confirm the deletion pass completes in reasonable time and every file actually disappears from the bind-mounted directory.
