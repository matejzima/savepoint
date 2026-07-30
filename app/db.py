from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def get_connection(state_db_path: str) -> sqlite3.Connection:
    dirname = os.path.dirname(state_db_path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    conn = sqlite3.connect(state_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(state_db_path: str) -> None:
    conn = get_connection(state_db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
        _ensure_column(conn, "targets", "file_path", "TEXT")
        _ensure_column(conn, "backup_runs", "method", "TEXT")
        _ensure_column(conn, "targets", "schedule_cron", "TEXT")
        _ensure_column(conn, "targets", "in_window", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "backup_runs", "triggered_by", "TEXT")
        _ensure_column(conn, "targets", "retention_daily", "INTEGER NOT NULL DEFAULT 7")
        _ensure_column(conn, "targets", "retention_weekly", "INTEGER NOT NULL DEFAULT 4")
        _ensure_column(conn, "targets", "retention_monthly", "INTEGER NOT NULL DEFAULT 2")
        _ensure_column(conn, "targets", "retention_confirmed", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "targets", "enabled", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "targets", "agent_id", "INTEGER REFERENCES agents(id)")
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_target(
    conn: sqlite3.Connection,
    name: str,
    engine: str,
    container_name: str,
    db_user: str,
    db_name: str,
    file_path: str | None = None,
    agent_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO targets (name, engine, container_name, db_user, db_name, file_path, agent_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, engine, container_name, db_user, db_name, file_path, agent_id, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def list_targets(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT t.*,
               r.status AS latest_run_status,
               r.started_at AS latest_run_started_at,
               r.finished_at AS latest_run_finished_at,
               a.name AS agent_name,
               a.offsite AS agent_offsite
        FROM targets t
        LEFT JOIN backup_runs r ON r.id = (
            SELECT id FROM backup_runs WHERE target_id = t.id ORDER BY id DESC LIMIT 1
        )
        LEFT JOIN agents a ON a.id = t.agent_id
        ORDER BY t.name
        """
    ).fetchall()


def list_all_targets(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT t.*, a.name AS agent_name, a.offsite AS agent_offsite
        FROM targets t
        LEFT JOIN agents a ON a.id = t.agent_id
        ORDER BY t.name
        """
    ).fetchall()


def get_target(conn: sqlite3.Connection, target_id: int):
    return conn.execute(
        """
        SELECT t.*, a.name AS agent_name, a.offsite AS agent_offsite
        FROM targets t
        LEFT JOIN agents a ON a.id = t.agent_id
        WHERE t.id = ?
        """,
        (target_id,),
    ).fetchone()


def update_target_schedule(
    conn: sqlite3.Connection, target_id: int, schedule_cron: str | None, in_window: bool
) -> None:
    conn.execute(
        "UPDATE targets SET schedule_cron = ?, in_window = ? WHERE id = ?",
        (schedule_cron, 1 if in_window else 0, target_id),
    )
    conn.commit()


def create_backup_run(
    conn: sqlite3.Connection,
    target_id: int,
    status: str = "running",
    triggered_by: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO backup_runs (target_id, started_at, status, triggered_by) VALUES (?, ?, ?, ?)",
        (target_id, now_iso(), status, triggered_by),
    )
    conn.commit()
    return cur.lastrowid


def update_backup_run_status(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    conn.execute("UPDATE backup_runs SET status = ? WHERE id = ?", (status, run_id))
    conn.commit()


def finish_backup_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    file_path: str | None = None,
    file_size_bytes: int | None = None,
    error_message: str | None = None,
    method: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE backup_runs
        SET status = ?, finished_at = ?, file_path = ?, file_size_bytes = ?, error_message = ?, method = ?
        WHERE id = ?
        """,
        (status, now_iso(), file_path, file_size_bytes, error_message, method, run_id),
    )
    conn.commit()


def list_backup_runs(conn: sqlite3.Connection, target_id: int):
    return conn.execute(
        "SELECT * FROM backup_runs WHERE target_id = ? ORDER BY id DESC",
        (target_id,),
    ).fetchall()


def get_backup_run(conn: sqlite3.Connection, run_id: int):
    return conn.execute("SELECT * FROM backup_runs WHERE id = ?", (run_id,)).fetchone()


def get_setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def update_target_retention(
    conn: sqlite3.Connection, target_id: int, daily: int, weekly: int, monthly: int
) -> None:
    conn.execute(
        """
        UPDATE targets
        SET retention_daily = ?, retention_weekly = ?, retention_monthly = ?, retention_confirmed = 1
        WHERE id = ?
        """,
        (daily, weekly, monthly, target_id),
    )
    conn.commit()


def add_backup_run_tag(conn: sqlite3.Connection, run_id: int, tier: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO backup_run_tags (backup_run_id, tier, tagged_at) VALUES (?, ?, ?)",
        (run_id, tier, now_iso()),
    )
    conn.commit()


def remove_backup_run_tag(conn: sqlite3.Connection, run_id: int, tier: str) -> None:
    conn.execute(
        "DELETE FROM backup_run_tags WHERE backup_run_id = ? AND tier = ?",
        (run_id, tier),
    )
    conn.commit()


def list_tagged_runs(conn: sqlite3.Connection, target_id: int, tier: str):
    """Runs for target_id currently holding `tier`, most recent (by started_at) first."""
    return conn.execute(
        """
        SELECT r.* FROM backup_runs r
        JOIN backup_run_tags bt ON bt.backup_run_id = r.id
        WHERE r.target_id = ? AND bt.tier = ?
        ORDER BY r.started_at DESC, r.id DESC
        """,
        (target_id, tier),
    ).fetchall()


def has_any_tags(conn: sqlite3.Connection, run_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM backup_run_tags WHERE backup_run_id = ? LIMIT 1", (run_id,)
    ).fetchone()
    return row is not None


def get_tags_for_runs(conn: sqlite3.Connection, run_ids: list) -> dict:
    """Batched tag lookup for a list of run ids, avoids an N+1 query when rendering history."""
    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT backup_run_id, tier FROM backup_run_tags WHERE backup_run_id IN ({placeholders})",
        tuple(run_ids),
    ).fetchall()
    result = {run_id: [] for run_id in run_ids}
    for row in rows:
        result[row["backup_run_id"]].append(row["tier"])
    return result


def list_successful_runs(conn: sqlite3.Connection, target_id: int):
    """Successful runs for target_id, chronological (oldest first)."""
    return conn.execute(
        "SELECT * FROM backup_runs WHERE target_id = ? AND status = 'success' ORDER BY started_at ASC, id ASC",
        (target_id,),
    ).fetchall()


def update_target_connection(
    conn: sqlite3.Connection,
    target_id: int,
    container_name: str,
    db_user: str,
    db_name: str,
    file_path: str | None,
    agent_id: int | None = None,
) -> None:
    conn.execute(
        "UPDATE targets SET container_name = ?, db_user = ?, db_name = ?, file_path = ?, agent_id = ? WHERE id = ?",
        (container_name, db_user, db_name, file_path, agent_id, target_id),
    )
    conn.commit()


def update_target_enabled(conn: sqlite3.Connection, target_id: int, enabled: bool) -> None:
    conn.execute("UPDATE targets SET enabled = ? WHERE id = ?", (1 if enabled else 0, target_id))
    conn.commit()


def _existing_target_files(conn: sqlite3.Connection, target_id: int) -> list:
    """This target's distinct backup file paths that are actually still present on disk.

    backup_runs.file_path is deliberately left populated even after a file has been
    pruned (Phase 4's own audit-trail decision) or removed by hand outside Savepoint, so
    a raw non-null count would overstate what's really there.
    """
    rows = conn.execute(
        "SELECT DISTINCT file_path FROM backup_runs WHERE target_id = ? AND file_path IS NOT NULL",
        (target_id,),
    ).fetchall()
    return [row["file_path"] for row in rows if os.path.exists(row["file_path"])]


def count_target_files(conn: sqlite3.Connection, target_id: int) -> int:
    return len(_existing_target_files(conn, target_id))


def list_target_file_paths(conn: sqlite3.Connection, target_id: int) -> list:
    return _existing_target_files(conn, target_id)


def delete_target(conn: sqlite3.Connection, target_id: int) -> None:
    """Cascades backup_run_tags -> restore_runs -> backup_runs -> targets. No commit()
    happens between the statements, so they land in one transaction (sqlite3's default
    deferred isolation mode already opens an implicit transaction before the first DML
    statement and holds it open until commit()), a crash partway through leaves nothing
    committed rather than an orphaned row pointing at an already-deleted parent.
    """
    conn.execute(
        "DELETE FROM backup_run_tags WHERE backup_run_id IN (SELECT id FROM backup_runs WHERE target_id = ?)",
        (target_id,),
    )
    conn.execute("DELETE FROM restore_runs WHERE target_id = ?", (target_id,))
    conn.execute("DELETE FROM backup_runs WHERE target_id = ?", (target_id,))
    conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
    conn.commit()


def create_restore_run(
    conn: sqlite3.Connection, target_id: int, backup_run_id: int, status: str = "running"
) -> int:
    cur = conn.execute(
        "INSERT INTO restore_runs (target_id, backup_run_id, started_at, status) VALUES (?, ?, ?, ?)",
        (target_id, backup_run_id, now_iso(), status),
    )
    conn.commit()
    return cur.lastrowid


def finish_restore_run(
    conn: sqlite3.Connection,
    restore_run_id: int,
    status: str,
    stopped_container: bool = False,
    error_message: str | None = None,
) -> None:
    conn.execute(
        "UPDATE restore_runs SET status = ?, finished_at = ?, stopped_container = ?, error_message = ? WHERE id = ?",
        (status, now_iso(), 1 if stopped_container else 0, error_message, restore_run_id),
    )
    conn.commit()


def list_restore_runs(conn: sqlite3.Connection, target_id: int):
    return conn.execute(
        """
        SELECT rr.*, br.started_at AS backup_started_at
        FROM restore_runs rr
        JOIN backup_runs br ON br.id = rr.backup_run_id
        WHERE rr.target_id = ?
        ORDER BY rr.id DESC
        """,
        (target_id,),
    ).fetchall()


def list_eligible_backups_for_restore(conn: sqlite3.Connection, target_id: int):
    """This target's successful runs whose file is verified present on disk right now,
    most recent first. Mirrors the os.path.exists() principle count_target_files() and
    list_target_file_paths() already established, so the restore dropdown can't offer a
    backup that's already been pruned or removed by hand.
    """
    rows = conn.execute(
        "SELECT * FROM backup_runs WHERE target_id = ? AND status = 'success' ORDER BY started_at DESC, id DESC",
        (target_id,),
    ).fetchall()
    return [r for r in rows if r["file_path"] and os.path.exists(r["file_path"])]


def create_agent(
    conn: sqlite3.Connection, name: str, base_url: str, token: str, offsite: bool = False
) -> int:
    cur = conn.execute(
        "INSERT INTO agents (name, base_url, token, offsite, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, base_url, token, 1 if offsite else 0, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def list_agents(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM agents ORDER BY name").fetchall()


def get_agent(conn: sqlite3.Connection, agent_id: int):
    return conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()


def update_agent(
    conn: sqlite3.Connection, agent_id: int, name: str, base_url: str, token: str, offsite: bool
) -> None:
    conn.execute(
        "UPDATE agents SET name = ?, base_url = ?, token = ?, offsite = ? WHERE id = ?",
        (name, base_url, token, 1 if offsite else 0, agent_id),
    )
    conn.commit()


def update_agent_contact(
    conn: sqlite3.Connection, agent_id: int, status: str, error_message: str | None = None
) -> None:
    conn.execute(
        "UPDATE agents SET last_contact_at = ?, last_contact_status = ?, last_contact_error = ? WHERE id = ?",
        (now_iso(), status, error_message, agent_id),
    )
    conn.commit()


def count_targets_for_agent(conn: sqlite3.Connection, agent_id: int) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM targets WHERE agent_id = ?", (agent_id,)).fetchone()
    return row["c"]


def list_targets_for_agent(conn: sqlite3.Connection, agent_id: int):
    """Full target rows (not just a count), used to re-sync each one's schedule
    immediately after the owning agent's offsite flag changes, rather than waiting for
    that target's own schedule to be saved again or the app to restart.
    """
    return conn.execute(
        """
        SELECT t.*, a.name AS agent_name, a.offsite AS agent_offsite
        FROM targets t
        LEFT JOIN agents a ON a.id = t.agent_id
        WHERE t.agent_id = ?
        """,
        (agent_id,),
    ).fetchall()


def delete_agent(conn: sqlite3.Connection, agent_id: int) -> None:
    conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    conn.commit()
