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
    retention_daily INTEGER NOT NULL DEFAULT 7,
    retention_weekly INTEGER NOT NULL DEFAULT 4,
    retention_monthly INTEGER NOT NULL DEFAULT 2,
    retention_confirmed INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_runs (
    id INTEGER PRIMARY KEY,
    target_id INTEGER NOT NULL REFERENCES targets(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    file_path TEXT,
    file_size_bytes INTEGER,
    error_message TEXT,
    method TEXT,
    triggered_by TEXT
);

CREATE TABLE IF NOT EXISTS backup_run_tags (
    backup_run_id INTEGER NOT NULL REFERENCES backup_runs(id),
    tier TEXT NOT NULL,
    tagged_at TEXT NOT NULL,
    PRIMARY KEY (backup_run_id, tier)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS restore_runs (
    id INTEGER PRIMARY KEY,
    target_id INTEGER NOT NULL REFERENCES targets(id),
    backup_run_id INTEGER NOT NULL REFERENCES backup_runs(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stopped_container INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    token TEXT NOT NULL,
    offsite INTEGER NOT NULL DEFAULT 0,
    last_contact_at TEXT,
    last_contact_status TEXT,
    last_contact_error TEXT,
    created_at TEXT NOT NULL
);
