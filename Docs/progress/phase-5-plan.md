# Savepoint - Phase 5 Plan: Restore Workflow (+ Local-Time Display Fix)

## Context

Phases 1-4.5 built everything up to and around backups, but there is still no way to actually use one: `Adapter.restore()` has been a `NotImplementedError` stub since Phase 1 in every adapter. `Docs/04-Initial-Build-Plan.md`'s Phase 5 scope is exactly this: pick a backup from history, explicit confirmation, restore execution per adapter, and safety handling for live-vs-stopped targets where the engine matters.

Bundled into this phase per explicit request: the known display-only timestamp bug flagged at the end of Phase 4.5's closeout (`backup_runs.started_at`/`finished_at` are correctly stored as UTC, but rendered with no local-time conversion, so the UI reads 2 hours behind the container's actual `Europe/Prague` time). Storage stays UTC, only display changes.

## Design decisions - restore

- **One execution path, reusing the exact per-target lock backups already use, not a separate lock.** A restore and a backup (of any trigger source) against the same target are mutually exclusive for the same underlying reason the Phase 3 lock exists at all: two operations touching the same database at once is the hazard, not "two backups specifically." So restore claims the target via the *same* `jobs.try_claim()`/`_in_progress` set, not a second lock. This falls out for free: a scheduled/window backup that collides with an in-progress restore is already handled exactly like any other collision (a `skipped` row, no code change needed there).
- **Restore is dispatch, not inline, mirroring manual backup exactly.** `POST /targets/{id}/restore` synchronously claims the target (reject with a clear message if already busy, same as delete's in-progress rejection, but claiming rather than merely peeking, since restore needs to hold the lock for its own entire duration, not just check-then-proceed), synchronously creates a `restore_runs` row (`status = "running"`), and dispatches the actual work to APScheduler via a new `scheduler.dispatch_restore()` (same one-off-job pattern as `dispatch_manual`). The request returns immediately with a polling-capable status view, exactly like backup's `#history-table` pattern from Phase 3.
- **New `app/restore.py`, mirroring `app/retention.py`'s role.** `jobs.py` stays generic execution plumbing (claim, dispatch, release); `retention.py` holds backup-lifecycle domain logic; `restore.py` holds restore-lifecycle domain logic: `perform_restore(conn, target, restore_run_id, backup_run, stop_container)` is the one place that ever calls `adapter.restore()`, records the `restore_runs` outcome, handles the optional container stop/start, and fires notifications.
- **Confirmation reuses the typed-name pattern from delete**, not a new UX: the operator must type the target's current name to confirm, enforced server-side only (no JS), consistent with `03-Proposed-Architecture.md`'s "explicit confirmation step, type-to-confirm or similar, this is destructive" and Phase 4.5's precedent.
- **Only eligible backups are offered**: the restore form's dropdown lists this target's `success` runs whose file is verified present on disk right now (same `os.path.exists()` principle Phase 4.5 established for delete's file count, not a raw list of historical rows), so picking one can't fail for a reason the UI already knew about. One shared dropdown-based form rather than a "Restore" button per history row: with potentially many rows, N duplicate inline forms (each needing its own confirm-name field) would be far more visual clutter for no real benefit over one form where the operator picks from a list. Eligible SQLite backups whose `backup_runs.method` is `"raw-copy"` (Phase 2's fallback technique, already tracked and already shown as a tag in the history table) show the same "raw copy, not live-consistent" tag inline in the dropdown option text, so the operator sees that caveat at the moment they're choosing what to restore from, not only after the fact in history.
- **Per-engine restore mechanics, all via the same Docker archive/exec primitives already in `docker_client.py`**: the backup file lives in Savepoint's own bind-mounted `backup-target` directory, not inside the target container, so it has to be pushed in before the engine's restore tool can read it. This mirrors `get_archive_file()` (already used by the SQLite adapter to pull a file *out*) in the opposite direction, a new `docker_client.put_archive_file(client, container_name, dest_dir, local_path)` wraps the local file in an in-memory tar (via `tarfile`, same approach as `get_archive_file`) and calls `container.put_archive()`.
  - **Postgres**: push the `.dump` file to a fixed temp path, then `pg_restore -U user -d dbname --clean --if-exists /tmp/savepoint-restore.dump`, `PGPASSWORD` via environment exactly like `pg_dump`. `--clean --if-exists` drops existing objects before recreating them, which is exactly why Phase 1 chose the custom dump format (`-Fc`) in the first place, `pg_restore` expects it and this flag combination gives clean "replace with backup's contents" semantics without dropping/recreating the whole database. No privilege gap here: `--clean --if-exists` only drops the objects it owns within the target database, well within the same scoped grant backups already use. The temp file is removed from inside the container after the restore attempt completes, success or failure, mirroring the partial-file cleanup discipline the backup adapters already follow.
  - **MySQL/MariaDB**: `mysqldump`'s default output (Phase 2's choice, no `--add-drop-table`) contains no `DROP TABLE` statements, so sourcing it into a database that already has *any* data would hit "table already exists"/duplicate-key errors. The original draft of this plan proposed `DROP DATABASE IF EXISTS <db>; CREATE DATABASE <db>;` before sourcing the dump, mirroring Postgres's "replace, don't merge" guarantee. **Found during plan review, not during a failed build: this doesn't work with the credentials backups already rely on.** The official MySQL/MariaDB images' `MYSQL_USER` grant pattern is `GRANT ALL PRIVILEGES ON <db>.*`, a table-level grant scoped to that one database, not the instance-level `CREATE`/`DROP` privilege that dropping and recreating a database itself requires. Using the same `db_user` backups already use, `DROP DATABASE` would fail with a permissions error, and restore has no reason to demand broader credentials than backup ever needed.

    Replaced with **drop-and-recreate-tables**, which stays within the app user's existing scoped grant: query `information_schema.tables` for every table currently in the target database (using the same client binary/credentials already resolved for restore), wrap the drop sequence with `SET FOREIGN_KEY_CHECKS=0` before and `SET FOREIGN_KEY_CHECKS=1` after (so drop order doesn't have to respect foreign key dependencies), `DROP TABLE IF EXISTS` for each one, then source the dump exactly as already planned. This keeps restore working with the same minimal-privilege user philosophy backups already established, rather than quietly making restore depend on root credentials a target's backups never needed. Sourcing itself uses the client's own `--execute="source /tmp/savepoint-restore.sql"` (a real command the `mysql`/`mariadb` CLI supports natively), not shell redirection, keeping `docker_client`'s argv-list-only execution style (no `sh -c` anywhere in this codebase, restore doesn't introduce the first one). The temp `.sql` file is removed from inside the container after the restore attempt completes, success or failure.
  - **The MySQL/MariaDB *client* binary needs the same per-adapter fix Phase 2 already needed for the *dump* binary.** Recent MariaDB images ship `mariadb` as the client, with `mysql` only a deprecated/sometimes-absent compat symlink, exactly the `mariadb-dump` vs `mysqldump` situation from Phase 2's real bug. A new `restore_client_binary` class attribute (`MySQLAdapter.restore_client_binary = "mysql"`, `MariaDBAdapter.restore_client_binary = "mariadb"`) avoids repeating that exact mistake on the restore side.
  - **SQLite**: no server process is involved in a restore at all, `put_archive()` writes directly over the target's existing file path. This is also why SQLite is the only engine where "stop the container first" is a meaningful option, see below. No temp file to clean up here, the pushed file *is* the target's own file_path, there's no separate staging path.
