# Savepoint - Proposed Architecture

## Overview

Single Python codebase, single Docker image, two run modes controlled by `MODE=master|agent`.

```
+------------------------------------------------+
|              Savepoint (master)                 |
|  FastAPI app + APScheduler + Jinja2/HTMX        |
|                                                  |
|  - Only UI, only scheduler, only state db       |
|  - Local Docker socket discovery                |
|  - Adapter layer (pg/mysql/mariadb/sqlite)      |
|  - RemoteAdapter (talks to registered agents)   |
|  - Scheduler + backup window orchestration      |
|  - GFS retention/pruning (all targets, local or |
|    agent-owned, retained centrally the same way)|
|  - Restore workflow (including agent-owned      |
|    targets, triggered from master's own UI)     |
|  - Agent registry (name, base_url, token,       |
|    offsite flag)                                |
|  - ntfy notifications                           |
+------------------+-------------------------------+
                   |
   token-authed HTTP: discover / validate /
        run backup (streams file back) /
      run restore (multipart upload + result)
                   |
+------------------v-------------------------------+
|         Savepoint (agent, remote host)           |
|  Same image, MODE=agent                          |
|  - No web UI, no Jinja templates, no state db    |
|  - Local Docker socket discovery, on request     |
|  - Runs the same adapter layer locally, on       |
|    request, exposes only a small token-authed    |
|    API: /api/health, /api/discover, /api/validate,|
|    /api/backup, /api/restore                     |
+---------------------------------------------------+
```

## Backend

- FastAPI for the HTTP layer (UI routes + a small internal API for HTMX partials and the agent-pull endpoint).
- APScheduler for cron-style and interval scheduling, running in-process.
- Docker SDK for Python for all container introspection (list containers, inspect image/env/mounts, `exec` for dump commands). No shelling out to raw `docker` CLI.

## Adapter layer

Common interface, one implementation per engine:

- `discover()` - can this adapter claim a given container (image name match)?
- `default_connection_info(container)` - pull user/db/etc. from env vars.
- `backup(target, destination)` - run the dump, write to destination.
- `restore(target, source)` - run the restore.

Implementations:
- **Postgres**: `pg_dump`/`pg_restore` via `docker exec` into the DB container, same technique the current bash scripts already use.
- **MySQL / MariaDB**: `mysqldump`/`mysql` via `docker exec`, both engines share the same adapter given dump-format compatibility, differentiate only where connection defaults differ.
- **SQLite**: file-level copy (or `.backup` command via `sqlite3` if available in the target container) of the manually-specified file path. No `docker exec` dump step needed, just a consistent-copy of the file.

## Discovery

1. List all containers via Docker socket.
2. For each, match image name against known keywords (`postgres`, `mysql`, `mariadb`).
3. On match, inspect env vars for that image family to pre-fill connection details, surface as a discovered candidate in the UI (not auto-added, needs a one-click confirm).
4. Separately, scan mounted volumes of non-matched containers for SQLite-looking files (`.db`/`.sqlite`/`.sqlite3` extensions), surface as a suggestion, always requires manual confirmation before becoming a tracked target.
5. Manual add is always available as a parallel path, regardless of what discovery finds, this is the fallback/override for anything discovery gets wrong or can't see.

## Scheduling and orchestration

- Each tracked database has either:
  - Its own cron-style schedule, or
  - Membership in the shared "backup window" (start time + duration + concurrency cap).
