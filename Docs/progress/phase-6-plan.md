# Savepoint - Phase 6 Plan: Remote Agent

## Context

Phases 1-5 cover a single Docker host end to end: discovery, scheduling, GFS retention, and restore, all running as one `MODE=master` process talking to its local Docker socket. `04-Initial-Build-Plan.md`'s Phase 6 scope is the remote-agent piece: other homelab hosts (a LAN host today, a future offsite cottage node) run their own databases with nothing backing them up beyond hand-copying the current bash-script pattern.

**This plan deliberately departs from what `03-Proposed-Architecture.md`'s "Remote agent" section currently describes**, per explicit direction during planning: that doc describes the agent running "identical to master's logic" with its own full local UI, discovery, scheduling, and retention, with master only pulling finished files in on a schedule. The direction for this phase instead is **full centralization**: master is the only UI, the only scheduler, the only state db, and the only retention engine, full stop. An agent is a headless remote executor with no web UI at all, it only runs a small token-authed API that master calls to do, on that remote host, exactly what `docker_client` already does locally: discover candidates, validate a container, run a dump, run a restore. Restoring a remote host's database is done from master's UI, the same Restore section every local target already has, not from anything running on the agent. `03-Proposed-Architecture.md` gets its "Remote agent" section rewritten to match as part of this phase's deliverables, so the docs stay authoritative for future sessions.

An offsite host (the future cottage node, reached over WAN rather than LAN) still registers as an agent the same way, but its targets are never picked up by master's automatic window/cron scheduling, only a manual "run backup now" click ever reaches it, so nothing silently drags a dump across a residential WAN link on a timer. This is a per-agent flag (`offsite`), not a separate mechanism.

## Design decisions - the core reuse: RemoteAdapter

The single most important decision, because it is what keeps this phase from turning into a second parallel codebase: **an agent-owned target is just a target whose `backup()`/`restore()` happens to run over HTTP instead of a local `docker exec`.** Everywhere else in the app, it is indistinguishable from a local target.

