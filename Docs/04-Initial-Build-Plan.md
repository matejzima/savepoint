# Savepoint - Initial Build Plan (Phased)

Follows the same phased approach as Homebase: Claude Code writes a plan for the phase, Matej reviews and approves, then Claude Code builds it. Do not start a phase's code before its plan is approved.

## Phase 1 - Core skeleton, Postgres only, manual add

Goal: prove the core loop end to end with the simplest possible scope.

- FastAPI app skeleton, Jinja2/HTMX UI shell.
- SQLite app state db (targets, backup history).
- Manual "add database" flow: container + connection details, Postgres only.
- Postgres adapter: `backup()` via `docker exec` + `pg_dump`, writes to a configured bind-mounted target.
- One-off manual "run backup now" button, no scheduling yet.
- Basic backup history list (what ran, when, success/failure, file location).
- No retention/pruning yet, no restore yet, no discovery yet.

## Phase 2 - Discovery and remaining SQL engines

- Docker socket discovery: image keyword match for Postgres/MySQL/MariaDB, env var pre-fill.
- MySQL and MariaDB adapters.
- SQLite adapter (manual file path only at this stage, discovery heuristic comes with it or can slip to this phase depending on complexity).
- Discovered-candidate UI: list of suggestions, one-click confirm to add, never auto-added.
- Manual add still available in parallel for anything discovery misses.

## Phase 3 - Scheduling and backup window

- Per-database cron-style schedule.
- Master backup window config (start time, duration, concurrency cap).
- Scheduler enqueues and runs jobs respecting the window + concurrency cap.
- ntfy notifications on failure and on run completion.

## Phase 4 - Rolling (GFS) retention

- Tagging logic on successful backup (daily/weekly/monthly based on date rules).
- Per-tier retention counts, configurable per database.
- Prune sweep that removes backups once they hold no tags.
- Retention visible in the UI (which tier(s) a given backup belongs to).

## Phase 5 - Restore workflow

- Pick a backup from history, explicit confirmation step.
- Restore execution per adapter.
- Safety handling for live-vs-stopped target where the engine matters (Postgres/MySQL/MariaDB especially).

## Phase 6 - Remote agent

- `MODE=agent` behavior: local discovery, local scheduling, local backup target, token-authed pull endpoint.
- Master-side agent registry: add an agent by `ip:port` + token, pull schedule.
- LAN pull-in flow first, offsite/deferred-sync configuration option second.

## Phase 7 - Polish

- Forward-auth support implemented (env-var configured, off by default, no auth for local/LAN-only use).
- UI pass: dashboard overview, per-database detail view, clearer failure states.
- Resource limits, compose file finalized to match homelab convention.
- Decide and document the transition plan away from the legacy `db-backup-scripts` bash setup.

## Notes for Claude Code

- Each phase gets its own plan doc and build summary, same convention as Homebase (`docs/progress/` style), stored wherever this repo's docs live.
- Do not jump ahead to a later phase's code while an earlier phase is still open.
- Flag any open question from `03-Proposed-Architecture.md` at the start of the relevant phase's plan rather than guessing silently.
