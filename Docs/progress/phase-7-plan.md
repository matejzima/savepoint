# Savepoint - Phase 7 Plan: Polish

## Context

Phases 1-6 built the full functional core: discovery, scheduling, GFS retention, restore, and the remote agent. `04-Initial-Build-Plan.md`'s Phase 7 is explicitly scoped as "Polish", the last phase, closing out four loose ends rather than adding new capability: forward-auth support, a UI pass (dashboard overview, per-database detail view, clearer failure states), finalizing the compose/resource-limit story (including the still-missing agent-mode reference compose file), and documenting the cutover plan away from the legacy `db-backup-scripts` bash setup.

Two scope decisions confirmed before drafting this plan: forward-auth gets a real enforcement mechanism (not documentation-only), and the dashboard is a summary strip added to the existing target list page (not a separate landing route).

## Design decisions - forward-auth

- **New env var `FORWARD_AUTH_HEADER`** (`config.py` gains `forward_auth_header: str | None`), unset by default, matching "off by default, no auth for local/LAN-only use" exactly. When unset, nothing changes, zero overhead, current behavior untouched.
- **New `app/forward_auth.py`**, a small Starlette middleware checking that the configured header is present and non-empty on every request; missing or empty returns 401 with a clear plain-text body. No value validation against anything, Savepoint has no user database to check against, presence-only, exactly matching the architecture doc's "trusts the identity headers passed by the proxy rather than implementing its own login." Kept in its own module (not inlined in `main.py`) so it's unit-testable in isolation with a throwaway FastAPI app, the same pattern already used for other small self-contained concerns in this codebase.
- **`main.py`** only adds the middleware when `settings.mode == "master"` and `settings.forward_auth_header` is set. Agent mode never mounts it, forward-auth protects the human-facing UI, agent mode has no UI and already has its own separate `AGENT_TOKEN` bearer-auth for its API, unrelated concern, no interaction between the two.
- **`base.html`** shows the header's value in the topbar when present (e.g. "signed in as: `<value>`"), purely informational, reads directly from `request.headers` (already available in every template via Starlette's `Jinja2Templates` convention), no new context plumbing needed. Absent when forward-auth isn't enabled.
- Applies uniformly to every master-mode route, no exemptions. Nothing in this app needs unauthenticated access (no health-check endpoint on master mode today), and adding exemption logic for a hypothetical would be scope creep the "keep it simple" convention argues against.
- **Explicit caveat: presence-only header checking has no signature or cryptographic binding to the proxy's own session.** Savepoint's protection depends entirely on the network guaranteeing that only the forward-auth proxy (e.g. Authentik) can reach its port, not anyone else directly. If direct access to Savepoint's port is ever possible, from the LAN, a misconfigured firewall rule, whatever, anyone can set the configured header themselves on a raw request and authenticate as anyone, there is nothing checking that the header was actually set by the proxy rather than forged by the requester. This is stated plainly here, and the same one-line caveat goes in `.env.sample` right next to `FORWARD_AUTH_HEADER`, so it's visible at the point someone configures it, not just buried in a docs file.

## Design decisions - dashboard summary + clearer failure states