- `targets` gains one new nullable column, `agent_id INTEGER NULL REFERENCES agents(id)`. `NULL` means local (100% of existing behavior, existing rows, existing tests, untouched). Every other existing column (`container_name`, `db_user`, `db_name`, `file_path`, `schedule_cron`, `in_window`, `enabled`, `retention_*`) keeps meaning exactly what it already means, just describing a container on the agent's host instead of master's.
- New `app/adapters/remote.py::RemoteAdapter`, parameterized by an `agents` row (not a singleton in the `ADAPTERS` dict the way Postgres/MySQL/MariaDB/SQLite are, since it needs to know *which* agent). It implements `backup(target_row, backup_target_dir) -> BackupResult` (matching the `Adapter` protocol exactly, no signature change) by calling the agent's `/api/backup`, streaming the response body straight to the same local dest-path convention the local adapters already use, and returning a `BackupResult` with that local path, so `jobs.py::_execute()`'s existing success/failure handling, `finish_backup_run()`, and `retention.tag_and_prune()` all run completely unchanged.
- **`RemoteAdapter.backup()` streams to a temp path first, never directly to the final destination.** The response body is written to `<dest_path>.part` (or an equivalent temp suffix) inside the target's backup-target folder; only once the stream has fully and successfully completed does it get renamed (`os.replace()`) to the real `dest_path` that `finish_backup_run()`/retention/restore will treat as a real backup file. A connection drop or any error partway through the stream removes the partial `.part` file instead of leaving it at the final path, so retention/restore can never later pick up a truncated file as if it were a complete, valid backup.
- Restore needs one small branch, because of the stop-container lifecycle: today `restore.py::perform_restore()` stops/starts the *local* container itself, around calling `adapter.restore()`. For a remote target there is no local container to stop, the agent has to do that on its own host, atomically, as part of the same call (two separate network round trips for stop/restore/start would leave a window where a network blip could strand the container stopped). So `RemoteAdapter` gets one extra method, `restore_with_lifecycle(target_row, source_path, stop_container) -> RestoreResult`, called only by `perform_restore()`'s new top-of-function branch (`if target["agent_id"]:`), which uploads the file and lets the agent do stop-restore-start as one request. `RestoreResult` gains one new field, `stopped_container: bool = False` (defaults preserve every existing call site and test), so `perform_restore()` can record what actually happened without needing its own separate stop/start calls for this path. The existing local branch (`agent_id` is `NULL`) is entirely untouched, same stop/start orchestration as Phase 5 built it.
- **Both `RemoteAdapter.backup()` and `restore_with_lifecycle()` catch broadly, not just the documented non-2xx response case.** Connection errors, timeouts, and any exception during the local streaming write (e.g. disk full mid-stream) are all caught and converted into a clean `BackupResult(success=False, error_message=...)` / `RestoreResult(success=False, error_message=...)`, the same way every other adapter failure already flows through the existing failure-recording and `notify_failure()`/`notify_restore_result()` paths, rather than propagating an uncaught exception up through `jobs._execute()`/`perform_restore()`. This directly avoids repeating Phase 5's real bug, where `start_container()`'s caller only caught `NotFound` and anything else propagated uncaught, silently killing a job with no notification and a stuck `running` row.
- `jobs.py::_execute()` changes one line: `adapter = remote.remote_adapter_for(target, _settings) or ADAPTERS[target["engine"]]` (`remote_adapter_for()` returns `None` for a local target, a `RemoteAdapter` instance for an agent-owned one). Everything after that line is unchanged.
- Net effect: `scheduler.py`, `retention.py`, `notifications.py`, `routes/history.py`, and every existing template (`targets/detail.html`, `history_row.html`, `restore_history.html`, `index.html`) need **zero changes** for the backup/restore/retention/history lifecycle. A connection failure to an agent just becomes a normal `BackupResult(success=False, error_message="...")`, which already flows through the existing failure-recording and `notify_failure()` path unchanged, no new notification function needed for that case.
- **Accepted limitation: `POST /api/backup` couples the agent's local dump with streaming it back to master in one synchronous HTTP call.** The `offsite` flag (see below) fully prevents this from ever happening *automatically* on a schedule/window, honoring CLAUDE.md's "agents back up locally first, master pulls it in, no forced WAN transfer by default" wording for anything unattended. But a manual "run backup now" against an offsite target still performs a full synchronous transfer as part of that single call, there is no decoupled "stays local on the agent, gets synced home separately later" state the way `03-Proposed-Architecture.md`'s original design described. This is a deliberate, accepted limitation for now, not an oversight: no genuinely slow-WAN offsite host exists yet (the cottage node is still unbuilt), so there is nothing real to design a decoupled async transfer-later flow against. Revisit with a real design once an actual offsite host and its link characteristics exist.

## Design decisions - agent mode itself

