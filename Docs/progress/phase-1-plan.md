# Savepoint - Phase 1 Plan: Core Skeleton, Postgres Only, Manual Add

## Context

Savepoint replaces the current `db-backup-scripts` bash/cron setup with a proper app. The repo currently contains only planning docs (`Docs/01` through `Docs/04`) and no code. Per `CLAUDE.md`'s phased build process, this plan covers Phase 1 only, as scoped in `Docs/04-Initial-Build-Plan.md`:

- FastAPI + Jinja2/HTMX skeleton.
- SQLite app-state db (targets, backup history).
- Manual "add database" flow, Postgres only.
- Postgres adapter: `backup()` via `docker exec` + `pg_dump`.
- One-off "run backup now" button, no scheduling.
- Basic backup history list.
- Explicitly out of scope for this phase: discovery, retention/pruning, restore, other engines, remote agent.

Goal: prove the full loop (add a real Postgres container as a target, trigger a backup, see a dump file land on the bind-mounted target, see it recorded in history) end to end against a real container on the homelab Docker host.

## Design decisions

- **State storage**: plain `sqlite3` + a hand-written `schema.sql` applied at startup (`CREATE TABLE IF NOT EXISTS`), no ORM. Matches the "keep this simple, Python-only" instruction in `CLAUDE.md`; schema is small enough (2 tables this phase) that SQLAlchemy would be premature.
- **Dump format**: Postgres custom format (`pg_dump -Fc`), file extension `.dump`. Chosen because `03-Proposed-Architecture.md` names `pg_restore` for the restore phase, which expects custom-format dumps, not plain SQL.
- **Container reference**: targets store the Docker container name (not ID), since names survive container recreation and `docker exec` accepts either.
- **Credential storage**: the password is never stored in Savepoint's state db. Only `db_user` and `db_name` are stored; at backup time the password is read live from the target container's own environment (e.g. `POSTGRES_PASSWORD`) via `docker inspect`, the same source Phase 2 discovery will read from. This avoids Savepoint becoming a second place secrets can leak from, and keeps the container's own env as the single source of truth for its own credential. If no matching password env var exists on the container at backup time, the run fails immediately with a clear error rather than prompting for one.
- **Container existence check**: creating a target validates the container name via `docker inspect` before the row is saved. An unknown container name is rejected at form-submission time with a clear error, not left to fail later on first "run backup now".
- **Execution model**: "run backup now" runs synchronously in the request handler (blocking until `pg_dump` finishes), no background task queue yet. Scheduling/concurrency arrives in Phase 3; adding async job handling now would be built twice. The UI shows a simple in-flight spinner/"running" state while the request is outstanding, then swaps to the success/failure result. No percentage progress, no polling, no SSE, that's out of scope until Phase 3's real job orchestration exists.
- **MODE env var**: scaffolded from day one per `CLAUDE.md` ("single image, do not build two"), but only `MODE=master` behaves in Phase 1. `MODE=agent` on this image logs a clear "not implemented until Phase 6" message and exits, rather than silently behaving like master.

## Project layout

```
savepoint/
  app/
    __init__.py
    main.py                 # FastAPI app factory, MODE dispatch, router mounting
    config.py                # env var loading (dataclass/Settings, no pydantic dependency needed)
    db.py                    # sqlite3 connection helper, schema init on startup
    docker_client.py         # thin wrapper: list/inspect containers, read env vars, exec_run helper (stdout capture to file, demux stdout/stderr)
    adapters/
      __init__.py
      base.py                # Adapter protocol: discover(), default_connection_info(), backup(), restore()
      postgres.py            # PostgresAdapter.backup(); discover()/restore() raise NotImplementedError with a clear message (filled in Phase 2/5)
    routes/
      __init__.py
      targets.py             # GET add form, POST create target, POST run-backup-now
      history.py             # GET history list/detail (full page + HTMX partial)
    templates/
      base.html
      index.html             # target list + latest status
      targets/add.html
      targets/detail.html      # target detail + its history
      partials/history_row.html  # HTMX swap target after a run
  schema.sql                 # targets, backup_runs tables
  Dockerfile
  docker-compose.yml          # example deployment, bind-mounts /backup-target and docker.sock
  requirements.txt
  tests/
    test_postgres_adapter.py  # command construction only, no real docker
```

## Data model (`schema.sql`)

```sql
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    engine TEXT NOT NULL DEFAULT 'postgres',
    container_name TEXT NOT NULL,
    db_user TEXT NOT NULL,
    db_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_runs (
    id INTEGER PRIMARY KEY,
    target_id INTEGER NOT NULL REFERENCES targets(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',  -- running | success | failure
    file_path TEXT,
    file_size_bytes INTEGER,
    error_message TEXT
);
```

## Adapter interface (`app/adapters/base.py`)

Defined now so later engines slot in without touching routes/scheduling:

```python
class Adapter(Protocol):
    def discover(self, container) -> bool: ...
    def default_connection_info(self, container) -> dict: ...
    def backup(self, target_row, destination_dir: str) -> BackupResult: ...
    def restore(self, target_row, source_path: str) -> None: ...
```

`PostgresAdapter.backup()`:
1. Insert a `backup_runs` row, `status='running'`.
2. `docker_client.get_container_env(target.container_name)`, read the container's live env vars via `docker inspect` and look for `POSTGRES_PASSWORD` (Postgres image convention). If missing, immediately mark the run `failure` with an error message naming the expected env var and container, and stop, no dump attempted.
3. Build destination path: `{BACKUP_TARGET_DIR}/{target.name}/{target.name}_{timestamp}.dump`, `os.makedirs(..., exist_ok=True)`.
4. `docker_client.get_container(target.container_name).exec_run(["pg_dump", "-U", user, "-d", dbname, "-Fc"], environment={"PGPASSWORD": password}, stream=True, demux=True)`. The password is used only for this one call and never persisted.
5. Stream stdout chunks to the destination file; collect stderr for the error message if the exec exits non-zero.
6. Inspect exit code, update the `backup_runs` row: `finished_at`, `status`, `file_size_bytes` (via `os.path.getsize`), `error_message` on failure.

## Routes

- `GET /` - list targets with latest run status (index.html).
- `GET /targets/add` - manual add form (container name, db user, db name; engine fixed to postgres this phase; no password field).
- `POST /targets` - validate the container name exists via `docker inspect` (`docker_client.get_container()` raising `NotFound` maps to a form error); on success create the target row and redirect to `/`, on failure re-render the form with a clear "container 'x' not found" error.
- `GET /targets/{id}` - target detail + history table.
- `POST /targets/{id}/run` - execute `PostgresAdapter.backup()` synchronously, return the updated history partial (HTMX swap) or redirect for a full-page fallback. The triggering button shows an HTMX indicator (spinner/"running...") for the duration of the request, then the partial swap replaces it with the success/failure result, no polling involved.

## Config (env vars, all with sane local defaults for dev)

- `MODE` (default `master`)
- `BACKUP_TARGET_DIR` (default `/backup-target`)
- `STATE_DB_PATH` (default `/data/savepoint.db`)
- `HOST` / `PORT` for uvicorn (default `0.0.0.0:8000`)

## Docker

- `Dockerfile`: single image. `pg_dump` client tools are NOT needed in the Savepoint image itself (the dump runs inside the target Postgres container via `docker exec`); Savepoint's own image only needs Python deps + Docker SDK.
- `docker-compose.yml` (example, homelab-style): mounts `/var/run/docker.sock`, a bind-mounted backup target (e.g. `/mnt/core/backups/db:/backup-target`), a state-db volume, `deploy.resources.limits` + top-level `memswap_limit` per convention.

## Verification plan

1. `docker compose up` against a real (or throwaway test) Postgres container on the Docker host.
2. Attempt to add a target with a nonexistent container name, confirm the form is rejected with a clear error and no row is created.
3. Add the real container as a target via the UI form (container name, db user, db name, no password field).
4. Click "run backup now", confirm the button shows a running/spinner state while the request is in flight, then confirm a `.dump` file appears under the bind-mounted target directory, correctly named and non-empty.
5. Confirm the history table shows the run as `success` with file path and size.
6. Temporarily rename or unset the container's `POSTGRES_PASSWORD` env var (recreate the container without it), run a backup again, confirm the run is recorded as `failure` with an error message naming the missing env var (and no partial/corrupt file left as `success`).
7. Restart the Savepoint container, confirm existing targets and history persist (SQLite state db survives via its volume), and confirm no password was ever written to the state db (inspect the `targets` table directly).
8. Set `MODE=agent` once and confirm the container logs the "not implemented" message and exits cleanly rather than mimicking master.

No unit test suite beyond `tests/test_postgres_adapter.py` (command/args construction, no live Docker) is planned for this phase; correctness here is proven by the manual end-to-end pass above, per the project's phase build process.

## Real-world verification results

The verification plan above was run against real Docker containers on a throwaway test LXC (not just the mocked `TestClient` pass recorded in `phase-1-build-summary.md`):

- Container-not-found rejected at target creation: confirmed.
- Successful backup, correct `.dump` file landed under `BACKUP_TARGET_DIR`: confirmed.
- Wrong `db_name` produces a clean `pg_dump` error with no partial file left behind: confirmed. This case wasn't in the original verification plan, it surfaced during testing and is worth keeping as extra coverage alongside the missing-password case.
- Missing `POSTGRES_PASSWORD` env var fails cleanly with the expected error message: confirmed.
- Restarting the Savepoint container preserves targets and history via the state db volume: confirmed.

## Status

Plan approved 2026-07-27, revised after review, cleared to build. Fully verified against real Docker containers and closed 2026-07-27.