- **"Stop the container during restore" is a SQLite-only, opt-in, default-checked option, not offered for the server engines.** For Postgres/MySQL/MariaDB the database *server* must stay running for `pg_restore`/`mysql` to connect at all, "stop it first" would make the restore mechanism itself impossible, so the UI never shows this option for those engines, it shows a clear static warning instead ("this will overwrite the live database, make sure nothing else is actively using it"), matching the architecture doc's fallback clause for engines where restoring against a stopped instance "doesn't" apply. For SQLite, Docker's archive API can overwrite the file whether the container is running or stopped, but stopping first genuinely avoids the app holding an open handle mid-write during the overwrite, a real, bounded, clearly-communicated safety improvement, so it defaults to checked (recommended) with a one-line explanation, and the operator can uncheck it (e.g. a throwaway test container nothing else depends on). If the requested stop or the post-restore start fails, the restore is marked `failure` (if the stop failed, before touching the file at all) or the success message explicitly calls out that the container needs to be started manually (if only the post-restore start failed), rather than silently proceeding past a safety step the operator explicitly asked for.
- **`RestoreResult` is a small new dataclass in `adapters/base.py`** (`success: bool`, `error_message: str | None`), not a reuse of `BackupResult`, since restore has no file path/size/method to report, reusing `BackupResult` would just mean carrying three always-`None` fields around.
- **ntfy notifies on both success and failure for restore, unlike backup (failure-only).** Backups are routine and automated; a nightly success ping would be noise, already decided against in Phase 3. Restore is the opposite: rare, manual, deliberate, and the operator may not be staring at the screen for the whole duration of a multi-minute restore, they're actively trying to recover from something and want to know it's actually done, good or bad, without having to keep the tab open. `notifications.notify_restore_result(target, backup_run, success, error_message)` covers both cases in one function.
- **Deleting a target now also cascades `restore_runs`.** `db.delete_target()` (Phase 4.5) gains a fourth delete statement, same reasoning as the original three: no page left to browse restore history for a target that no longer exists.