- **`routes/targets.py::index()`** additionally fetches `db.list_agents(conn)` (master mode always, agent mode never reaches this route since it mounts no UI at all) and passes it to `index.html` alongside the existing `targets` list. No new query needed for the failing-target list itself, `list_targets()` already returns `latest_run_status` per target, filtering happens in the template (`targets | selectattr('latest_run_status', 'equalto', 'failure')`).
- **`index.html`** gains a summary strip above the existing table: total target count, a prominent "currently failing" list (name + link, using the filter above) shown only when non-empty so a healthy system shows a quiet page, an agent-reachability count (`agents | selectattr('last_contact_status', 'equalto', 'error') | list`, only rendered if any agents are registered at all), and "next backup window" (see the scheduling helper below). This is additive to the existing page, the table underneath is untouched.
- **Failure clarity in history tables** (`partials/history_row.html`, `partials/restore_history.html`, `agents.html`'s `last_contact_error` cell): two small CSS-only changes in `base.html`, a `.row-failure` class applied to failing `<tr>` elements for a subtle full-row tint (so a failing run is scannable down a long table without reading every pill) and a max-width + wrap rule on error-message cells so a long error string wraps onto multiple lines instead of stretching the table. Purely presentational, no route/data changes. **`.row-failure` applies only when `status == "failure"`, explicitly not `"skipped"`.** Skipped means the target didn't get a turn during a window (a scheduling outcome, nothing went wrong), not a failure, and Phase 3.5's status pills already treat the two as visually distinct for exactly that reason (`pill-skipped` vs `pill-failure`, different colors). The row-level tint has to preserve that same distinction, not collapse it.

## Design decisions - "next run" and per-database detail polish

- **New `scheduler.next_fire_time(trigger, after=None) -> datetime | None`**, a thin wrapper around APScheduler's own `CronTrigger.get_next_fire_time(previous_fire_time, now)` (APScheduler is already a dependency, no new one added). Callers build the trigger themselves: a target's own `CronTrigger.from_crontab(target['schedule_cron'])`, or the shared window's `CronTrigger(hour=h, minute=m)` built from `parse_hhmm(db.get_setting(conn, "window_start", DEFAULT_WINDOW_START))`, already exactly how `_register_window_job()` builds it today.
- **`targets/detail.html`'s "Currently:" line** becomes precise about whether a next-run time is real: only shown when the schedule would actually fire (`enabled` and `not agent_offsite`), matching exactly what `sync_target_schedule()`/`window_tick()` already gate on. A cron-scheduled or window-member target that's disabled or agent-offsite shows why it's inactive instead of a fabricated next-run time that won't actually happen, this is the same "clearer failure/inactive states" goal applied to scheduling, not just backup outcomes.
- **`routes/history.py::target_detail()`** (or `_detail_context()`) computes this next-run value once per render and passes it through, reusing the new `scheduler.next_fire_time()` helper, `local_time` filter handles display exactly like every other timestamp in the app.
- **Dashboard's "next backup window"** reuses the same helper against the window's own cron, one call, no duplication.

## Design decisions - compose/deployment finalization

- **New `docker-compose.agent.yml`** (or `docker-compose.agent.example.yml`), the reference deployment file `03-Proposed-Architecture.md`'s Deployment section already calls for but was never actually created during Phase 6: `MODE: agent`, `AGENT_TOKEN: ${AGENT_TOKEN}` (required, operator-generated, e.g. `openssl rand -hex 32`, entered again in master's `/agents` Add form, no mechanism exists for master to push this), `TZ: Europe/Prague` (kept for homelab consistency even though agent mode's headless design means it isn't functionally required, no scheduling runs on an agent since Phase 6), docker socket mounted read-write (same as master, needed for `exec`-based dumps/restores), a published port (mirroring master's own `ports: ["8000:8000"]`, reachable over Tailscale), the same `deploy.resources.limits`/`memswap_limit` shape as master's compose file (same numbers as a starting point, noted as safe to right-size down later given an agent does meaningfully less than master). No `BACKUP_TARGET_DIR`/`STATE_DB_PATH` volumes needed, agent mode never touches either. **A comment at the top of the file reminds whoever deploys it that the published port must only ever be reachable via Tailscale/LAN, never a genuinely public-facing host**, mirroring master's own compose file (not a new regression this phase introduces), but agent mode is an even more sensitive target than master's UI (Docker socket access plus arbitrary backup/restore execution), so the `AGENT_TOKEN` gate alone shouldn't be treated as the only line of defense. Documentation-only, no behavior change.
- **`.env.sample`** gains `AGENT_TOKEN=` (documented: only needed when deploying `MODE=agent`) and `FORWARD_AUTH_HEADER=` (documented: optional, off by default, the exact header name your forward-auth proxy sets, e.g. `X-Authentik-Username`).
- **`docker-compose.yml`** (master's) gains `FORWARD_AUTH_HEADER: ${FORWARD_AUTH_HEADER:-}`, matching the existing `${VAR:-}` passthrough pattern already used for the `NTFY_*` vars.
- Master's existing resource limits (1 CPU / 512M / 768M memswap) are already compliant with CLAUDE.md's convention (`deploy.resources.limits` + top-level `memswap_limit` at roughly 1.5x memory), left as-is, no evidence they need changing.

## Design decisions - legacy migration doc

- **New `Docs/05-Legacy-Migration.md`**, a runbook, not code, covering: what `db-backup-scripts` currently covers (Nextcloud, Invoicerr, Immich, per `01-High-Level-Description.md`) versus what it doesn't (Paperless-ngx, Gitea, Wallos, others); how to bring each currently-covered database into Savepoint (discover-or-manual-add, schedule/window choice, retention configuration); a verification checklist before cutover (run in parallel with the legacy cron jobs for at least one full week so both daily and the first weekly tier are exercised, and critically, actually exercise Savepoint's restore workflow at least once as a real test rather than just trusting a file exists); cutover steps (remove the relevant cron entries from `docker-host-compose`, an external repo this plan only describes procedurally, keep old backup files for a grace period before deleting); and what stays explicitly out of scope (Redis, per the requirements doc; anything not yet added to either system).

## Project layout changes

```
app/
  forward_auth.py          # NEW: header-presence middleware
  config.py                  # + forward_auth_header: str | None (FORWARD_AUTH_HEADER)
  main.py                      # mounts forward_auth middleware when master mode + header configured
  scheduler.py                   # + next_fire_time(trigger, after=None)
  routes/
    targets.py                    # index(): + agents list in context
    history.py                      # target_detail()/_detail_context(): + computed next-run value
  templates/
    base.html                        # topbar identity display when forward-auth enabled;
                                      # .row-failure / error-cell wrap CSS
    index.html                         # summary strip: totals, failing list, agent-unreachable count,
                                        # next window time
    targets/detail.html                  # "Currently:" line shows next-run time only when it would
                                          # actually fire, explains why otherwise
    partials/history_row.html              # .row-failure class on failing rows
    partials/restore_history.html            # same
    agents.html                                # error-cell wrap CSS (shared rule, no template change)
docker-compose.agent.yml                         # NEW: reference agent deployment file
docker-compose.yml                                 # + FORWARD_AUTH_HEADER passthrough
.env.sample                                          # + AGENT_TOKEN, FORWARD_AUTH_HEADER (documented,
                                                      # including the presence-only-checking caveat)
Docs/
  05-Legacy-Migration.md                                # NEW: cutover runbook
```

No schema/data model changes this phase.

## Verification plan

1. Boot master with `FORWARD_AUTH_HEADER` unset, confirm every existing route works exactly as before, no header required anywhere (regression check on the default/current behavior).
2. Set `FORWARD_AUTH_HEADER=X-Authentik-Username`, confirm a request with that header present succeeds, confirm a request missing it (or with an empty value) gets 401, confirm the topbar shows the header's value when present.
3. Confirm agent mode is completely unaffected by `FORWARD_AUTH_HEADER` being set (the middleware never mounts there).
4. Send a request with the configured header's value set to something containing HTML/script-like content (e.g. `<script>alert(1)</script>`), confirm the topbar renders it as literal escaped text, not executed or unescaped markup, confirming Jinja's default auto-escaping is genuinely in effect and nothing renders that value with `|safe`.
5. With a healthy system (no failing targets, all agents reachable), confirm the dashboard summary strip stays quiet (no failing-target section, no agent-warning section).
6. Force a target into a `failure` state and, separately, a `skipped` state, confirm the `failure` row gets the red tint and the `skipped` row does not, the two remain visually distinguishable from each other, not just both distinguishable from `success`. Confirm the failing target also appears in the dashboard's failing list with a working link, and that a long error message wraps instead of overflowing.
7. Force an agent's `last_contact_status` to `error` (e.g. a bad token), confirm the dashboard's agent-unreachable count reflects it.
8. On a target's detail page: cron-scheduled + enabled + not offsite shows a real next-run time; the same target disabled, or agent-offsite, shows an explanation instead, not a fabricated time. Same for window membership.
9. Confirm the dashboard's "next backup window" time matches what `window_tick`'s own registered job would actually fire at (cross-check against `db.get_setting(conn, "window_start", ...)`).
10. Boot a real (or LXC-based throwaway) `MODE=agent` container from the new `docker-compose.agent.yml`, confirm it starts, confirm master can register and use it exactly as it could deploying from a hand-written compose file, confirm resource limits are present and reasonable, confirm the new top-of-file comment about Tailscale/LAN-only exposure is present and clearly worded.
11. Unit tests: `forward_auth.py`'s middleware (header present/absent/empty, master-only mounting, auto-escaping of the header value in the rendered topbar); `scheduler.next_fire_time()` against known cron expressions and a fixed "now" (including a window-derived trigger); the index route's new context (failing list, agent-unreachable count) with fixture data; the detail page's next-run-or-explanation branching for the enabled/disabled/offsite/no-schedule combinations; `.row-failure` applied for `status == "failure"` and confirmed absent for `status == "skipped"`.

## Status

Plan drafted 2026-07-29, amended 2026-07-29 (explicit presence-only-checking caveat for forward-auth, stated in the design decision and in .env.sample; .row-failure scoped explicitly to status == "failure", not "skipped"; Tailscale/LAN-only reminder comment added to docker-compose.agent.yml), approved and built 2026-07-29. See [phase-7-build-summary.md](phase-7-build-summary.md). Awaiting real-world verification on the homelab Docker host before closeout.
