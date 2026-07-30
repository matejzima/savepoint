# Savepoint - Phase 6 Build Summary

Built against [phase-6-plan.md](phase-6-plan.md) (approved as amended: the offsite manual-transfer limitation documented explicitly; broad exception handling in `RemoteAdapter` matching Phase 5's `start_container()` lesson; explicit per-call timeouts in `agent_client.py`; agent editing via `POST /agents/{id}/edit`; `RemoteAdapter.backup()` writes to a temp path and renames on success).

## What was built

### The core reuse: RemoteAdapter

- **`app/adapters/remote.py::RemoteAdapter`**: implements `backup(target_row, backup_target_dir) -> BackupResult`, the exact same signature every local adapter already has. Streams the agent's `/api/backup` response to a `.<name>.part` temp file inside the target's folder, and only `os.replace()`s it into the real destination (named from the `X-Savepoint-Filename` response header, so master reuses whatever naming convention the agent's own local adapter already produced) once the stream has fully and successfully completed. Any failure, connection error, timeout, non-2xx response, a missing filename header, or an unexpected exception during the write itself (e.g. disk full), is caught by one broad `except Exception` and converted into a normal `BackupResult(success=False, ...)`, with the partial temp file removed. `restore_with_lifecycle(target_row, source_path, stop_container) -> RestoreResult` is the equivalent for restore, called only from `restore.py`'s new agent branch (see below), also broadly caught.
- **`jobs.py::_execute()`** changed one line: `adapter = remote.remote_adapter_for(target, _settings) or ADAPTERS[target["engine"]]`. Everything after that line, `finish_backup_run()`, `retention.tag_and_prune()`, failure notification, is completely unchanged for an agent-owned target.
- **`restore.py::perform_restore()`** gained one branch at the top: if `target["agent_id"]` is set, it calls `RemoteAdapter.restore_with_lifecycle()` (which does stop-restore-start as one atomic remote call) instead of the existing local stop/`adapter.restore()`/start orchestration, which is otherwise byte-for-byte untouched. `restore.py` gained its own `init(settings)`/`_settings` module global, mirroring `jobs.py`/`notifications.py`, called from `scheduler.start()`.
- **`RestoreResult`** (`adapters/base.py`) gained one new field, `stopped_container: bool = False`, default preserves every existing local-adapter call site.

### Agent mode (headless)

- **`app/routes/agent_api.py`** (mounted only when `MODE=agent`): `GET /api/health`, `GET /api/discover`, `POST /api/validate`, `POST /api/backup` (streams the resulting file back with `X-Savepoint-Filename`/`X-Savepoint-Method` headers, or a JSON error on failure, staging directory always cleaned up via `FileResponse`'s `background` task or immediately on failure), `POST /api/restore` (multipart upload, stages locally, runs the matching adapter's `restore()`, optionally stops/starts the container around it atomically, always returns the `{success, stopped_container, error}` shape regardless of HTTP status, always cleans up its staging directory in a `finally`). All routes sit behind `require_agent_token()`, a small dependency comparing the `Authorization: Bearer` header against `AGENT_TOKEN`.
- **`app/validation.py`** / **`app/discovery.py`**: pure extractions of `routes/targets.py`'s connection validation and `routes/discover.py`'s candidate-scanning loop, reused as-is by both the local routes and the new agent-mode API handlers.
- **`config.py`** gained `agent_token: str | None` (`AGENT_TOKEN`). **`main.py`**: agent mode refuses to start if `AGENT_TOKEN` is unset; agent mode skips `db.init_db()`, `retention.reconcile_all()`, and `scheduler.start()` entirely (no state db, no schedule, no history of its own); agent mode mounts only `agent_api.router`, master mode mounts everything else plus the new `agents.router`.

### Master-side agent registry

- **Schema**: new `agents` table (`name`, `base_url`, `token`, `offsite`, `last_contact_at/status/error`); `targets` gained a nullable `agent_id REFERENCES agents(id)` via the existing `_ensure_column()` pattern.
- **`app/agent_client.py`**: `health()`, `discover()`, `validate()`, `open_backup_stream()`, `run_restore()`, each with an explicit timeout (`SHORT_TIMEOUT=10` for health/discover/validate, `LONG_TIMEOUT=600` for backup/restore), never unbounded. Each call records `agents.last_contact_*` via its own short-lived connection, mirroring `notifications.py`'s side-effect pattern. A well-formed non-2xx response (the agent explicitly reporting an operation failure) is recorded as a successful contact, only an actual `requests.RequestException` counts as an unreachable agent.
- **`app/routes/agents.py`** (master-mode only): `GET/POST /agents` (list + add), `POST /agents/{id}/edit` (name/base_url/token/offsite, so a token rotation doesn't require deleting an agent first), `POST /agents/{id}/delete` (blocked with a friendly message while any target still references it, backed by the real `agent_id` foreign key), `POST /agents/{id}/health-check`.
- **Offsite gating**: `db.list_all_targets()`/`get_target()`/`list_targets()` all `LEFT JOIN agents` to expose `agent_name`/`agent_offsite` on every target row. `scheduler.sync_target_schedule()` and `window_tick()`'s membership filter both additionally require `not target["agent_offsite"]`; manual "run backup now" and restore are completely unaffected (mirroring how Phase 4.5's `enabled` flag already only gates automated dispatch).

### Adding and using a remote target

- `/discover` gained a Local/agent selector; selecting an agent calls `agent_client.discover()` instead of the local Docker loop, with "already added" filtering applied on master's side (scoped to that agent's existing targets), since the agent itself has no state db to do that filtering.
- `/targets/add` and `create_target`/`update_connection` (`routes/targets.py`) gained the same selector / an `agent_id` field. Validation branches to `agent_client.validate()` when `agent_id` is set. `agent_id` is fixed at creation (mirroring how `engine` already is), `update_connection` always passes the target's existing `agent_id` back through rather than the form's (which never includes it), so editing connection details can never accidentally detach a target from its agent.
- `targets/detail.html` gained one line, "Host: `<agent name>` or `local`" (with an `offsite` chip). No other template or route gating anywhere, Connection/Schedule/Retention/Restore/Delete all continue to mean exactly what they mean today for an agent-owned target.
- `Docs/03-Proposed-Architecture.md`'s "Remote agent" section (and the overview diagram) rewritten to describe this centralized design instead of the original peer-pull sketch, with the resolved "push vs pull" open question struck through.

## Deviations from the plan, and why

None. The amended plan's five specific concerns (offsite limitation documented, broad exception handling, explicit timeouts, agent editing, temp-then-rename) were all built exactly as described, and are each covered by a dedicated unit test (see below).

## Testing performed

- `pytest tests/` - 180/180 pass (120 from Phases 1-5, 3 existing scheduler test fixtures updated for the new `agent_offsite` key on plain-dict targets exactly as Phase 4.5's `enabled` key needed the same fix, 57 new tests: `RemoteAdapter` (temp-then-rename, mid-stream failure leaves no partial file, broad exception handling for connection/timeout/write failures, missing-filename-header), `agent_client.py` (every call's timeout, contact-recording distinguishing "reached the agent" from "actually unreachable"), the agent-mode API routes (token dependency, streaming + cleanup on both outcomes, restore's stop/start ordering and failure branches), the master-side registry (create/edit/delete-blocked-while-referenced/health-check), `db.py`'s agents CRUD and the `agent_offsite` joins, the offsite scheduling gate in both `sync_target_schedule()` and `window_tick()`, the discover route's agent-scoped filtering, and the remote create/edit validation branch in `routes/targets.py`).
- **End-to-end integration sanity check**, not just each side tested against its own mock: wired `agent_client.py`'s real request-construction code to a real `agent_api.py` `TestClient` (via a small `requests`-shaped bridge), so the actual wire contract between master and agent was exercised, not two independently-mocked halves. This caught nothing wrong in the app code, but is exactly the kind of check that would have caught a URL/field-name/content-type mismatch between the two sides that isolated unit tests can't see. Verified: token rejection/acceptance, discover round-trip, validate round-trip (rejection case), a full backup round-trip through the real Postgres adapter on the "agent" side with the resulting file correctly landing and matching on the "master" side, a full restore round-trip, and a backup failure (container not found) surfacing as a clean `BackupResult`.
- **`main.py` mode-gating**, run as real subprocesses (not just mocked): confirmed agent mode refuses to start without `AGENT_TOKEN` (`sys.exit(1)` with a clear log message); confirmed agent mode serves zero UI routes (`/`, `/targets/add`, `/agents` all 404) while `/api/health` works correctly with and without the right token; confirmed master mode boots normally, the new Agents nav link and page work end to end (add an agent, see it listed), and the agent-only `/api/health` route does not exist on master.

## Not tested here (needs the real homelab Docker host, per the plan's verification list)

No Docker daemon and no second real host were used in this dev environment, all Docker and network interaction was mocked or bridged in-process. Before considering Phase 6 done, worth confirming on the real homelab host with a genuine second `MODE=agent` deployment:
1. A real agent registration against an actual second host over Tailscale, health-check succeeding/failing appropriately for a wrong token, wrong `base_url`, or an agent that isn't running.
2. Discovering, adding, backing up, and restoring a real Postgres/MySQL/MariaDB/SQLite database on that remote host, end to end, confirming the file genuinely lands in master's central `backup_target_dir` and that retention/GFS tagging behaves identically to a local target.
3. Restoring a remote SQLite target with `stop_container` checked, confirming the container that actually gets stopped/started is the one on the *agent's* host.
4. Editing a live agent's token and confirming the next real remote call picks up the new value with no target needing to be re-added.
5. Flagging a real agent offsite, confirming its targets are genuinely skipped by the shared window/cron over a real fire time, while manual runs still work.
6. Confirming no leftover temp/staging files accumulate on the agent's host across repeated real backups and restores, including a forced failure case (killing the agent mid-transfer).

## Status

Built and tested 2026-07-28. Awaiting real-world verification on the homelab Docker host (with a genuine second agent host) before closeout.