- **Agent mode has no web UI, no Jinja templates, and no state db.** `main.py` mounts `targets`/`discover`/`history`/`settings` routers only when `MODE=master`; agent mode mounts a single new router, `app/routes/agent_api.py`, and nothing else. `db.init_db()`, `retention.reconcile_all()`, and `scheduler.start()` are only called in master mode, an agent has no schedule, no retention config, and no history of its own to track, master's state db is the single source of truth for all of that. This is a genuine simplification versus the originally-documented design, not just a smaller UI: an agent process is little more than "FastAPI + a token check + `docker_client` + the four adapters + a temp-file cleanup sweep."
- **Every agent route requires a bearer token**, checked via a small FastAPI dependency comparing the `Authorization: Bearer <token>` header against a new `AGENT_TOKEN` env var (`config.py` gains `agent_token: str | None`). `main.py` refuses to start in agent mode if `AGENT_TOKEN` is unset, an unauthenticated remote-docker-exec API is not a safe default even on a Tailscale-only network. The operator sets `AGENT_TOKEN` in the agent's own compose file and enters the same value into master's "add agent" form, there is no mechanism for master to push config into a remote host it doesn't control the deployment of, this mirrors how `NTFY_TOKEN` etc. already work (operator-set env var, tools just consume it), it is simply master, not the agent itself, that has to remember the value since it's the one authenticating outbound.
- **Backup and restore are each a single HTTP call, not a two-step stage-then-fetch dance.** `POST /api/backup` (body: `engine`, `container_name`, `db_user`, `db_name`, `file_path`) runs the matching local adapter's `backup()` against a small local temp path, then **streams that file straight back as the response body** (with `X-Savepoint-Method` carrying `method` for the SQLite live-vs-raw-copy distinction, and a non-2xx JSON `{"error": "..."}` body on failure), deleting its own temp file once the stream completes or the request fails. `POST /api/restore` (multipart: the backup file + the same connection fields + `stop_container`) stages the uploaded file to a local temp path, runs the local adapter's `restore()`, optionally stops/starts the container first (reusing `docker_client.stop_container()`/`start_container()`, already built in Phase 5, unchanged), returns `{"success": bool, "stopped_container": bool, "error": str|null}`, and always removes its own temp file in a `finally`. No in-memory staging-id bookkeeping needed on either side.
- **Discovery and connection validation are also plain synchronous calls**, reusing the exact existing logic rather than reimplementing it: `_validate_connection_fields()` moves out of `routes/targets.py` into a new `app/validation.py` (pure move, no behavior change) so both the local route and the new `POST /api/validate` handler call the identical function. Likewise the container-scanning loop in `routes/discover.py` moves into `app/discovery.py::find_candidates(client)` (pure extraction), reused by both the local `/discover` route and the new `GET /api/discover` handler. Since an agent has no state db, `/api/discover` always returns every matching container, "already added" filtering (the thing `existing_names` does locally) happens on master's side instead, scoped to that specific agent's already-known targets (`agent_id` match), exactly the same filtering concept, just applied one level up.
- **`GET /api/health`** is a trivial `{"status": "ok"}`, used by master's "test connection" button when adding/editing an agent, and implicitly exercised by every other call too.

## Design decisions - master-side agent registry

- New `agents` table: `id`, `name` (unique), `base_url`, `token`, `offsite` (`INTEGER NOT NULL DEFAULT 0`), `last_contact_at`, `last_contact_status`, `last_contact_error`, `created_at`. Master stores the token in its own state db in plaintext, the same way `container_name`/`db_user` already are, this is a deliberate, necessary exception to Phase 1's "don't store the database password" precedent: unlike a DB password (which master can always re-read live from the target container's own env vars via the Docker socket), an agent's token isn't locally inspectable at all, master has no choice but to remember what it was configured with.
- A small `app/agent_client.py` wraps every outbound call (`discover`, `validate`, `run_backup`, `run_restore`, `health`) via `requests` (already a dependency, used for ntfy), and updates `agents.last_contact_at/status/error` after each real call (opening its own short-lived connection using `_settings.state_db_path`, mirroring how `notifications.py` manages its own side effects independently of whatever connection a caller happens to hold), so the registry page's "last seen" status reflects actual usage, not just manual health-checks.
- **Every call carries an explicit timeout, mirroring the `timeout=10` pattern `notifications.py` already uses, never an unbounded request.** Fast calls (`health`, `discover`, `validate`) use a short timeout (e.g. 10s). `run_backup`/`run_restore` use a longer timeout appropriate for a real dump/restore duration (e.g. a generous multi-minute value), but always a bounded one, an unreachable or hung agent must never be able to block a request (and, since these calls happen inside a dispatched background job, a stuck `running` row) indefinitely.
- New `app/routes/agents.py` (master-mode only), a single page like `/settings` rather than a per-agent detail page, the surface is small enough: list of registered agents (name, base_url, offsite chip, last-contact pill, local-time-filtered last-contact timestamp), an add-agent form (name, base_url, token, offsite checkbox), and per-row edit/health-check/delete actions. **`POST /agents/{id}/edit`** lets the operator update `name`/`base_url`/`token`/`offsite` in place (e.g. rotating a token or correcting a `base_url` typo) without deleting and re-adding the agent, which is useful precisely because deleting is blocked while any target still references it. Deleting an agent is blocked with a clear message while any target still references it (`targets.agent_id` is a real foreign key under `PRAGMA foreign_keys=ON`, so the database itself refuses the delete either way, the route just checks first for a clean error instead of a raw constraint failure).
- **Offsite gating**: `db.list_all_targets()`/the query `scheduler.py` reads from gets a `LEFT JOIN agents` to expose `agent_offsite` on each target row (one extra column, no new query). `scheduler.sync_target_schedule()` and `window_tick()`'s membership filter both additionally require `not target["agent_offsite"]` before registering a cron job or queuing a window member, an offsite-owned target simply never gets scheduled automatically, exactly mirroring how Phase 4.5's `enabled` flag already gates automated dispatch only, manual runs (`execute_claimed`/`dispatch_manual`, unchanged) always work regardless. Flipping an agent's `offsite` flag off later makes its targets immediately eligible for scheduling again, no target-level changes needed.

