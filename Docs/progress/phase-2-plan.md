# Savepoint - Phase 2 Plan: Discovery and Remaining SQL Engines

## Context

Phase 1 proved the core loop end to end for Postgres only, with manual add and no discovery. Phase 2, as scoped in `Docs/04-Initial-Build-Plan.md`, adds:

- Docker socket discovery for Postgres/MySQL/MariaDB (image keyword match, env var pre-fill), surfaced as one-click-confirm suggestions, never auto-added.
- MySQL and MariaDB adapters, following the `Adapter` interface pattern from `app/adapters/base.py` and `app/adapters/postgres.py` (password read live from the container's own env at backup time, never stored).
- SQLite adapter, manual add only this phase (container + file path); no dedicated container to discover, so container-image-based discovery does not apply to it.
- Manual add stays available in parallel for every engine, as the fallback/override for anything discovery misses or gets wrong (already true for Postgres since Phase 1; this phase extends it to all four engines through the same routes).

Goal: any of the four engines can be backed up, either via a one-click discovery confirm or manual add, using one shared set of routes rather than one path per engine.

## Design decisions

- **Adapter registry**: `app/adapters/__init__.py` exposes `ADAPTERS = {"postgres": PostgresAdapter(), "mysql": MySQLAdapter(), "mariadb": MariaDBAdapter(), "sqlite": SQLiteAdapter()}`, keyed by the `targets.engine` column. `app/routes/targets.py`'s `run_backup` currently hardcodes a module-level `PostgresAdapter()` instance; this becomes `ADAPTERS[target["engine"]].backup(...)`. This is the one change to Phase 1 code this phase requires.
- **One create-target path for both flows**: discovery does not get its own "create target" endpoint. `GET /targets/add` (existing) accepts optional query-string pre-fill (`?engine=&container_name=&db_user=&db_name=`) and `POST /targets` (existing) gains an `engine` field. A discovery candidate's one-click "Add" is a plain `<form method=post action=/targets>` with hidden inputs pre-filled from the scan, submitting to the exact same handler manual add already uses (this doubles as a live re-check that the container still exists, container existence is already validated in `POST /targets` from Phase 1's review decision 3). A "review before adding" link next to each candidate goes to `GET /targets/add` with the same values as query params, for the case where the auto-fill guessed wrong. Net result: one route creates targets, one route renders the add form, discovery only adds a read-only listing route.
- **MySQL and MariaDB share one implementation**: per `03-Proposed-Architecture.md` ("both engines share the same adapter given dump-format compatibility"), `app/adapters/mysql.py` holds a private `_MySQLFamilyAdapter` base with the shared `mysqldump`/exec logic, and two thin public classes `MySQLAdapter`/`MariaDBAdapter` that only differ in image keywords and candidate env var names. Dump format is plain SQL (`mysqldump`, no `-F`-style custom format equivalent exists), extension `.sql`, password passed via the `MYSQL_PWD` env var to the exec call (keeps it out of argv, mirrors the `PGPASSWORD` pattern from Phase 1).
- **Env var names are not assumed correct, they degrade to a clear failure**: rather than hardcoding one password/user/db env var name per engine, each adapter tries an ordered list of candidates and fails with a message naming every candidate it checked if none are set. Concretely:
  - Postgres (already exact from Phase 1): `POSTGRES_USER`, `POSTGRES_DB`, `POSTGRES_PASSWORD`.
  - MySQL/MariaDB user+db (pre-fill only, not validated at creation): `MARIADB_USER`/`MYSQL_USER`, `MARIADB_DATABASE`/`MYSQL_DATABASE`.
  - MySQL/MariaDB password at backup time: if `db_user == "root"`, try `MARIADB_ROOT_PASSWORD` then `MYSQL_ROOT_PASSWORD`; otherwise try `MARIADB_PASSWORD` then `MYSQL_PASSWORD`. This list is a reasonable default covering the official images, not a guarantee, it should be verified against whatever image is actually deployed (e.g. `linuxserver/mariadb` vs official `mariadb`) before being trusted in production, exactly the open question flagged in `03-Proposed-Architecture.md`.
