# CLAUDE.md - Savepoint

Guidance for Claude Code when working on this project.

## What this project is

Savepoint is a self-hosted web app that discovers, schedules, and manages database backups across a homelab. It replaces ad hoc bash + cron scripts with a proper UI, autodiscovery, and rolling (GFS) retention.

Read these files in this folder before starting any work, in order:
1. `01-High-Level-Description.md`
2. `02-Requirements.md`
3. `03-Proposed-Architecture.md`
4. `04-Initial-Build-Plan.md`

## Tech stack

- Backend: Python, FastAPI
- Scheduler: APScheduler
- DB access: Docker SDK for Python (talks to `/var/run/docker.sock`)
- App state storage: SQLite (its own metadata db: configured targets, schedules, backup history, retention tags)
- Frontend: server-rendered (Jinja2 + HTMX preferred over a separate SPA, keep this simple and Python-only unless a real need for a JS framework shows up)
- Supported backup engines: Postgres, MySQL, MariaDB, SQLite

## Conventions to follow

- No em-dashes ("-") anywhere, not in code comments, docs, commit messages, or UI copy. Use "-" or a period instead.
- This repo is public: never reference real homelab host names or the specific list of other production services anywhere in it (code, docs, commit messages, UI copy). Keep examples generic (e.g. "a second host", "a NAS or spare machine") instead of naming actual infrastructure.
- Docker Compose conventions match the rest of the homelab (this operator's own private compose repo, not part of this project):
  - `deploy.resources.limits` (cpus + memory) for resource limits
  - top-level `memswap_limit` (not nested under `deploy.resources.limits`), roughly 1.5x memory for larger services
  - one stack per app, own directory
- Storage paths: default backup target is a bind mount, e.g. `/mnt/core/backups/db:/backup-target`. Never hardcode a homelab-specific path into the app itself, it must be configurable per deployment.
- Single Docker image, mode controlled via env var: `MODE=master` or `MODE=agent`. Do not build two separate images.
- Config and secrets (DB credentials, ntfy tokens, etc.) via environment variables or a mounted `.env`, never hardcoded.
- Keep adapters (Postgres / MySQL / MariaDB / SQLite) behind a common interface so adding a new engine later does not touch scheduling, retention, or UI code.
- Containers running scheduled/timed logic must have `tzdata` installed and `TZ` set to `Europe/Prague`, matching the rest of the homelab's convention. Do not leave this to default to UTC, a container with no timezone data installed silently ignores `TZ` and schedules fire at the wrong wall-clock time.

## Build process

This project follows a phased build with review gates, same pattern as the Homebase project:

1. Claude Code writes a PLAN only (no code) for the phase in question.
2. Matej reviews and approves the plan before any code is written.
3. Claude Code builds the phase.
4. Claude Code writes a short build summary.
5. Basic testing / sanity check.
6. Git commit + push.

Do not skip ahead to a later phase before the current one is reviewed and approved. Do not write code during the planning step.

## Things to actively avoid

- Do not assume Redis or any in-memory cache needs backing up, it is explicitly out of scope.
- Do not force remote agents to copy data over WAN by default, agents back up locally first, master pulls it in (see architecture doc for LAN vs offsite handling).
- Do not build a heavier frontend stack (React/Vue/etc.) unless the HTMX approach genuinely can't do the job, this is meant to stay simple.
- Do not silently auto-add discovered SQLite files as backup targets, always require manual confirmation since file discovery there is a heuristic, not a certainty.
