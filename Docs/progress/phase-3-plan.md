# Savepoint - Phase 3 Plan: Scheduling and the Backup Window

## Context

Phases 1-2 proved manual backups end to end for all four engines, with discovery for three of them. Everything so far runs synchronously inside the HTTP request that triggered it, an explicit Phase 1 stopgap ("Execution model" decision in `phase-1-plan.md`) meant to last only until real job orchestration existed. Phase 3, as scoped in `Docs/04-Initial-Build-Plan.md`, is that orchestration:

- Per-database cron-style schedules.
- A shared backup window (start time, duration, concurrency cap) that targets can join instead of having their own schedule.
- ntfy notifications on failure and on a window summary, using Savepoint's own topic.

This phase is as much an architectural change as a feature: it moves backup execution out of the request/response cycle entirely, onto APScheduler, and the plan below exists mainly to settle that shift before any code is written.

## Design decisions

- **One execution path for every trigger source**: `app/jobs.py::run_backup(target_id, triggered_by, run_id=None)` (`triggered_by` is `"manual"`, `"schedule"`, or `"window"`) is the only function that ever calls `ADAPTERS[engine].backup(...)`. The manual "run now" button, per-target cron jobs, and window worker threads all call this same function, with no separate manual-only or scheduler-only code path. It opens its own `db.get_connection()` (not the request-scoped `Depends(get_db_conn)` from `app/deps.py`, since scheduled/window calls have no request), looks up the target, and does everything from recording the `backup_runs` row through calling the adapter and finishing the result. `run_id` is `None` for manual/schedule calls (the function creates its own row), and set for window calls, where `window_tick()` already created the row as `queued` up front and this call just updates it in place rather than inserting a second one.
- **Manual "run now" is dispatch, not execution**: `POST /targets/{id}/run` no longer runs the adapter inline. It does a fast, non-blocking check (see per-target locking below), and if the target is free, submits `run_backup(target_id, "manual")` to APScheduler as a one-off immediate job (`scheduler.add_job(run_backup, args=[...])`, no trigger, i.e. "run once, now") and returns right away. APScheduler's own worker thread pool executes it; no second thread pool is introduced for "manual" as distinct from "scheduled". Manual runs bypass the window concurrency cap the same way per-database schedules do (see below), since a one-off user click isn't the batch-of-simultaneous-jobs problem the cap exists to solve.
- **Per-target locking, applied to every trigger source**: a single module-level `threading.Lock()` guarding a `set()` of in-progress target IDs in `app/jobs.py`, living for the process lifetime (per-container, matches "in-process lock is fine, no distributed lock needed"). Before anything else, `run_backup()` tries to add `target_id` to that set; if it's already there, the target has a run in flight from *any* source, and this call does not start a second one. What happens next differs by trigger source, since the two situations aren't equally surprising to whoever's watching:
  - **Schedule or window collision**: unattended, nobody's watching in real time, so the operator needs a record it didn't happen. For a schedule-triggered collision, a fresh `backup_runs` row is created with `status = "skipped"` and an `error_message` naming the run already in progress. For a window-triggered collision, the row already exists (window members get a `queued` row up front, see below), so that same row is updated to `status = "skipped"` with the same kind of message, rather than a second row being created.
  - **Manual collision**: the user is looking at the page right now. No new row is created (a `skipped` row for every impatient double-click would clutter history for no reason); the request just re-renders the current history state with a small inline notice, "a backup for this target is already running."
