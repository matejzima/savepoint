# Savepoint - Phase 5 Build Summary

Built against [phase-5-plan.md](phase-5-plan.md) (approved as amended: drop-and-recreate-tables instead of drop-and-recreate-database for MySQL/MariaDB restore, explicit in-container temp-file cleanup, raw-copy tag shown in the restore dropdown).

## What was built

### Restore workflow

- **Schema**: new `restore_runs` table (`target_id`, `backup_run_id`, `started_at`, `finished_at`, `status`, `stopped_container`, `error_message`), created via the same `CREATE TABLE IF NOT EXISTS` pattern as every other table, no migration needed since it's a fresh table.
- **`adapters/base.py`**: new `RestoreResult` dataclass (`success`, `error_message`); `Adapter.restore()`'s return type updated from `None` to `RestoreResult`.
- **`docker_client.py`**: `put_archive_file()` (pushes a local file into a container at a full path, the inverse of `get_archive_file()`), `stop_container()`, `start_container()`.
- **Per-engine `restore()`**, replacing the `NotImplementedError` stubs:
  - **Postgres**: pushes the `.dump` file to `/tmp/savepoint-restore.dump`, runs `pg_restore -U <user> -d <db> --clean --if-exists <path>`, removes the temp file afterward regardless of outcome.
  - **MySQL/MariaDB**: pushes the `.sql` file to `/tmp/savepoint-restore.sql`, then **drops every existing table in the target database (not the database itself)** before sourcing the dump, via `--execute="source ..."` on the client. Table dropping queries `information_schema.tables`, disables `FOREIGN_KEY_CHECKS`, drops the collected tables in one statement, and re-enables `FOREIGN_KEY_CHECKS` from a `finally` block, so a failed drop can never leave checks silently disabled. New `restore_client_binary` class attribute (`mysql` / `mariadb`) mirrors the `dump_binary` split from Phase 2. Temp file removed afterward regardless of outcome.
  - **SQLite**: `put_archive_file()` straight onto the target's existing `file_path`, no temp staging path and nothing to clean up.
