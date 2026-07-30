# Savepoint - High Level Description

## Elevator pitch

Savepoint is a self-hosted database backup manager for the homelab. It finds the databases running on a Docker host, lets you schedule and configure backups per database (or one shared backup window for all of them), keeps a rolling history using a daily/weekly/monthly retention scheme, and can pull backups from other hosts on the network through a lightweight agent.

## Why

The current setup is a handful of bash scripts (`db-backup-scripts` in `docker-host-compose`) run in sequence by cron. It works, but:

- Only covers a handful of the databases actually running; several others are not backed up at all.
- No visibility. No UI, no history browser, you find out something broke by reading a log file or waiting for an ntfy alert.
- No restore workflow, restoring today means manually running `pg_restore`/`psql` by hand.
- Fixed 7-day retention, no long-term (weekly/monthly) copies.
- Adding a new database means writing and wiring up another bash script by hand.
- Nothing for other hosts (e.g. a future offsite node) beyond copy-pasting the pattern.

## What Savepoint does

- Connects to the Docker socket and detects database containers automatically (Postgres, MySQL, MariaDB) by image name, and reads their env vars to pre-fill connection details.
- Also supports pointing manually at a database, either a container it couldn't autodetect or a SQLite file living inside an app container.
- Runs backups on a schedule, either per-database or inside a shared "backup window" so multiple jobs don't hit the storage target at once.
- Keeps a rolling (GFS-style) history: daily, weekly, monthly copies, with independent retention counts per tier.
- Supports restoring a chosen backup, with an explicit confirmation step.
- Runs as a single Docker image with two modes: `master` (UI, scheduler, orchestration) and `agent` (runs on a remote host, backs up locally, master pulls the result in).
- Sends notifications (ntfy) on failure and on completed runs.

## What it is not

- Not a replacement for Proxmox Backup Server or VM/container-level backups, this is database-content-level only.
- Not a general file backup tool, scope is limited to Postgres, MySQL, MariaDB, and SQLite.
- Not multi-tenant or multi-user, this is a single-operator homelab tool.
