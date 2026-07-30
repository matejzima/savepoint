import os

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from apscheduler.triggers.cron import CronTrigger

from .. import agent_client
from .. import db as db_module
from .. import docker_client
from .. import jobs
from .. import retention
from .. import scheduler
from .. import validation
from ..deps import get_db_conn, templates

router = APIRouter()

ENGINES = ("postgres", "mysql", "mariadb", "sqlite")


def _add_form_error(request: Request, error: str, form: dict, conn):
    return templates.TemplateResponse(
        request,
        "targets/add.html",
        {"error": error, "form": form, "agents": db_module.list_agents(conn)},
        status_code=400,
    )


def _validate_connection(settings, agent_id, engine, container_name, db_user, db_name, file_path):
    """Validates against the local Docker socket, or against a specific agent's
    /api/validate if agent_id is set, so create/edit behave identically either way.
    Returns an error message, or None if valid.
    """
    if agent_id:
        conn = db_module.get_connection(settings.state_db_path)
        try:
            agent = db_module.get_agent(conn, agent_id)
        finally:
            conn.close()
        if agent is None:
            return f"agent #{agent_id} not found"
        return agent_client.validate(agent, settings, engine, container_name, db_user, db_name, file_path)

    client = docker_client.get_client()
    return validation.validate_connection_fields(client, engine, container_name, db_user, db_name, file_path)


def _compute_next_run(conn, target):
    """Only returns a real time when the schedule would actually fire, matching exactly
    what sync_target_schedule()/window_tick() gate on (enabled, not agent-offsite), a
    fabricated next-run time for a schedule that won't actually fire would be misleading.
    The template itself decides what to show when this is None (disabled, agent-offsite,
    or genuinely unscheduled), it already has target['enabled']/['agent_offsite'] to work
    out which.
    """
    if not target["enabled"] or target["agent_offsite"]:
        return None
    if target["schedule_cron"]:
        return scheduler.next_fire_time(CronTrigger.from_crontab(target["schedule_cron"]))
    if target["in_window"]:
        return scheduler.next_window_fire_time(conn)
    return None


def _detail_context(conn, target, target_id, **overrides):
    runs = db_module.list_backup_runs(conn, target_id)
    tags_by_run = db_module.get_tags_for_runs(conn, [r["id"] for r in runs])
    context = {
        "target": target,
        "runs": runs,
        "tags_by_run": tags_by_run,
        "schedule_error": None,
        "retention_error": None,
        "connection_error": None,
        "delete_error": None,
        "file_count": db_module.count_target_files(conn, target_id),
        "eligible_backups": db_module.list_eligible_backups_for_restore(conn, target_id),
        "restore_runs": db_module.list_restore_runs(conn, target_id),
        "next_run": _compute_next_run(conn, target),
    }
    context.update(overrides)
    return context


@router.get("/", response_class=HTMLResponse)
def index(request: Request, conn=Depends(get_db_conn)):
    targets = db_module.list_targets(conn)
    agents = db_module.list_agents(conn)
    next_window = scheduler.next_window_fire_time(conn)
    return templates.TemplateResponse(
        request, "index.html", {"targets": targets, "agents": agents, "next_window": next_window}
    )


@router.get("/targets/add", response_class=HTMLResponse)
def add_target_form(
    request: Request,
    engine: str = "postgres",
    container_name: str = "",
    db_user: str = "",
    db_name: str = "",
    file_path: str = "",
    agent_id: str = "",
    conn=Depends(get_db_conn),
):
    form = {
        "name": "",
        "engine": engine,
        "container_name": container_name,
        "db_user": db_user,
        "db_name": db_name,
        "file_path": file_path,
        "agent_id": agent_id,
    }
    return templates.TemplateResponse(
        request, "targets/add.html", {"error": None, "form": form, "agents": db_module.list_agents(conn)}
    )


@router.post("/targets", response_class=HTMLResponse)
def create_target(
    request: Request,
    name: str = Form(...),
    engine: str = Form("postgres"),
    container_name: str = Form(...),
    db_user: str = Form(""),
    db_name: str = Form(""),
    file_path: str = Form(""),
    agent_id: str = Form(""),
    conn=Depends(get_db_conn),
):
    form = {
        "name": name,
        "engine": engine,
        "container_name": container_name,
        "db_user": db_user,
        "db_name": db_name,
        "file_path": file_path,
        "agent_id": agent_id,
    }
    resolved_agent_id = int(agent_id) if agent_id else None

    if engine not in ENGINES:
        return _add_form_error(request, f"unknown engine '{engine}'", form, conn)

    error = _validate_connection(
        request.app.state.settings, resolved_agent_id, engine, container_name, db_user, db_name, file_path
    )
    if error:
        return _add_form_error(request, error, form, conn)

    db_module.create_target(
        conn,
        name,
        engine,
        container_name,
        db_user if engine != "sqlite" else "",
        db_name if engine != "sqlite" else "",
        file_path if engine == "sqlite" else None,
        agent_id=resolved_agent_id,
    )
    return RedirectResponse(url="/", status_code=303)