- **Window concurrency is enforced by a bounded worker pool, not a semaphore handed to independent jobs**: the window doesn't fire-and-forget one job per member at window start, it runs continuously from window start until either every member has been backed up or the window's configured duration elapses, whichever comes first, exactly matching how the window is meant to behave. At window start, `window_tick()` creates a `queued` `backup_runs` row for every `in_window=1` target up front (see below), pushes those `(target_id, run_id)` pairs onto a `queue.Queue`, and starts exactly `concurrency` worker threads. Each worker loops: check whether the window's deadline (`start + duration_minutes`) has passed, if so stop; otherwise pop the next pair off the queue (stop if empty) and call `run_backup(target_id, "window", run_id=run_id)`, which blocks that worker until the job finishes. `window_tick()` joins all workers before doing anything else, so the concurrency cap falls directly out of "only `concurrency` threads exist to pull work", no semaphore object needs to be constructed or threaded through independently-dispatched jobs.
- **`queued` is a real, visible status, not just an implementation detail**: a window with, say, 8 member targets and a cap of 2 means several targets sit waiting for a meaningful amount of time, this is the entire point of the cap, so every member gets its `queued` row the moment the window starts, not only once a worker happens to reach it. `backup_runs.status` gains two new legal values this phase: `queued` and `skipped` (on top of the existing `running`/`success`/`failure`), still the same TEXT column, no schema change needed for that part. `started_at` keeps its existing meaning (row creation time); no separate "actually started running" timestamp is added, precise queue-wait analytics aren't a goal here, and adding one would be complexity without a driving need.
- **Duration cutoff leaves unclaimed members visibly `skipped`, not silently dropped**: once every worker has exited (queue drained, or the deadline passed and there was nothing left to pop), `window_tick()` re-reads the status of every `run_id` it created at window start; any still sitting at `queued` (never claimed by a worker before the deadline) get updated to `status = "skipped"` with an `error_message` noting the window closed before it could run. An in-progress run is never killed to make room for this, consistent with duration being advisory (see below), this only accounts for members that never got picked up in the first place.
- **UI reflects background jobs via HTMX polling, not JS**: `partials/history_row.html` conditionally adds `hx-get="/targets/{id}/history" hx-trigger="every 2s" hx-swap="outerHTML"` to its own root element, but only when at least one of its rows is `queued` or `running`. Once every row is terminal, the next server-rendered swap simply omits those attributes and HTMX stops polling on its own, since it re-reads hx-attributes from whatever HTML was just swapped in. No websockets, no SSE, no hand-written JS, this is the same pattern HTMX already uses for the existing spinner/indicator, just applied to the whole table instead of one button click. A new `GET /targets/{id}/history` route (in `history.py`) serves both the initial page include and the polling refreshes from one shared render helper, so there's one place that decides "does this table need to keep polling."
- **Backup window duration is advisory, not enforced**: the window's `duration_minutes` setting is shown in the UI (e.g. "window: 02:00 for 2h") but nothing force-kills a job once the duration elapses. Forcibly cancelling a `docker exec` mid-dump is a real can of worms (partial files, an engine left in an inconsistent state) for a problem this phase doesn't need to solve, a long-running job just runs long; the cap already limits how many run at once.
- **Scheduling storage, and window membership as the encouraged default**: `targets` gains `schedule_cron` (nullable cron string) and `in_window` (boolean), configured via a new "Schedule" section on the existing target detail page (not bundled into the add-target form, since scheduling is naturally a second step after a target already exists and has been test-backed-up manually). The two are mutually exclusive, enforced server-side: setting one clears the other. The form itself is not a neutral either/or choice: window membership is the pre-selected default for any target with no schedule configured yet, matching the point of having a shared window at all (predictable, capped-concurrency backup timing the operator doesn't have to think about per target). Choosing a per-target cron schedule instead is framed as an explicit override for the exception case, a target that genuinely needs a specific time no other target uses, and selecting it shows a plain warning in the form: "this bypasses the shared window's concurrency limit and runs independently, use only if this target needs a specific time." Cron strings are validated by attempting `CronTrigger.from_crontab(value)` (APScheduler's own parser) before saving, a `ValueError` becomes a clear form error.
- **Window configuration storage**: a new small `settings` key-value table (`window_start`, `window_duration_minutes`, `window_concurrency`), editable via a new `GET/POST /settings` page, with code-level defaults (`02:00`, `120`, `2`) if a key is absent. This is operator-tunable behavior an operator will reasonably want to change without a redeploy, unlike `BACKUP_TARGET_DIR`-style deployment-time config which stays an env var, that's the line drawn between "env var" and "settings table" here.
- **Live schedule sync, no restart required**: `app/scheduler.py::sync_target_schedule(scheduler, target)` adds, updates, or removes that target's APScheduler job (keyed `f"target-{id}"`) to match its current `schedule_cron`. Called once per target at startup (after `db.init_db()`) and again every time a target's schedule is saved via the UI, so an edit takes effect immediately, this is the difference between scheduling actually working and only working after the next container restart.
- **Window firing**: one APScheduler cron job (`CronTrigger` built from the `window_start` setting, re-registered at startup and whenever `/settings` is saved) calls `window_tick()` at window start. `window_tick()` reads the `in_window=1` targets and the current `window_duration_minutes`/`window_concurrency` settings, creates a `queued` `backup_runs` row for each member, queues them, and runs the bounded worker pool described above (`concurrency` worker threads pulling from the queue until it's empty or the deadline passes). It blocks (joining all workers) until that pool has fully drained, then sweeps any still-`queued` rows to `skipped` ("window closed before this could run") and calls `notify_window_summary()`. "All workers have exited, queue empty or duration elapsed, nothing still running" is the exact condition that marks the window run complete and triggers the summary, there's no separate completion signal to track.
- **ntfy**: `NTFY_URL` and `NTFY_TOPIC` env vars (both optional, notifications are silently skipped if unset, this feature doesn't hard-fail the app). `app/notifications.py::notify_failure(target, run)` fires from inside `run_backup()`'s failure branch, for every trigger source (a failure is worth knowing about regardless of how the job started). `notify_window_summary(...)` fires once per window execution, once the worker pool above has fully drained, with counts of successes, failures, and targets skipped because the window closed before they were claimed, plus the failed/skipped targets' names. This reads "a summary after each backup window/run completes" as one summary per window batch, not one per individual run (a success ping for every routine per-database scheduled backup would be noisy and wasn't asked for; only window batches get a summary, individual schedule/manual runs only ever notify on failure). Uses `requests` (already a transitive dependency via `docker`, no new package needed) to `POST` to `{NTFY_URL}/{NTFY_TOPIC}`.
- **SQLite state db needs WAL mode now**: Phases 1-2 only ever had one writer at a time (one request thread). Phase 3 introduces genuinely concurrent writers (multiple window jobs, plus request threads, hitting `backup_runs`/`targets` at once), so `db.get_connection()` now also sets `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000`, reducing "database is locked" failures under real concurrency instead of discovering them the hard way during window testing. This is a small, necessary addition this phase specifically creates the need for, not scope creep.

## Project layout changes

```
app/
  jobs.py                  # NEW: run_backup(), per-target lock/set
  scheduler.py               # NEW: BackgroundScheduler setup/shutdown, sync_target_schedule(), window_tick() (queue + bounded worker pool), startup registration
  notifications.py            # NEW: notify_failure(), notify_window_summary(), ntfy HTTP calls via requests
  config.py                     # + NTFY_URL, NTFY_TOPIC
  db.py                          # + schedule_cron/in_window columns + migration, + settings table + get/set helpers, + triggered_by column, WAL/busy_timeout pragmas
  main.py                         # start scheduler + register existing schedules/window job on startup, shutdown on exit
  routes/
    targets.py                    # POST /targets/{id}/run becomes dispatch-only; POST /targets/{id}/schedule (cron or window membership)
    history.py                     # GET /targets/{id}/history (new, polling endpoint + shared partial-render helper)
    settings.py                     # NEW: GET/POST /settings, window start/duration/concurrency + read-only list of window member targets
  templates/
    targets/detail.html              # schedule form (window membership pre-selected default, cron override with inline warning), history include now polls
    partials/history_row.html          # hx-trigger only while a row is queued/running; status-queued/status-skipped CSS
    settings.html                       # NEW
schema.sql                                 # targets: + schedule_cron, in_window; backup_runs: + triggered_by; + settings table
requirements.txt                             # + apscheduler
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
    schedule_cron TEXT,
    in_window INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_runs (
    id INTEGER PRIMARY KEY,
    target_id INTEGER NOT NULL REFERENCES targets(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',  -- + 'queued', 'skipped' this phase
    file_path TEXT,
    file_size_bytes INTEGER,
    error_message TEXT,
    method TEXT,
    triggered_by TEXT  -- 'manual' | 'schedule' | 'window'
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

`app/db.py::init_db()` adds three more guarded `ALTER TABLE ADD COLUMN` steps (`targets.schedule_cron`, `targets.in_window`, `backup_runs.triggered_by`) to the same `_ensure_column()` pattern Phase 2 established, so an existing Phase 2 state db migrates in place. `settings` is a fresh table, no migration needed beyond the `CREATE TABLE IF NOT EXISTS` already in `schema.sql`. New helpers: `get_setting(conn, key, default)`, `set_setting(conn, key, value)`.

## Execution model summary

```
manual click ────────────────────────────────────────────┐
per-target cron fires ─────────────────────────────────────┼──> run_backup(target_id, triggered_by, run_id=None) ──┐
                                                             │                                                       │
window_tick() at window start:                              │                                                       ├──> ADAPTERS[engine].backup(...) ──> finish_backup_run(...)
  1. create a "queued" row per in_window=1 target             │                                                       │
  2. push (target_id, run_id) pairs onto a queue.Queue        │                                                       │
  3. start `concurrency` worker threads, each looping:        │                                                       │
       deadline passed? stop.                                 │                                                       │
       queue empty? stop.                                     │                                                       │
       pop (target_id, run_id) ────────────────────────────────┴──> run_backup(target_id, "window", run_id=run_id) ───┘
  4. join all workers (queue drained or deadline passed, nothing left running)
  5. sweep any rows still "queued" -> "skipped" ("window closed before this could run")
  6. notify_window_summary(successes, failures, skipped)

Inside run_backup(), regardless of caller:
  already in progress (any source)? -> "skipped" (schedule: new row; window: update the pre-created "queued" row; manual: no row, inline notice)
  otherwise -> row is/becomes "running", adapter runs, row finishes "success"/"failure"
  (manual and schedule both bypass the window entirely, no queueing, no worker pool involved)
```

## Routes

- `POST /targets/{id}/run` - changed: dispatches via `run_backup(target_id, "manual")` on APScheduler, returns immediately with the current history partial (either the new `running` row, or an inline "already running" notice on collision).
- `GET /targets/{id}/history` - new: renders just `partials/history_row.html` for a target, used by the detail page's initial include and by HTMX polling; this is the one place that decides whether the polling attributes are included in the response.
- `GET /targets/{id}` - unchanged route, template now includes the schedule form and the polling-capable history block.
- `POST /targets/{id}/schedule` - new: window membership is the default choice; sets `schedule_cron` (validated via `CronTrigger.from_crontab`) only when the operator explicitly overrides to a per-target cron, clearing whichever of the two wasn't chosen; calls `sync_target_schedule()` immediately.
- `GET /settings` / `POST /settings` - new: window start time, duration, concurrency cap; re-registers the window's APScheduler job on save; lists current window member targets read-only for visibility.

## Verification plan

1. Set a per-target cron schedule (e.g. every minute, for testing) via the new schedule form, confirm the form defaulted to window membership beforehand and shows the "bypasses the shared window" warning once cron is selected, confirm the schedule fires without restarting the container, and the resulting history row shows `triggered_by = "schedule"`.
2. Add 4-5 targets to the shared window with concurrency cap 2 and a duration long enough for all of them to finish, trigger the window (or wait for it), confirm every member gets a `queued` row the moment the window starts, confirm at most 2 run at once throughout (e.g. via slow test targets or docker activity, not just a momentary snapshot), and confirm targets are picked up continuously as workers free up rather than only in one batch at the start.
3. Click "run backup now" for a target whose scheduled/window run is already in progress, confirm no duplicate row and no double execution: for a schedule collision, a new `skipped` row; for a window collision, the target's pre-created `queued` row is updated to `skipped` instead of a second row appearing; for a manual-vs-manual collision, an inline notice with no new row.
4. Watch a target's detail page through a queued -> running -> success/failure transition without manually reloading, confirm polling then stops (no further `GET /targets/{id}/history` requests once nothing is active).
5. Configure `NTFY_URL`/`NTFY_TOPIC` (distinct from the legacy `/cron` topic), force a failure, confirm exactly one failure notification arrives on the configured topic.
6. Trigger a window run with a mix of succeeding and failing targets, confirm exactly one summary notification after the worker pool fully drains, correctly listing which targets failed. Confirm a routine successful per-database *schedule* run does **not** also send a summary ntfy, only window batches do.
7. Configure a window with more member targets than can realistically finish inside a short configured duration (e.g. slow test targets, a tight duration), confirm the workers stop pulling new work once the deadline passes, any in-progress run at that moment finishes naturally rather than being killed, and every target that never got picked up ends up `skipped` with an error message noting the window closed, not silently absent from history. Confirm the window summary notification's skipped count matches.
8. Restart the Savepoint container, confirm schedules, window membership, and window settings all persist and re-register correctly (no manual reconfiguration needed).
9. Restart against an existing Phase 2 state db, confirm the new columns and `settings` table appear via the guarded migration, with existing targets/history untouched.
10. Unit tests (mocked, no live Docker): `run_backup()`'s per-target lock behavior (simulated concurrent calls, second is skipped correctly per trigger source, window collisions update the existing row rather than creating a new one), the worker pool actually capping concurrency (adapter mocked to block briefly, queue N > cap targets, assert peak concurrency never exceeds `concurrency` and that all N eventually get processed), the duration cutoff correctly leaving unclaimed targets `skipped` when the queue can't fully drain in time, `sync_target_schedule()` adding/updating/removing APScheduler jobs correctly, `notifications.py` (mocked `requests.post`, correct URL/topic/message on failure and on window summary including the skipped count).

## Real-world verification results

The verification plan above was run against real Postgres, MySQL, MariaDB, and SQLite containers plus a real scheduled window on a throwaway test LXC, not just the mocked route-level and unit-test passes recorded in `phase-3-build-summary.md`:

- Per-target cron and the shared window both fire unattended on correct local time, after fixing a timezone gap: the container had no `tzdata` installed, so it defaulted to UTC despite `TZ` being set, and schedules entered in Europe/Prague wall-clock time were firing 2 hours off. Confirmed, fixed (`tzdata` installed in the Dockerfile, `TZ=Europe/Prague` set in `docker-compose.yml`, no code change needed since APScheduler already takes the system local timezone unless told otherwise).
- Every window member gets a `queued` row at window start, the worker pool respects the concurrency cap, and targets are picked up continuously as workers free up rather than in one batch: confirmed.
- The deadline cutoff correctly leaves unclaimed targets `skipped` with a clear reason: confirmed, verified by artificially slowing one Postgres target (a large table plus CPU throttling via `docker update --cpus`) to force a real window overrun with concurrency 1.
- An in-progress run past the deadline finishes naturally rather than being killed: confirmed, observed directly, a roughly 2 minute Postgres dump completed inside a 1 minute window while the rest of that window's targets were correctly marked `skipped`.
- Bonus finding, not originally in the verification plan: an accidental overlapping window reschedule triggered APScheduler's own `max_instances=1` guard ("maximum number of running instances reached"), confirming there's already built-in protection against a second `window_tick` starting while one is still in progress. Worth keeping in mind as documented, relied-upon behavior rather than something Savepoint's own code needs to separately guard against.
- ntfy failure notifications fire correctly for manual runs, not just schedule/window, confirming the shared `_execute()` path notifies regardless of trigger source as designed. Two real-world gaps were found and fixed along the way: ntfy calls need an `Authorization: Bearer` token against this homelab's ntfy instance (`NTFY_TOKEN` added), and the LAN IP doesn't accept plain HTTP on port 80, ntfy sits behind a reverse proxy the same as the legacy `db-backup-scripts` already assume, so `NTFY_URL` needs to be the operator's real ntfy domain (HTTPS, behind the proxy), not the container's raw IP. Confirmed, fixed. A related gap was also caught and fixed during this closeout: `_send()` wasn't calling `response.raise_for_status()`, so a non-2xx response (such as the 401 seen before `NTFY_TOKEN` was wired up) was silently treated as a delivered notification instead of being caught and logged.
- ntfy window summary notifications fire once per window with correct success/failure/skipped counts: confirmed.

## Status

Plan approved 2026-07-27, revised to use a bounded worker-pool/queue for the window instead of a semaphore-per-tick, cleared to build. Fully verified against real Docker containers and a real scheduled window, and closed 2026-07-27.