- **SQLite backup technique, and which one is recorded**: per `03-Proposed-Architecture.md` ("file-level copy, or `.backup` command via `sqlite3` if available"), the adapter first tries `sqlite3 <path> ".backup '/tmp/savepoint-sqlite-backup'"` inside the target container (a live-safe copy using SQLite's own backup API), pulls the resulting file out via the Docker SDK's `get_archive()` (a tar-based file transfer, works regardless of what's installed in the container), and cleans up the temp file. If `sqlite3` isn't present in that container (nonzero/not-found exit), it falls back to pulling the original file path directly via `get_archive()`, a plain copy with no live-consistency guarantee. These two techniques are not equivalent successes: a raw copy taken while the file is actively being written can produce a silently corrupted backup that only reveals the problem at restore time. So `BackupResult` (and, from it, `backup_runs`) records which technique actually ran, `"live"` for the `.backup`-command path or `"raw-copy"` for the fallback, not just a bare `success`. This is a small, mandatory addition to `BackupResult` (a new `method: str | None` field, populated by `SQLiteAdapter` only, `None` for the other three engines) rather than a UI-only afterthought, so the distinction survives all the way from the adapter to the history view.
- **SQLite target creation validates the file exists**: extending Phase 1's "verify the container exists before saving" pattern, adding a SQLite target also calls `get_archive()` on the given path (without consuming the stream) and rejects the form if it 404s. Typo'd file paths are far more likely than typo'd container names, so this closes an obvious gap the same way review decision 3 did for containers.
- **Discovery scope**: running containers only. A stopped container's image name and env can technically be inspected, but nothing can be backed up from it until it's running anyway, and manual add remains available for anyone who wants to pre-register a target ahead of time.
- **SQLite volume-mount discovery heuristic (scanning non-DB-image containers for `.db`/`.sqlite`/`.sqlite3` files) is deferred, not part of this phase.** Reason: image-keyword discovery for Postgres/MySQL/MariaDB is a mechanical extension of the `Adapter.discover(container)` interface already being built for those three engines. Volume-mount scanning is a structurally different mechanism, it has to enumerate mounted volumes on containers that do *not* match a known DB image, then walk file trees inside them looking for extensions, and it doesn't fit the same "does this adapter claim this container" pattern at all. Bundling it into this phase risks scope creep on top of three new adapters plus discovery plus a schema change; `04-Initial-Build-Plan.md` itself already treats this as optional/slippable for Phase 2 ("discovery heuristic comes with it or can slip to this phase depending on complexity"). It should be scheduled explicitly whenever it's next prioritized, manual add already covers SQLite target creation in the meantime.
- **Schema change stays additive, no migration framework introduced**: `targets` gains a nullable `file_path` column (used by SQLite targets only) and `db_user`/`db_name` become `NOT NULL DEFAULT ''` so SQL-engine-only fields can be blank for SQLite rows without relaxing a constraint that would need a table rebuild in SQLite. `app/db.py`'s `init_db()` gains a small idempotent step: check `PRAGMA table_info(targets)` for `file_path`, and `ALTER TABLE targets ADD COLUMN file_path TEXT` if it's missing, so the test LXC's existing Phase 1 state db (a handful of rows) picks up the new column on next start without a manual drop/recreate. This is a minimal, deliberately unambitious migration mechanism (a few guarded `ALTER TABLE ADD COLUMN` calls), not a framework, future phases needing new columns (schedules in Phase 3, retention tags in Phase 4) can reuse the same pattern.
- **Add-target form shows all fields, no show/hide JS**: rather than adding a toggle script to hide irrelevant fields per engine, the form always shows DB user / DB name / file path together with inline hints on which apply to which engine. Requiredness is enforced server-side in `POST /targets` (based on the selected `engine`), not via HTML `required` attributes, since which fields are required varies by engine. Keeps the frontend at zero added JS, consistent with the project's HTMX-first, Python-only preference.

## Project layout changes

```
app/
  adapters/
    __init__.py        # now exports ADAPTERS registry (was empty)
    base.py             # unchanged interface, now actually implemented by 4 adapters
    postgres.py          # discover()/default_connection_info() implemented (were NotImplementedError stubs)
    mysql.py              # NEW: _MySQLFamilyAdapter, MySQLAdapter, MariaDBAdapter
    sqlite.py             # NEW: SQLiteAdapter
  routes/
    targets.py            # POST /targets gains `engine` field + per-engine validation; run_backup dispatches via ADAPTERS[engine]
    discover.py           # NEW: GET /discover, read-only candidate listing
    history.py            # unchanged
  templates/
    targets/add.html       # engine <select>, file_path field, query-string pre-fill support, per-engine hint text
    targets/detail.html      # shows file_path instead of db_user/db_name when engine == sqlite
    partials/history_row.html # shows a "raw copy, not live-consistent" tag next to any run where method == "raw-copy"
    discover.html            # NEW: candidate list, one-click "Add" form + "review first" link per row
    index.html                 # add an Engine column
  docker_client.py             # add exec_and_capture() (generalized from exec_pg_dump), get_archive_file(), exec_simple()
schema.sql                       # targets: file_path column added, db_user/db_name become NOT NULL DEFAULT ''
```

## Data model changes (`schema.sql`)

```sql
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    engine TEXT NOT NULL DEFAULT 'postgres',
    container_name TEXT NOT NULL,
    db_user TEXT NOT NULL DEFAULT '',
    db_name TEXT NOT NULL DEFAULT '',
    file_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_runs (
    id INTEGER PRIMARY KEY,
    target_id INTEGER NOT NULL REFERENCES targets(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    file_path TEXT,
    file_size_bytes INTEGER,
    error_message TEXT,
    method TEXT
);
```

`backup_runs` gains a nullable `method` column alongside `targets.file_path`. Only `SQLiteAdapter` ever writes to it (`"live"` or `"raw-copy"`), the other three engines leave it `NULL`; `db_module.finish_backup_run()` gains an optional `method` keyword argument that the SQLite run passes through, everyone else omits.

`app/db.py::init_db()` runs the existing `schema.sql` script (handles fresh installs) and then two guarded, additive migration steps for anyone upgrading from a Phase 1 state db: `ALTER TABLE targets ADD COLUMN file_path TEXT` and `ALTER TABLE backup_runs ADD COLUMN method TEXT`, each gated on the column being absent from `PRAGMA table_info(...)`. Same pattern, same reasoning, just one more column added this phase than originally planned.

## Adapter interface usage

`Adapter.discover(container)` and `Adapter.default_connection_info(container)` (declared in `base.py` since Phase 1, unimplemented until now) get real implementations:

- `PostgresAdapter.discover`: `"postgres" in container.attrs["Config"]["Image"].lower()`.
- `PostgresAdapter.default_connection_info`: `{"db_user": env.get("POSTGRES_USER", "postgres"), "db_name": env.get("POSTGRES_DB") or env.get("POSTGRES_USER", "postgres")}`.
- `MySQLAdapter.discover` / `MariaDBAdapter.discover`: `"mysql"` / `"mariadb"` substring match against the same image string.
- `MySQLAdapter.default_connection_info` / `MariaDBAdapter.default_connection_info`: read the env-var candidate lists above, first match wins, blank if none found (still surfaced as a suggestion, just with an empty field the operator fills in before confirming).
- `SQLiteAdapter.discover` / `default_connection_info`: not applicable (`discover` returns `False` always, this engine is never image-matched), consistent with it being manual-add only this phase.

`backup()` for each engine follows the same shape established by `PostgresAdapter.backup()` in Phase 1: check preconditions (container exists, credentials resolvable, or file exists for SQLite), run the engine-specific dump/copy, clean up on failure, return a `BackupResult`. `restore()` stays `NotImplementedError` for all three new adapters (Phase 5 scope), matching `PostgresAdapter`.

## Routes

- `GET /discover` - lists running containers matched against Postgres/MySQL/MariaDB `discover()`, excluding any container name already present in `targets`. Each row shows container name, image, guessed engine, and pre-filled connection info.
- `GET /targets/add` - unchanged shape, now also accepts optional `engine`, `container_name`, `db_user`, `db_name` query params to pre-fill the form (used by discovery's "review first" link).
- `POST /targets` - gains an `engine` form field (`postgres` default, for backward compatibility with the Phase 1 form shape); validates required fields per engine (`db_user`+`db_name` for the three SQL engines, `file_path` for sqlite); for sqlite, additionally checks the file exists in the container via `get_archive()` before saving.
- `POST /targets/{id}/run` - unchanged route, now dispatches to `ADAPTERS[target["engine"]]` instead of a hardcoded `PostgresAdapter()`; passes the adapter's `BackupResult.method` through to `finish_backup_run()`.
- `GET /targets/{id}` - unchanged route, template conditionally shows file_path vs db_user/db_name based on engine. The included history table (`partials/history_row.html`, shared with the HTMX partial response from `POST /targets/{id}/run`) renders a small inline tag, "raw copy, not live-consistent", next to any row where `method == "raw-copy"`; rows with `method == "live"` or `NULL` (every non-SQLite run) show nothing extra. `index.html`'s per-target summary row is unaffected, it only shows the latest run's status, not per-run method detail, so the tag lives at the history-row granularity where the rest of the run's detail (file path, size, error) already is.

## Verification plan

1. Run discovery against a Docker host with a real Postgres container (from Phase 1's testing), a MySQL container, and a MariaDB container running. Confirm all three show up as candidates with plausible pre-filled user/db values, and confirm a container already added as a target in Phase 1 does **not** reappear as a candidate.
2. One-click "Add" a discovered MySQL candidate, confirm it's created with the pre-filled values and appears on the index page with engine `mysql`.
3. Use "review first" on a MariaDB candidate, change the pre-filled db name, submit, confirm the target is created with the edited value (not the discovery guess).
4. Run a backup against the MySQL and MariaDB targets, confirm a `.sql` file lands under the bind-mounted target directory and history shows `success`.
5. Recreate one of the MySQL/MariaDB containers with a renamed password env var, run a backup, confirm a clean `failure` naming every candidate env var name that was checked.
6. Manually add a SQLite target (container + file path) pointing at a real file inside a running app container; confirm a nonexistent path is rejected at creation time with a clear error.
7. Run a SQLite backup where the target container has `sqlite3` installed, confirm the resulting file opens cleanly (e.g. `sqlite3 <file> .tables` works against the copy), the run is recorded as `success`, and its history row shows `method = "live"` with **no** "raw copy" tag. Then repeat against a container without `sqlite3` installed, confirm the fallback path still produces a usable file, the run is still recorded as `success`, but its history row shows `method = "raw-copy"` and the "raw copy, not live-consistent" tag. Put both runs side by side on the same target's detail page and confirm they are visibly distinguishable at a glance, not just both marked `success`.
8. Restart the Savepoint container against the existing (Phase 1-era) state db, confirm it starts cleanly and the `targets` table now has a `file_path` column and `backup_runs` now has a `method` column, both without needing a manual reset, and that pre-existing Postgres targets/history are untouched (their `method` reads `NULL`, no tag shown).
9. Re-run `tests/test_postgres_adapter.py` (should be unaffected) plus new adapter tests for MySQL/MariaDB (env var fallback logic, command construction) and SQLite (asserting `method == "live"` on the `.backup`-command path and `method == "raw-copy"` on the fallback path, in addition to the existing success/failure cases), all mocked, no live Docker required, following the same pattern as Phase 1's tests.

## Real-world verification results

The verification plan above was run against real Postgres, MySQL, and MariaDB containers plus two SQLite-holding containers (one with `sqlite3` installed, one without) on a throwaway test LXC, not just the mocked route-level dry run recorded in `phase-2-build-summary.md`:

- Discovery correctly found the Postgres, MySQL, and MariaDB containers and excluded a non-DB container: confirmed.
- Postgres and MySQL backups succeeded on the first try: confirmed.
- MariaDB backup initially failed with exit code 127 (`mysqldump` not found, recent official MariaDB images only ship `mariadb-dump`); fixed via the `dump_binary` class attribute (`MySQLAdapter` uses `mysqldump`, `MariaDBAdapter` uses `mariadb-dump`), re-tested and confirmed succeeding.
- SQLite live-backup (`.backup` command) and raw-copy fallback both succeeded and are visibly distinguishable in the history UI via the `method` tag, exactly as the amendment intended: confirmed.
- The "review first" edit-then-submit flow correctly uses the edited value, not the original discovery guess: confirmed.
- Schema migration (`file_path`, `method` columns) persists correctly across a container restart, pre-existing Phase 1 target/history rows untouched: confirmed.

## Status

Plan approved 2026-07-27, revised to add `backup_runs.method`, cleared to build. Fully verified against real Docker containers and closed 2026-07-27.
