# Savepoint - Phase 1 Build Summary

Built against [phase-1-plan.md](phase-1-plan.md) (revised after review, cleared to build).

## What was built

- FastAPI app skeleton (`app/main.py`) with Jinja2/HTMX templates, single image, `MODE` env var dispatch. `MODE=agent` logs "not implemented until Phase 6" and exits 1, `MODE=master` runs the app.
- SQLite app-state db (`schema.sql`, `app/db.py`): `targets` and `backup_runs` tables. No password column, per review decision 1.
- Manual add-target flow (`GET/POST /targets`, `app/templates/targets/add.html`): name, container name, db user, db name. Creating a target checks the container exists via `docker inspect` first (review decision 3) and rejects the form with a clear error if not.
- Postgres adapter (`app/adapters/postgres.py`): `backup()` reads the password live from the target container's `POSTGRES_PASSWORD` env var via `docker inspect` at backup time (review decision 1), runs `pg_dump -Fc` via `docker exec`, streams stdout straight to the bind-mounted target. Fails clearly (and cleans up any partial file) if the container is gone, the password env var is missing, or `pg_dump` exits non-zero. `discover()`/`default_connection_info()`/`restore()` are stubbed with `NotImplementedError` for Phase 2/5.
- "Run backup now" (`POST /targets/{id}/run`): synchronous, HTMX-driven. The button shows a running/spinner state (`hx-indicator`) and disables itself for the duration of the request (review decision 2), then swaps in the updated history table with success/failure.
- Backup history list on the target detail page (`GET /targets/{id}`), with status, file path, size, and error message per run.
- `Dockerfile` + example `docker-compose.yml` (docker.sock mount, bind-mounted backup target, `deploy.resources.limits` + top-level `memswap_limit` per homelab convention).
- `tests/test_postgres_adapter.py`: 4 tests covering missing container, missing password env var, successful backup, and failed backup with partial-file cleanup.

## Deviations from the plan doc

- Added `app/deps.py` (not in the original file tree) to hold the shared `get_db_conn` FastAPI dependency and the single `Jinja2Templates` instance, so `targets.py` and `history.py` don't duplicate or import from each other.
- Added `pyproject.toml` with `[tool.pytest.ini_options] pythonpath = ["."]` so `pytest` can import the `app` package without an install step.
- Added `from __future__ import annotations` to `app/db.py` and `app/adapters/base.py` so the `str | None` style hints don't break on Python 3.9 (this dev machine's only local interpreter; the Docker image itself targets 3.12, where this is moot either way).
- Added `.gitignore` (`.venv/`, `__pycache__/`, `*.db`, `*.dump`, etc.), none existed before.

## Testing performed

- `pytest tests/` - 4/4 adapter tests pass (real Docker not involved, `docker_client` mocked).
- Booted the app against a temp SQLite db and temp backup-target dir via FastAPI's `TestClient`:
  - `GET /`, `GET /targets/add`, `GET /targets/{missing}` return expected 200/200/404.
  - Confirmed the `targets` table has no `db_password` column.
  - `MODE=agent` exits with code 1 and logs the expected message.
  - With `docker_client` mocked (no real Docker daemon available in this dev environment): rejected a target pointing at a nonexistent container (400, clear error), created a target against a "found" container (303 redirect), ran a mocked backup end to end (success recorded, file written, shown on both the index and detail pages).

## Post-build fix

`GET /` was 500ing: `app/routes/targets.py` and `app/routes/history.py` were built against the old Starlette `TemplateResponse(name, {"request": request, ...})` signature, but the installed FastAPI/Starlette (0.128.8 / 0.49.3) require the newer `TemplateResponse(request, name, context)` form. All call sites were updated to the new signature (`request` dropped from the context dict), and `fastapi`/`starlette` were pinned to `0.128.8`/`0.49.3` in `requirements.txt` (previously `>=0.115` with no ceiling) so a future rebuild can't silently pull another breaking API change. Verified by rebuilding the Docker image and hitting the running container directly: `GET /` and `GET /targets/add` both return 200.

## Not tested here (needs the real homelab Docker host)

This dev machine has no Docker daemon running, so the actual `docker exec` + `pg_dump` path against a real Postgres container hasn't been exercised yet. Before considering Phase 1 done, run the verification steps in `phase-1-plan.md` against a real container:
1. Add a real Postgres container as a target, confirm container-not-found is rejected at creation time.
2. Run a backup, confirm the button shows "running..." and disables, then a `.dump` file lands under the bind-mounted target and history shows `success`.
3. Recreate the container without `POSTGRES_PASSWORD` (or rename it), run again, confirm a clear `failure` with the missing-env-var message and no dangling partial file.
4. Restart the Savepoint container, confirm targets/history persist and no password was ever written to `targets`.