## Design decisions - adding a remote target

- `/discover` gains an agent selector (Local vs. each registered agent). Selecting an agent re-queries against `app/agent_client.py::discover(agent)` instead of the local Docker loop, same `discover.html` candidate table, the one-click "Add" form gains a hidden `agent_id` field.
- `/targets/add` (manual add, the documented fallback for anything discovery misses or gets wrong) gains the same optional agent selector, since manual add must support pointing at a remote host's container just as well as a local one.
- `create_target`/`update_connection` (`routes/targets.py`) gain an optional `agent_id` form field. When set, connection validation calls `agent_client.validate(agent, ...)` instead of local `docker_client`/`validation.validate_connection_fields()`; when unset, behavior is byte-for-byte what it is today. `db.create_target()`/`update_target_connection()` gain the `agent_id` param, defaulting `NULL`.
- `targets/detail.html` gains one line near the top, "Host: `<agent name>`" or "Host: local", so it's visually obvious which machine a target lives on. No other template gating anywhere, Connection/Schedule/Retention/Restore/Delete all continue to mean exactly what they mean today, they just happen to run against a `RemoteAdapter` under the hood when `agent_id` is set.

## Project layout changes

```
app/
  adapters/
    remote.py                # NEW: RemoteAdapter (backup, restore_with_lifecycle),
                              # remote_adapter_for(target, settings) factory
  validation.py               # NEW: validate_connection_fields(), moved out of routes/targets.py verbatim
  discovery.py                  # NEW: find_candidates(client), moved out of routes/discover.py verbatim
  agent_client.py                 # NEW: discover(), validate(), run_backup(), run_restore(), health(),
                                   # each updating agents.last_contact_* via its own short-lived connection
  jobs.py                           # _execute(): one-line adapter lookup change (remote_adapter_for(...) or ADAPTERS[...])
  restore.py                         # perform_restore(): new top-of-function branch for target["agent_id"],
                                      # calling RemoteAdapter.restore_with_lifecycle() instead of local stop/restore/start
  scheduler.py                        # sync_target_schedule()/window_tick(): also require `not agent_offsite`
  db.py                                 # agents table CRUD; targets.agent_id column + param threaded through
                                          # create_target()/update_target_connection(); list_all_targets() gains
                                          # agent_offsite via LEFT JOIN
  config.py                               # + agent_token: str | None (AGENT_TOKEN)
  main.py                                  # mode-gated router mounting, mode-gated init_db/reconcile_all/scheduler.start,
                                            # agent mode requires AGENT_TOKEN or exits
  routes/
    targets.py                              # create_target/update_connection: optional agent_id + remote validation branch;
                                             # imports validate_connection_fields from app/validation.py instead of defining it
    discover.py                              # agent selector; local loop now calls app/discovery.py::find_candidates()
    agents.py                                 # NEW (master-mode only): registry list/add/edit/delete/health-check
    agent_api.py                               # NEW (agent-mode only): /api/health, /api/discover, /api/validate,
                                                # /api/backup, /api/restore, all behind the bearer-token dependency
  templates/
    agents.html                                 # NEW: registry page, same single-page shape as settings.html
    discover.html                                # agent selector added
    targets/add.html                              # agent selector added
    targets/detail.html                            # one "Host: ..." line added
    base.html                                       # nav gains "Agents" link when mode == master
schema.sql                                            # + agents table; targets gains agent_id column (migration
                                                       # via the existing _ensure_column() pattern, additive-only)
Docs/
  03-Proposed-Architecture.md                          # "Remote agent" section rewritten to match this design
```

## Data model changes (`schema.sql` + `db.py` migration)

