# Savepoint - Phase 7 Build Summary

Built against [phase-7-plan.md](phase-7-plan.md) (approved as amended: explicit presence-only-checking caveat for forward-auth, stated in the design decision and in `.env.sample`; `.row-failure` scoped explicitly to `status == "failure"`, not `"skipped"`; Tailscale/LAN-only reminder comment added to `docker-compose.agent.yml`).

## What was built

### Forward-auth

- **`app/forward_auth.py`** (new): `ForwardAuthMiddleware`, a small Starlette `BaseHTTPMiddleware` requiring a configured header to be present and non-empty on every request, 401 otherwise. Presence-only, no value validation, matching "trusts the identity headers passed by the proxy." The caveat about this depending entirely on network isolation is stated in the module docstring, in the plan, and in `.env.sample` right next to the env var.
- **`config.py`** gained `forward_auth_header: str | None` (`FORWARD_AUTH_HEADER`), unset by default.
- **`main.py`** only calls `app.add_middleware(ForwardAuthMiddleware, ...)` when `mode == "master"` and the header name is configured. Agent mode never mounts it.
- **`base.html`**'s topbar shows `signed in as: <value>` when the header is present, reading `request.headers` directly (already available via `Jinja2Templates`), auto-escaped by Jinja's default (verified by test, see below), no `|safe` anywhere.

### Dashboard summary + clearer failure states

- **`routes/targets.py::index()`** now also fetches `db.list_agents(conn)` and `scheduler.next_window_fire_time(conn)`, passed to `index.html` alongside the existing `targets` list.
- **`index.html`** gained a summary strip: target/agent counts, next backup window time, a "Currently failing" list (only rendered when non-empty) linking to each failing target, and an "N agent(s) unreachable" line (only rendered when any agent's `last_contact_status == "error"`).
- **`base.html`** gained two small CSS rules: `td.error` gets `max-width` + `word-break: break-word` so long error strings wrap instead of stretching the table, and `tr.row-failure` gets a subtle full-row red tint. `history_row.html` and `restore_history.html` apply `class="row-failure"` **only when `status == "failure"`**, explicitly excluded for `"skipped"` (a scheduling outcome, not something wrong, Phase 3.5's pill colors already draw this distinction, the row tint had to preserve it, not collapse it).

### "Next run" and detail-page polish

- **`scheduler.py`** gained `next_fire_time(trigger, after=None)` (a thin wrapper around APScheduler's own `trigger.get_next_fire_time()`) and `next_window_fire_time(conn)` (builds the exact same `CronTrigger(hour=h, minute=m)` `_register_window_job()` already builds, including its invalid-setting fallback to `DEFAULT_WINDOW_START`).
- **`routes/targets.py::_detail_context()`** gained `_compute_next_run(conn, target)`: returns a real next-fire time only when the schedule would actually fire (`enabled` and `not agent_offsite`, exactly what `sync_target_schedule()`/`window_tick()` gate on), `None` otherwise.
- **`targets/detail.html`**'s "Currently:" line now shows `(next run: ...)` when a real time is available, or `(not active: target disabled)` / `(not active: agent flagged offsite)` when it isn't, computed once via a `{% set inactive_reason %}` and reused for both the cron and window-member branches.
- **`app/deps.py`**'s `_local_time` filter now accepts either a stored ISO string (unchanged, every existing call site) or a real `datetime` (new, what `next_fire_time()` returns), so `next_run`/`next_window` can be piped straight through `| local_time` like every other timestamp in the app without an extra string-conversion step at each call site.

### Compose/deployment finalization

- **`docker-compose.agent.yml`** (new): the reference `MODE=agent` deployment file the architecture doc already called for but Phase 6 never actually created. Opens with a comment about Tailscale/LAN-only exposure (agent mode is a more sensitive target than master's UI, Docker socket plus arbitrary backup/restore execution, the token gate shouldn't be the only line of defense). `MODE: agent`, `AGENT_TOKEN: ${AGENT_TOKEN}`, `TZ`, docker socket mounted read-write, published port, same resource-limit shape as master's compose file. No `BACKUP_TARGET_DIR`/`STATE_DB_PATH` volumes, agent mode never touches either.
- **`docker-compose.yml`** gained `FORWARD_AUTH_HEADER: ${FORWARD_AUTH_HEADER:-}`, matching the existing `NTFY_*` passthrough pattern.
- **`.env.sample`** gained `AGENT_TOKEN` and `FORWARD_AUTH_HEADER`, the latter with the full presence-only-checking caveat inline as a comment, visible at the point someone configures it.
- Master's existing resource limits were already compliant with CLAUDE.md's convention, left untouched.

### Legacy migration doc

- **`Docs/05-Legacy-Migration.md`** (new): a runbook covering what `db-backup-scripts` currently covers vs. doesn't, how to bring each database into Savepoint, a verification checklist before cutover (run in parallel for a week, and critically, actually exercise the restore workflow at least once rather than trusting a file exists), cutover steps, and what stays explicitly out of scope (Redis, anything not yet added to either system).

## Deviations from the plan, and why

None. All three amendments (forward-auth caveat, `.row-failure` scoping, compose file port comment) were built exactly as specified.

## Testing performed

- `pytest tests/` - 207/207 pass (185 from Phases 1-6, 22 new: `ForwardAuthMiddleware` header present/absent/empty/configurable-name; `scheduler.next_fire_time()`/`next_window_fire_time()` including the invalid-setting fallback; the dashboard's quiet-when-healthy state, failing-target list with link, unreachable-agent count, next-window display; the topbar identity display's auto-escaping with a literal `<script>` payload; `row-failure` present for `failure`, absent for both `skipped` and `success`; the detail page's next-run-or-inactive-reason branching across active-cron, disabled, agent-offsite, active-window-member, and manual-only cases).
- **Full app-boot sanity check** via a real `app.main` import (not just isolated route-router test clients): confirmed a request with no forward-auth header gets 401 on every route including `/`; confirmed a request with the header present succeeds and the topbar shows it; confirmed the dashboard stays quiet on a healthy system and correctly surfaces a forced failure (with a working link) and a forced-unreachable agent; confirmed the detail page shows a real next-run time for an active cron schedule and switches to "not active: target disabled" immediately after disabling it, with no fabricated time; confirmed agent mode's `/api/health` doesn't exist on master.
- Confirmed agent mode is completely unaffected by `FORWARD_AUTH_HEADER` being set: its own `AGENT_TOKEN` bearer-auth still works alone, and it still serves zero UI routes.
- Validated both `docker-compose.yml` and the new `docker-compose.agent.yml` with `docker compose config` (real Docker Compose available in this environment), both parse correctly, resource limits and volumes render as expected.

## Not tested here (needs the real homelab Docker host / a real forward-auth proxy)

1. A real Authentik (or Traefik forward-auth) deployment in front of master, confirming the header it actually sets matches what's configured in `FORWARD_AUTH_HEADER`, and that direct access to Savepoint's port (bypassing the proxy) is genuinely blocked at the network level, not just by this middleware.
2. Booting `docker-compose.agent.yml` for real on a second host, confirming it behaves identically to the hand-written compose file Phase 6 was deployed and verified with.
3. Visual review of the dashboard summary strip, the row tinting, and the wrapped error cells in an actual browser, this build only confirms the correct HTML/CSS is present, not how it actually reads at a glance.
4. Working through `Docs/05-Legacy-Migration.md` against a real database still covered by `db-backup-scripts`, to confirm the runbook's steps are accurate and nothing is missing once tried for real.

## Status

Built and tested 2026-07-29. Awaiting real-world verification on the homelab Docker host before closeout.