@router.post("/targets/{target_id}/run", response_class=HTMLResponse)
def run_backup_route(request: Request, target_id: int, conn=Depends(get_db_conn)):
    target = db_module.get_target(conn, target_id)
    if target is None:
        return HTMLResponse("target not found", status_code=404)

    notice = None
    if jobs.try_claim(target_id):
        run_id = db_module.create_backup_run(conn, target_id, status="running", triggered_by="manual")
        scheduler.dispatch_manual(target_id, run_id)
    else:
        notice = "a backup for this target is already running"

    runs = db_module.list_backup_runs(conn, target_id)
    tags_by_run = db_module.get_tags_for_runs(conn, [r["id"] for r in runs])
    return templates.TemplateResponse(
        request,
        "partials/history_row.html",
        {"target": target, "runs": runs, "notice": notice, "tags_by_run": tags_by_run},
    )


@router.post("/targets/{target_id}/restore", response_class=HTMLResponse)
def restore_target_route(
    request: Request,
    target_id: int,
    backup_run_id: int = Form(...),
    confirm_name: str = Form(""),
    stop_container: bool = Form(False),
    conn=Depends(get_db_conn),
):
    """Dispatch-not-inline, mirroring manual backup: claim, create the row, hand off to
    the scheduler, return immediately. Validation failures (name mismatch, ineligible
    backup, target busy) are reported as a `notice` inside the restore-history partial
    rather than a full-page error, the same idiom run_backup_route already uses for its
    own collision case.
    """
    target = db_module.get_target(conn, target_id)
    if target is None:
        return HTMLResponse("target not found", status_code=404)

    notice = None
    if confirm_name != target["name"]:
        notice = f"typed name '{confirm_name}' does not match '{target['name']}'"
    else:
        eligible_ids = {r["id"] for r in db_module.list_eligible_backups_for_restore(conn, target_id)}
        if backup_run_id not in eligible_ids:
            notice = "selected backup is not eligible for restore"
        elif not jobs.try_claim(target_id):
            notice = "a backup or restore for this target is already in progress"
        else:
            restore_run_id = db_module.create_restore_run(conn, target_id, backup_run_id, status="running")
            # "Stop container" is a SQLite-only option, ignored for server engines where
            # stopping the server would make the restore mechanism itself impossible.
            effective_stop = bool(stop_container) and target["engine"] == "sqlite"
            scheduler.dispatch_restore(target_id, restore_run_id, backup_run_id, effective_stop)

    restore_runs = db_module.list_restore_runs(conn, target_id)
    return templates.TemplateResponse(
        request,
        "partials/restore_history.html",
        {"target": target, "restore_runs": restore_runs, "notice": notice},
    )


@router.post("/targets/{target_id}/edit", response_class=HTMLResponse)
def update_connection(
    request: Request,
    target_id: int,
    container_name: str = Form(...),
    db_user: str = Form(""),
    db_name: str = Form(""),
    file_path: str = Form(""),
    conn=Depends(get_db_conn),
):
    target = db_module.get_target(conn, target_id)
    if target is None:
        return HTMLResponse("target not found", status_code=404)

    engine = target["engine"]
    # agent_id is fixed at creation, exactly like engine, editing connection details
    # never moves a target between hosts, it only corrects details on the same host.
    agent_id = target["agent_id"]
    error = _validate_connection(
        request.app.state.settings, agent_id, engine, container_name, db_user, db_name, file_path
    )
    if error:
        return templates.TemplateResponse(
            request,
            "targets/detail.html",
            _detail_context(conn, target, target_id, connection_error=error),
            status_code=400,
        )

    db_module.update_target_connection(
        conn,
        target_id,
        container_name,
        db_user if engine != "sqlite" else "",
        db_name if engine != "sqlite" else "",
        file_path if engine == "sqlite" else None,
        agent_id=agent_id,
    )
    return RedirectResponse(url=f"/targets/{target_id}", status_code=303)