```sql
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    token TEXT NOT NULL,
    offsite INTEGER NOT NULL DEFAULT 0,
    last_contact_at TEXT,
    last_contact_status TEXT,
    last_contact_error TEXT,
    created_at TEXT NOT NULL
);
```

`targets.agent_id INTEGER NULL REFERENCES agents(id)` added via the existing `_ensure_column()` guarded `ALTER TABLE ADD COLUMN` pattern (additive-only, matches every prior phase's migration style, `NULL` default preserves every existing row as "local"). `db.py` gains `create_agent()`, `list_agents()`, `get_agent()`, `update_agent()` (name/base_url/token/offsite, backing the new edit route), `update_agent_contact()`, `delete_agent()` (blocked with a friendly error if any target still references it), and `create_target()`/`update_target_connection()` gain an `agent_id` parameter.

## Routes

Master mode:
- `GET /agents`, `POST /agents`, `POST /agents/{id}/edit`, `POST /agents/{id}/delete`, `POST /agents/{id}/health-check` - the registry page.
- `GET /discover?agent_id=` - existing route, extended.
- `POST /targets`, `POST /targets/{id}/edit` - existing routes, extended with the optional `agent_id` field.

Agent mode (all behind the bearer-token dependency):
- `GET /api/health`
- `GET /api/discover`
- `POST /api/validate`
- `POST /api/backup` (streams the resulting file back, or a JSON error)
- `POST /api/restore` (multipart upload, JSON result)

## Verification plan

1. Register a real second host (or a second container acting as a stand-in agent on the test LXC) running `MODE=agent` with `AGENT_TOKEN` set, confirm the health-check button succeeds and fails appropriately (wrong token, wrong `base_url`, agent not running).
2. Confirm agent mode genuinely serves no HTML routes, a plain browser hit to the agent's root or any local-mode path 404s.
3. Discover a real Postgres/MySQL/MariaDB container on the agent host via `/discover?agent_id=`, confirm the candidate list matches what a local `/discover` would show for an equivalent container, confirm "already added" filtering works (add one, re-run discovery, it's no longer listed).
4. Add a remote target manually via `/targets/add` with an agent selected, confirming validation correctly rejects a nonexistent remote container name and accepts a real one.
5. Run a manual backup against a remote target end to end: confirm the file lands in master's own `backup_target_dir` under the normal naming convention, confirm `backup_runs`/retention tagging behave identically to a local backup, confirm the agent's own temp file is gone afterward.
6. Restore a remote target end to end, both with and without `stop_container` (where applicable to the engine), confirm the container on the *agent's* host is the one that gets stopped/started, confirm `restore_runs.stopped_container` is recorded correctly, confirm the agent's temp upload is cleaned up on both success and a forced failure.
7. Confirm a scheduled/window-triggered backup against a remote target behaves exactly like a local one from the operator's point of view (same history row shapes, same ntfy failure notification if the agent is unreachable).
8. Flag an agent as offsite, confirm its targets are silently excluded from the shared window and from cron scheduling (wait past a real fire time, nothing runs), confirm a manual "run backup now" still works against it regardless.
9. Un-flag offsite, confirm scheduling resumes without needing any change on the target itself.
10. Attempt to delete an agent that still has targets attached, confirm it's rejected with a clear message, no partial deletion.
11. Edit an agent's token (and separately its `base_url`), confirm subsequent calls use the new value, confirm existing targets that reference the agent keep working unaffected, no need to re-add them.
12. Simulate a dropped connection partway through a real backup transfer (e.g. kill the agent process mid-stream), confirm no partial/truncated file is left at the target's real destination path in `backup_target_dir`, only a normal `failure` row and (if configured) an ntfy notification, exactly like any other backup failure.
13. Unit tests (mocked `requests`, no live second host): `RemoteAdapter.backup()`/`restore_with_lifecycle()` request construction and response handling (success, non-2xx error, connection failure, timeout, and an unexpected exception during the streaming write, all surfaced as a normal `BackupResult`/`RestoreResult` failure rather than an uncaught exception); a mid-stream failure leaving no partial file at the final destination path (temp-path-then-rename behavior); the offsite scheduling filter in both `sync_target_schedule()` and `window_tick()`; `agents` CRUD including `update_agent()` and the delete-blocked-while-referenced case; every `agent_client.py` call passing an explicit timeout; the agent-mode API handlers (`/api/backup` streaming + temp-file cleanup on both success and failure, `/api/restore` multipart handling + stop/start ordering, the bearer-token dependency rejecting a missing/wrong token); `app/validation.py`/`app/discovery.py` behave identically to their pre-extraction versions (existing local-mode tests continue passing verbatim against the moved code).

## Real-world verification results

The verification plan above was run against a genuine second host, a separate throwaway LXC running `MODE=agent`, not just the mocked/bridged tests recorded in `phase-6-build-summary.md`. This confirms Phase 6's core design goal, a remote target is indistinguishable from a local one everywhere except the adapter, held up under real cross-host testing:

- Every agent route requires a valid bearer token, including `/api/health`, confirmed a request with no `Authorization` header is rejected: confirmed.
- Agent mode serves zero UI routes (`/`, `/targets/add`, etc. all 404), only the `/api/*` surface exists: confirmed.
- Discovery, manual add, manual backup, and manual restore against a real remote Postgres container all work end to end: confirmed.
- The resulting backup file lands correctly in master's own `backup_target_dir` under the normal naming convention (reusing the filename the agent's own local adapter generated, via the `X-Savepoint-Filename` header), and retention/GFS tagging behaves identically to a local target: confirmed.
- Agent-side temp file cleanup after a successful transfer: confirmed.
- Remote SQLite restore with `stop_container` checked correctly stops and restarts the container on the agent's host, not master's, confirmed by watching container uptime reset to seconds immediately after a restore: confirmed.
- Editing an agent's token from master's registry correctly updates subsequent calls without needing to re-add any of its targets: confirmed.
- A forced mid-stream failure (`docker kill` on the agent process during a real, artificially slowed Postgres backup) resulted in a clean `failure` row on master, not stuck at `running`, and no partial/truncated file at the real destination path: confirmed. Noted as an expected, inherent limitation, not a bug: SIGKILL gives zero opportunity for any cleanup code to run, so a leftover temp directory on the agent side after a hard kill is unavoidable by any software; a graceful network drop (the actually realistic failure mode) would trigger normal exception handling and cleanup via the agent's own `finally` block. The startup sweep (see fix below) provides ongoing hygiene against this specific hard-crash case rather than trying to prevent the unpreventable.
- "Already added" filtering during discovery is correctly scoped per agent (a fresh container shows as a candidate, an already-tracked one is excluded), tested with mixed Postgres/MySQL targets on the same agent: confirmed.
- The offsite flag correctly blocks a target's cron schedule from firing immediately upon being set (after the `sync_target_schedule` fix below), manual runs remain unaffected, and un-flagging resumes scheduling without any target-level change needed: confirmed.
- Window-triggered scheduling correctly respected the offsite flag from the start (no fix needed there, `window_tick()` re-queries fresh on every fire).

### Fixes applied during verification

- **`agents.py`'s edit route** now calls `scheduler.sync_target_schedule()` for every target belonging to an agent whenever `offsite` is toggled, so cron jobs are immediately unregistered/re-registered rather than waiting for an unrelated schedule save or app restart (`db.list_targets_for_agent()` added to support this).
- **`GET /discover`** accepts `agent_id` as a string and converts manually, fixing a 422 when switching the dropdown back to "Local" (the option submits `agent_id=""`, which a plain `Optional[int]` query param rejects before the route body runs).
- **Agent mode's startup sweep** (`agent_api.sweep_stale_staging_dirs()`, called from `main.py` only when `MODE=agent`) clears stale temp staging directories left behind by a hard crash (SIGKILL) mid-transfer, so repeated crashes don't accumulate them indefinitely.

## Status

Plan drafted 2026-07-28, amended 2026-07-28 (documented the offsite manual-transfer limitation explicitly; broad exception handling in RemoteAdapter matching Phase 5's start_container() lesson; explicit per-call timeouts in agent_client.py; added agent editing via POST /agents/{id}/edit; RemoteAdapter.backup() writes to a temp path and renames on success, never leaving a partial file at the final destination), approved and built 2026-07-28. Fully verified against a real second host (throwaway agent LXC) and closed 2026-07-28.
