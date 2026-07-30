# Savepoint - Requirements

## Core

- Web UI (server-rendered, no separate SPA build step required).
- Runs as a Docker container, deployable the same way as the rest of the homelab stacks (compose file, `/opt/compose/savepoint/`).
- Single image, mode set via `MODE=master` or `MODE=agent` env var.

## Database support

- Postgres
- MySQL
- MariaDB
- SQLite

Redis is explicitly out of scope (in-memory cache, not durable data worth backing up).

## Discovery

- Autodiscovery of local database containers via the Docker socket.
- Detection method: match container image name against known keywords (`postgres`, `mysql`, `mariadb`).
- For matched containers, read environment variables to pre-fill connection details (user, database name, etc. - exact var names depend on image, e.g. `POSTGRES_USER`/`POSTGRES_DB` vs `MYSQL_USER`/`MYSQL_DATABASE`).
- SQLite cannot be autodiscovered the same way (no dedicated container). Best-effort discovery: scan mounted volumes of non-DB-image containers for `.db`/`.sqlite`/`.sqlite3` files, surface as a suggestion requiring manual confirmation, never auto-add.
- Manual override always available: point directly at a container + connection details, or a container + file path for SQLite, regardless of what autodiscovery found. This also acts as a fallback for anything autodiscovery gets wrong.

## Backup target

- Backup target is a bind-mounted volume, e.g. `/mnt/core/backups/db:/backup-target` inside the container.
- Path must be configurable, not hardcoded.

## Scheduling

- Two modes, configurable:
  1. Per-database schedule (e.g. "back this one up at 3am daily").
  2. Master backup window (e.g. 02:00-04:00) where Savepoint orchestrates all jobs assigned to the window, with a concurrency cap so the backup target isn't hit by every job simultaneously.
- Per-database schedule can override the master window for anything that needs to run outside it.

## Retention

- Rolling (GFS-style) retention: daily, weekly, monthly tiers.
- Default example: 7 daily, 4 weekly, 2 monthly, but counts must be configurable per database.
- Tag each backup with its tier at creation time (based on date, e.g. first successful backup of the ISO week = weekly candidate), not derived retroactively from a flat file listing.
- Separate prune step per tier removes backups beyond the configured count for that tier.

## Restore

- Restore workflow: pick a backup from history, explicit confirmation step before it runs.
- Restore should avoid corrupting a live database where possible (e.g. against a stopped/isolated target where the engine allows it).

## Remote agent

- Same Docker image as master, started in `agent` mode via env var.
- Agent connects outward or is reachable via `ip:port` configured on the master (exact direction TBD in architecture doc).
- Agent mode backs up locally on the remote host first (its own local backup target), master then pulls the result over the network into the central backup target.
- No forced WAN transfer for offsite hosts. LAN hosts get pulled in normally. Offsite hosts (e.g. a future cottage node) write locally by default; syncing that back home is a separate, opt-in concern, not baked into the backup job.

## Notifications

- ntfy integration for failure alerts and completed-run summaries, matching the pattern already used elsewhere in the homelab.

## Auth

- No auth by default, Savepoint is meant for local/LAN-only exposure (Tailscale or internal network).
- Optional support for forward-auth (e.g. Authentik), configured via env variables, for anyone who wants MFA/passkey enforcement in front of it.

## Non-functional

- No em-dashes anywhere in generated content, docs, UI copy, or commit messages.
- Resource limits (CPU/memory) on the container, following homelab convention (`deploy.resources.limits` + top-level `memswap_limit`).
- Should be able to run alongside the existing `db-backup-scripts` bash setup during a transition period without conflicting (different backup file naming/location is fine).