@router.post("/targets/{target_id}/toggle-enabled", response_class=HTMLResponse)
def toggle_enabled(request: Request, target_id: int, conn=Depends(get_db_conn)):
    target = db_module.get_target(conn, target_id)
    if target is None:
        return HTMLResponse("target not found", status_code=404)

    db_module.update_target_enabled(conn, target_id, not target["enabled"])
    updated_target = db_module.get_target(conn, target_id)
    scheduler.sync_target_schedule(updated_target)
    return RedirectResponse(url=f"/targets/{target_id}", status_code=303)


@router.post("/targets/{target_id}/delete", response_class=HTMLResponse)
def delete_target_route(
    request: Request,
    target_id: int,
    confirm_name: str = Form(""),
    delete_files: bool = Form(False),
    conn=Depends(get_db_conn),
):
    target = db_module.get_target(conn, target_id)
    if target is None:
        return HTMLResponse("target not found", status_code=404)

    if jobs.is_in_progress(target_id):
        return templates.TemplateResponse(
            request,
            "targets/detail.html",
            _detail_context(
                conn,
                target,
                target_id,
                delete_error="a backup for this target is currently running, try again once it finishes",
            ),
            status_code=400,
        )

    if confirm_name != target["name"]:
        return templates.TemplateResponse(
            request,
            "targets/detail.html",
            _detail_context(
                conn,
                target,
                target_id,
                delete_error=f"typed name '{confirm_name}' does not match '{target['name']}'",
            ),
            status_code=400,
        )

    if delete_files:
        for path in db_module.list_target_file_paths(conn, target_id):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    scheduler.remove_target_job(target_id)
    db_module.delete_target(conn, target_id)
    return RedirectResponse(url="/", status_code=303)


@router.post("/targets/{target_id}/schedule", response_class=HTMLResponse)
def update_schedule(
    request: Request,
    target_id: int,
    mode: str = Form(...),
    schedule_cron: str = Form(""),
    conn=Depends(get_db_conn),
):
    target = db_module.get_target(conn, target_id)
    if target is None:
        return HTMLResponse("target not found", status_code=404)

    # Defensive fallback: a non-empty cron value always means cron mode, regardless of
    # what the mode radio says, so a desynced form (JS not run, browser quirk, future UI
    # change) never silently discards a typed cron expression.
    if mode == "window" and schedule_cron.strip():
        mode = "cron"

    if mode == "cron":
        try:
            CronTrigger.from_crontab(schedule_cron)
        except ValueError:
            return templates.TemplateResponse(
                request,
                "targets/detail.html",
                _detail_context(
                    conn,
                    target,
                    target_id,
                    schedule_error=f"invalid cron expression: '{schedule_cron}'",
                ),
                status_code=400,
            )
        db_module.update_target_schedule(conn, target_id, schedule_cron, in_window=False)
    else:
        db_module.update_target_schedule(conn, target_id, None, in_window=True)

    updated_target = db_module.get_target(conn, target_id)
    scheduler.sync_target_schedule(updated_target)
    return RedirectResponse(url=f"/targets/{target_id}", status_code=303)


@router.post("/targets/{target_id}/retention", response_class=HTMLResponse)
def update_retention(
    request: Request,
    target_id: int,
    retention_daily: str = Form(...),
    retention_weekly: str = Form(...),
    retention_monthly: str = Form(...),
    conn=Depends(get_db_conn),
):
    target = db_module.get_target(conn, target_id)
    if target is None:
        return HTMLResponse("target not found", status_code=404)

    fields = {
        "daily": retention_daily,
        "weekly": retention_weekly,
        "monthly": retention_monthly,
    }
    for label, raw in fields.items():
        if not raw.isdigit() or int(raw) <= 0:
            return templates.TemplateResponse(
                request,
                "targets/detail.html",
                _detail_context(
                    conn,
                    target,
                    target_id,
                    retention_error=f"{label} count must be a positive integer",
                ),
                status_code=400,
            )

    db_module.update_target_retention(
        conn, target_id, int(retention_daily), int(retention_weekly), int(retention_monthly)
    )
    # Confirming retention should apply to whatever history already exists right away,
    # not wait for the next backup or a restart, this is what actually enforces the
    # counts against the backfilled-but-previously-unconfirmed tags for this target.
    updated_target = db_module.get_target(conn, target_id)
    retention.prune_target(conn, updated_target, request.app.state.settings.backup_target_dir)
    return RedirectResponse(url=f"/targets/{target_id}", status_code=303)