- **`app/restore.py`** (new): `perform_restore()` is the one place that ever calls an adapter's `restore()`. Handles the optional stop/start (only ever requested for SQLite), records the `restore_runs` outcome, and fires `notifications.notify_restore_result()` on both success and failure. If the requested stop fails, the restore is marked `failure` before touching the file; if only the post-restore start fails, the restore stays `success` but the message calls out that the container needs a manual start.
- **`jobs.py`**: `execute_restore_claimed()` mirrors `execute_claimed()`'s manual-backup pattern (claim was already taken by the route, this runs the restore and releases). Defensively handles a vanished target or backup run the same way `execute_claimed()` does, even though the shared per-target lock makes that race effectively unreachable in practice.
- **`scheduler.py`**: `dispatch_restore()` mirrors `dispatch_manual()`, a one-off APScheduler job.
- **`notifications.py`**: `notify_restore_result()` fires on both outcomes (backup notifications stay failure-only, unchanged).
- **`db.py`**: `create_restore_run()`, `finish_restore_run()`, `list_restore_runs()` (joined with the backup run's `started_at` for display), `list_eligible_backups_for_restore()` (this target's successful runs whose file is verified `os.path.exists()`, same principle Phase 4.5 established for delete's file count). `delete_target()` now also cascades `restore_runs`.
- **`routes/targets.py`**: `POST /targets/{id}/restore` claims the target, validates the typed confirmation name and that the chosen backup is still eligible, then dispatches and returns immediately (HTMX polling on `#restore-history-table` picks up completion). Validation/collision failures are reported as a `notice` inside the restore-history partial, the same idiom `run_backup_route` already uses for its own collision case, rather than a full-page error. The "stop container" checkbox is only ever honored for SQLite targets, silently ignored otherwise, regardless of what a client submits. `_detail_context()` now also supplies `eligible_backups` and `restore_runs`.
- **`routes/history.py`**: `GET /targets/{id}/restore-history` polling endpoint, same shape as the existing backup history endpoint.
- **Templates**: new Restore section on the target detail page (dropdown of eligible backups, showing the "raw copy, not live-consistent" tag inline for SQLite backups with `method = "raw-copy"`; a stop-container checkbox for SQLite or a static overwrite warning for the other engines; typed-name confirmation), new `partials/restore_history.html` polling partial.

### Local-time display fix

- **`app/deps.py`**: `_local_time()` Jinja filter, registered once on the shared `templates` instance. Converts a stored UTC ISO string to the container's local time via `zoneinfo` (reading `TZ` from the environment, falling back to UTC if unset), formatted as `%Y-%m-%d %H:%M:%S %Z`. Storage is untouched, this only changes rendering.
- Applied to `partials/history_row.html`, `index.html`, and the new `partials/restore_history.html`.

## Deviations from the plan, and why

- **FOREIGN_KEY_CHECKS re-enabling uses three separate client invocations wrapped in a Python `try`/`finally`, not one multi-statement `--execute` string.** The plan described "wrapping the drop sequence" with the two `SET` statements; the mysql/mariadb client aborts a batched multi-statement `--execute` on its first error (unless `--force` is passed), which would have left `FOREIGN_KEY_CHECKS` disabled if the drop itself failed, exactly the silent hazard the plan called out. Issuing "FK off", "drop", "FK on" as three separate exec calls with the re-enable in a `finally` guarantees it always re-runs, drop success or failure. Verified by a unit test (`test_restore_reenables_foreign_key_checks_even_when_drop_fails`) and confirmed the sanity script's real MariaDB-shaped flow issues exactly this three-call sequence.
- **Two `test_jobs.py` edge-case tests ended up mocking `db.get_target`/`db.get_backup_run` rather than constructing real rows.** The originally-planned approach (create a `restore_runs` row, then delete its target or point it at a nonexistent backup run) collides with the schema's own foreign key constraints, both are enforced with `PRAGMA foreign_keys = ON`, and `delete_target()` now cascades `restore_runs` too, so a target can't actually be deleted out from under a live restore_runs row, and a restore_runs row can't reference a backup_run that was never created. These code paths are defensive symmetry with `execute_claimed()`'s own "target is None" check (protected by the same per-target lock in real use), so the tests mock the lookup instead of fighting the database's own integrity guarantees.

## Testing performed

- `pytest tests/` - 118/118 pass (78 from Phases 1-4.5, 40 new: adapter `restore()` tests for all three engines including the MariaDB-client-binary and FK-toggle-survives-failure cases, `docker_client` restore primitives, `app/restore.py`'s `perform_restore()` across success/failure/stop-fails/start-fails, `jobs.execute_restore_claimed()`, `db.py`'s restore_runs helpers and eligibility filtering and delete cascade, `notifications.notify_restore_result()`, the `_local_time` filter including a DST-boundary check, and the new restore route's validation/dispatch paths).
- Full app boot + end-to-end flow via `TestClient` as a context manager (real scheduler, mocked Docker):
  - Postgres: created a target, backed a real file onto disk, confirmed the detail page renders the restore dropdown and local time with a `CEST` zone abbreviation, posted a restore, confirmed `put_archive_file` was called with the right source file and `pg_restore ... --clean --if-exists` was the command run, confirmed the `restore_runs` row landed as `success`.
  - MariaDB: same flow, confirmed the `mariadb` client binary was used (not `mysql`), confirmed the exact FK-off/drop-two-tables/FK-on/source/cleanup sequence of exec calls.
  - SQLite: confirmed the "raw copy, not live-consistent" tag renders in the restore dropdown for a `method="raw-copy"` backup, confirmed `stop_container`/`start_container` were both called around a restore with the checkbox checked, confirmed `stopped_container` was recorded as `1`.
  - Confirmed a wrong-name restore attempt is rejected with no new `restore_runs` row.

## Not tested here (needs the real homelab Docker host)

No Docker daemon was used in this dev environment; all Docker interaction was mocked. Before considering Phase 5 done, worth confirming on the real homelab host, per the plan's verification list:
1. A real Postgres restore against `savepoint-test-postgres` (or equivalent), confirming actual data is replaced, not merged.
2. A real MySQL and MariaDB restore, specifically after adding extra post-backup tables/rows, confirming the drop-and-recreate-tables step actually removes them and that it works with the same minimal-privilege `db_user` credentials backups already use (no elevated grants required).
3. A real SQLite restore with the container actually running throughout (`stop_container` unchecked), confirming it still completes.
4. Confirming no leftover temp file remains in a real target container after both a successful and a forced-failure restore (`/tmp/savepoint-restore.dump` / `.sql`).
5. Confirming a real ntfy notification fires for both a successful and a failed restore, and that the message content is useful at a glance.
6. Confirming the local-time display genuinely matches the homelab host's wall clock, not just the sanity script's synthetic `TZ` override.

## Status

Built and tested 2026-07-28. Awaiting real-world verification on the homelab Docker host before closeout.