- The scheduler enqueues window-based jobs at window start and runs them with the configured concurrency cap (e.g. max 2 at once), queueing the rest, so the backup target isn't hit by every job simultaneously.
- Per-database schedules run independently of the window and bypass the concurrency cap (they're expected to be the exception, not the norm).

## Retention (GFS)

- On successful backup, tag it with a tier based on the date it ran:
  - Always counts as a `daily`.
  - If it's the first successful backup of the ISO week, also tag `weekly`.
  - If it's the first successful backup of the calendar month, also tag `monthly`.
- A backup can hold multiple tags at once (a Monday-morning backup that's also the first of the month is both `daily` and `monthly`).
- A separate prune sweep runs after each backup (or on its own schedule) per tier: keep the N most recent backups carrying that tag, delete the rest of that tag's membership (a file only gets physically deleted once it holds no tags at all).

## Storage layout

```
/backup-target/
  <database-name>/
    <database-name>_<timestamp>.<ext>
```

- Mounted from the host, e.g. `/mnt/core/backups/db:/backup-target`.
- Metadata (which files are tagged daily/weekly/monthly, schedule config, discovered targets, connection details) lives in Savepoint's own SQLite state db, not inferred from the filesystem. The filesystem holds the actual backup files, the state db holds everything about them.

## Restore workflow

1. User picks a tracked database and a specific backup from its history in the UI.
2. Explicit confirmation step (type-to-confirm or similar, this is destructive).
3. Savepoint runs the adapter's `restore()`, targeting the same container/connection the backup came from by default.
4. Where the engine allows it, prefer restoring against a stopped or isolated instance to avoid corrupting a live database mid-write; where it doesn't (e.g. restoring into a running container is the only option), warn clearly in the UI before proceeding.

## Remote agent

**Revised in Phase 6: fully centralized, not the peer-pull design originally sketched above.** Master is the only UI, the only scheduler, the only state db, and the only retention engine, full stop. An agent is a headless remote executor with no web UI, no Jinja templates, and no state db of its own, it only runs a small token-authed API that master calls to do, on that remote host, exactly what the Docker SDK already does locally: discover candidates, validate a container, run a dump, run a restore.

- Same image, `MODE=agent`. Refuses to start without `AGENT_TOKEN` set, an unauthenticated remote-docker-exec API is not a safe default even on a Tailscale-only network.
- An agent-owned target is, from every other part of the app's perspective, just a target whose `backup()`/`restore()` happens to run over HTTP instead of a local `docker exec` (`app/adapters/remote.py::RemoteAdapter`). Scheduling, GFS retention, restore, history, and notifications all run unmodified against it, master's scheduler dispatches it exactly like a local target, RemoteAdapter is the only thing that differs underneath.
- Agent-side API, all behind a bearer token (`Authorization: Bearer <AGENT_TOKEN>`):
  - `GET /api/health` - reachability check.
  - `GET /api/discover` - every container matching a known database image on that host (master filters out ones it already tracks, the agent has no state db to do that filtering itself).
  - `POST /api/validate` - the same connection-field validation local target add/edit already does, just run against the agent's own Docker socket.
  - `POST /api/backup` - runs the matching local adapter's `backup()` into a small local temp path, then streams that file straight back as the response body (or a JSON error on failure), deleting its own temp file once the response completes.
  - `POST /api/restore` - stages an uploaded backup file locally, runs the matching adapter's `restore()`, optionally stops/starts the container around it (the only place stop/start-around-restore happens for a remote target, atomically, in the same request, since a separate stop-then-restore-then-start round trip over the network could strand a container stopped if a request in the middle failed), always cleans up its own staged files afterward.
- Master-side agent registry (`agents` table: name, `base_url`, token, `offsite` flag, last-contact status) lives entirely in master's own state db. The operator sets `AGENT_TOKEN` in the agent's own compose file and enters the same value into master's "add agent" form, there is no mechanism for master to push config into a remote host it doesn't control the deployment of.
- **Offsite hosts** (e.g. a future cottage node, reached over WAN rather than LAN) are flagged `offsite` on the agent registry entry. This excludes every target owned by that agent from master's automatic window/cron scheduling entirely, a manual "run backup now" click still works regardless, so nothing silently drags a dump across a residential WAN link on a timer. **Accepted limitation:** because backup is a single synchronous "run it, then stream the result back" HTTP call (see `POST /api/backup` above), a manual run against an offsite target still performs a full synchronous transfer as part of that one click, there is no decoupled "stays local on the agent, gets synced home separately later" state. This is deliberate for now (no real offsite host with meaningfully slow WAN characteristics exists yet), revisit with an actual async transfer-later design once one does.
- Transport: Tailscale, matching how the rest of the homelab does remote/admin access already. No public exposure, the bearer token is defense-in-depth on top of that, not the sole security boundary.

## Notifications

- ntfy, matching the existing pattern (`db-backup-scripts` posts to the `/cron` topic today). Savepoint should use its own topic to keep it distinguishable from the legacy scripts during the transition period, configurable via env var.
- Notify on: job failure (per database), and a summary after each backup window/run completes.

## Auth

- Default: no auth. Savepoint assumes local/LAN-only exposure (Tailscale or internal network, never public), same trust model as Dozzle/Dockhand today.
- Optional forward-auth support via env variables, so it can sit behind Authentik (or any other forward-auth proxy) when the operator wants MFA/passkey enforcement in front of it. When enabled, Savepoint trusts the identity headers passed by the proxy rather than implementing its own login.
- No built-in username/password login is planned, forward-auth is the only auth path if one is wanted.

## Deployment

- `/opt/compose/savepoint/docker-compose.yml` for master.
- Same image, separate compose file (or profile) for any agent deployed on another host.
- Docker socket mounted read-write (needed for `exec`-based dumps, same as Dozzle/Dockhand already do on this host).
- Resource limits per homelab convention (`deploy.resources.limits` + top-level `memswap_limit`).
- App config via `.env`, no hardcoded secrets.

## Open questions to resolve during planning

- Exact env var names to read per adapter (varies slightly by image maintainer, e.g. `linuxserver/mariadb` vs official `mariadb` image).
- ~~Whether agent-to-master uses agent-initiated push registration or master-initiated pull-only~~ Resolved in Phase 6: master-initiated only, for every operation, the agent never calls out to master, it only answers requests master makes to its token-authed API.