## Design decisions - local-time display fix

- **Storage stays UTC, only rendering changes.** `db.py::now_iso()` is correct as-is and untouched, changing stored data would be a real regression (ordering, comparisons, and every existing ISO-format assumption in `retention.py`/tests depend on it).
- **A Jinja filter, registered once on the shared `templates` instance in `app/deps.py`**, converts a stored UTC ISO string to local time for display: `datetime.fromisoformat(value).astimezone(ZoneInfo(os.environ.get("TZ", "UTC")))`, formatted as `%Y-%m-%d %H:%M:%S %Z` (keeps the sortable year-first ordering, adds the zone abbreviation so it's unambiguous this is local time, not UTC, directly addressing the confusion the bug caused). No new dependency: Python's stdlib `zoneinfo` (3.9+) uses the system's tzdata, which Phase 3's Dockerfile fix already installs; this only works because of that earlier fix, worth noting as the connection between the two phases. Falls back to `"UTC"` if `TZ` is unset (safe, matches what's actually stored, never guesses).
- **Read directly from `os.environ` inside the filter, not threaded through `Settings`.** `TZ` doesn't change during a container's lifetime in practice, and adding a field to the `Settings` dataclass purely to satisfy one display filter's lookup would be one more thing to keep in sync for no behavioral benefit over a direct read.
- **Applied everywhere a stored timestamp currently reaches a template**: `partials/history_row.html` (`started_at`, `finished_at`, explicitly named in the bug report), `index.html` (`latest_run_started_at`, same underlying value, same bug), and the new restore-history partial this phase adds (built correctly from the start rather than needing its own follow-up).

## Project layout changes

```
app/
  adapters/
    base.py                  # + RestoreResult dataclass; restore() signature returns RestoreResult
    postgres.py                # restore(): put_archive_file() the dump in, pg_restore --clean --if-exists, cleanup
    mysql.py                    # + restore_client_binary per subclass; restore(): drop+recreate tables (FK checks
                                 # off/on around it, not drop+recreate database, see privilege note above), source
                                 # the dump, then remove the temp .sql file from inside the container
    sqlite.py                     # restore(): put_archive_file() straight onto the target's own file_path
  restore.py                       # NEW: perform_restore() - the one place adapter.restore() is called, records
                                     # restore_runs, orchestrates optional container stop/start, fires notifications
  jobs.py                            # + execute_restore_claimed(), mirroring execute_claimed() for manual backups
  scheduler.py                        # + dispatch_restore(), mirroring dispatch_manual()
  docker_client.py                     # + put_archive_file(), stop_container(), start_container()
  notifications.py                      # + notify_restore_result() (fires on both success and failure)
  db.py                                  # + restore_runs table helpers; delete_target() cascades restore_runs too
  deps.py                                  # registers the local-time Jinja filter on `templates`
  routes/
    targets.py                              # POST /targets/{id}/restore
    history.py                               # GET /targets/{id}/restore-history (polling), restore dropdown data
  templates/
    targets/detail.html                        # Restore section: dropdown of eligible backups, per-engine warning
                                                 # or stop-container checkbox, typed-name confirm
    partials/restore_history.html                # NEW: past restore attempts, same polling pattern as backup history
    partials/history_row.html                      # timestamps through the new local-time filter
    index.html                                       # same
schema.sql                                             # + restore_runs table
```

## Data model changes (`schema.sql`)

```sql
CREATE TABLE IF NOT EXISTS restore_runs (
    id INTEGER PRIMARY KEY,
    target_id INTEGER NOT NULL REFERENCES targets(id),
    backup_run_id INTEGER NOT NULL REFERENCES backup_runs(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',  -- running | success | failure
    stopped_container INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);
```

A fresh table, no migration needed beyond `CREATE TABLE IF NOT EXISTS` already being how every table in this schema is defined. `db.py` gains: `create_restore_run()`, `finish_restore_run()`, `list_restore_runs(conn, target_id)`, `list_eligible_backups_for_restore(conn, target_id)` (this target's `success` runs, filtered to `os.path.exists(file_path)`, same existence-checking principle as Phase 4.5's file count). `delete_target()` gains a `DELETE FROM restore_runs WHERE target_id = ?` alongside its existing three statements, same single-transaction shape.

## Routes

- `POST /targets/{target_id}/restore` - reads `backup_run_id`, `confirm_name`, and (sqlite only) `stop_container`. Rejects if `jobs.try_claim()` fails (target busy) or `confirm_name` doesn't match. Otherwise creates the `restore_runs` row and dispatches `scheduler.dispatch_restore()`, returns the restore-history partial immediately (polling picks up the rest).
- `GET /targets/{target_id}/restore-history` - new polling endpoint, same shape as the existing `GET /targets/{id}/history`.
- `GET /targets/{target_id}` (template change only) - gains the Restore section (eligible-backups dropdown, showing the raw-copy tag inline for eligible SQLite backups with `method = "raw-copy"`, per-engine warning/checkbox, confirm field) and the restore-history table.

## Verification plan

1. Restore a Postgres target from a real backup into the same live container, confirm the database's contents match what was backed up (e.g. a row added after the backup is gone, a row present at backup time is back).
2. Restore a MySQL and a MariaDB target the same way, specifically after adding extra rows/tables since the backup, confirm the drop-and-recreate-tables step (not drop-and-recreate-database) actually replaces rather than merges (no leftover post-backup tables survive), and confirm it succeeds using the same scoped `db_user` credentials backups already use, no elevated/root credentials required. Confirm `FOREIGN_KEY_CHECKS` is back to `1` after the restore completes, a table drop that leaves it disabled would be a silent hazard for anything using that database afterward.
3. Confirm a MariaDB restore uses the `mariadb` client binary, not `mysql`, against an image where only the former exists (mirroring Phase 2's `mariadb-dump` verification).
4. Restore a SQLite target with "stop container" checked, confirm the container is stopped before the file is overwritten and started again after, and the app comes back up reading the restored data.
5. Restore the same SQLite target with "stop container" unchecked, confirm it still completes (Docker's archive API doesn't require a stopped container), documented as the operator's explicit choice to skip the extra safety step.
6. Attempt to restore a target while a backup for it is in progress (and vice versa: attempt a manual backup while a restore is in progress), confirm both directions are rejected via the shared lock, no double-execution.
7. Attempt to restore typing the wrong confirmation name, confirm rejection with no state change.
8. Confirm the restore dropdown only ever offers `success` runs whose file is verified present on disk, a target with some already-pruned history doesn't offer those as restore candidates.
9. Force a restore failure (e.g. corrupt the pushed file, or misconfigure credentials), confirm an ntfy notification fires; force a success, confirm ntfy also fires (unlike backup's failure-only behavior), confirm the reasoning is visible in the message (which target, which backup, success or failure).
10. Delete a target that has restore history, confirm `restore_runs` rows are gone along with the rest of the cascade.
11. Confirm the history table and index page now show local (Europe/Prague) wall-clock time with a zone abbreviation, not raw UTC, matching the container's actual clock; confirm the underlying stored value in the state db is still UTC (unchanged).
12. Confirm no leftover temp file remains inside the target container after a restore, for both a successful restore and a forced-failure restore (Postgres's `.dump` path and MySQL/MariaDB's `.sql` path), cleanup happens either way.
13. Confirm the "raw copy, not live-consistent" tag renders correctly in the restore dropdown next to an eligible SQLite backup whose `method` is `"raw-copy"` (the fallback technique from Phase 2), and is absent for eligible backups that used the primary technique.
14. Unit tests (mocked, no live Docker): each adapter's `restore()` command construction (including the MySQL/MariaDB drop-and-recreate-*tables* step, the `FOREIGN_KEY_CHECKS` toggle, temp-file cleanup on both success and failure paths, and the `restore_client_binary` distinction); `docker_client.put_archive_file()`; the shared-lock collision in both directions (restore-blocks-backup, backup-blocks-restore); `list_eligible_backups_for_restore()`'s existence filtering and its raw-copy tag data; `delete_target()`'s restore_runs cascade; the local-time Jinja filter's correctness against a known UTC input and a known `TZ` value, including its `UTC` fallback when `TZ` is unset.

## Real-world verification results

The verification plan above was run on the throwaway test LXC, not just the mocked route-level tests recorded in `phase-5-build-summary.md`, across all four engines:

- SQLite restore correctly replaces file contents in both stop-container modes (checked and unchecked), container returns to a healthy running state after a stop/restart cycle: confirmed. One issue found and resolved during testing, not a Savepoint bug: the original throwaway test container's startup command was a one-shot setup script (`CREATE TABLE ... && sleep infinity`) rather than idempotent, so a Docker `start()` re-ran table creation against already-existing data and the container exited immediately. Recreating the test container with `sleep infinity` as its actual long-running command (data setup done as a separate one-time exec afterward) resolved it; real database images (Postgres/MySQL/MariaDB official entrypoints) don't have this problem, they're specifically designed to be idempotent across restarts.
- Postgres restore genuinely replaces rather than merges (a post-backup row is gone after restore, the pre-backup row is back): confirmed.
- MySQL restore's drop-and-recreate-tables approach (the amended fix) correctly removes post-backup tables and rows using the same scoped, non-root `db_user` credentials backups already use, no elevated privileges required: confirmed.
- MariaDB restore confirmed the same, plus correct use of the `mariadb` client binary (not `mysql`) and `FOREIGN_KEY_CHECKS` correctly back to `1` after the restore: confirmed.
- Temp files pushed into the container for Postgres/MySQL/MariaDB restores are cleaned up afterward, verified on both a successful restore and a forced failure (corrupted backup file): confirmed.
- ntfy fires on both restore outcomes (success and failure), unlike backup's failure-only behavior: confirmed.
- The restore dropdown correctly excludes a backup whose file no longer exists on disk (manually removed after use), while its `backup_runs` history row remains visible and unaffected, correctly preserving the record/reality distinction Phase 4.5 established for delete's file count: confirmed.
- Local-time display fix: timestamps across history, index, and the new restore dropdown all render correct Europe/Prague wall-clock time with a CEST zone abbreviation, confirmed against the real host clock throughout this entire testing session, not just a synthetic check: confirmed.
- The "raw copy, not live-consistent" tag renders correctly in the restore dropdown next to an eligible SQLite backup using the fallback method: confirmed.

## Status

Plan drafted 2026-07-28, amended 2026-07-28 (MySQL/MariaDB restore privilege fix: drop-and-recreate-tables instead of drop-and-recreate-database; explicit in-container temp-file cleanup; raw-copy tag shown in the restore dropdown), approved and built 2026-07-28. Fully verified against real Docker containers on the test LXC and closed 2026-07-28.
