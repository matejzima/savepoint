import os

from app import db as db_module
from app import retention


class FakeSettings:
    def __init__(self, state_db_path, backup_target_dir):
        self.state_db_path = state_db_path
        self.backup_target_dir = backup_target_dir


def _make_target(conn, name="t1"):
    return db_module.create_target(conn, name, "postgres", f"{name}-c", "u", "d")


def _set_retention(conn, target_id, daily=None, weekly=None, monthly=None, confirmed=None):
    fields = []
    values = []
    if daily is not None:
        fields.append("retention_daily = ?")
        values.append(daily)
    if weekly is not None:
        fields.append("retention_weekly = ?")
        values.append(weekly)
    if monthly is not None:
        fields.append("retention_monthly = ?")
        values.append(monthly)
    if confirmed is not None:
        fields.append("retention_confirmed = ?")
        values.append(1 if confirmed else 0)
    values.append(target_id)
    conn.execute(f"UPDATE targets SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()


def _make_success_run(conn, target_id, started_at_iso, file_path):
    run_id = db_module.create_backup_run(conn, target_id, status="running", triggered_by="manual")
    conn.execute("UPDATE backup_runs SET started_at = ? WHERE id = ?", (started_at_iso, run_id))
    conn.commit()
    with open(file_path, "wb") as f:
        f.write(b"dump")
    db_module.finish_backup_run(conn, run_id, "success", file_path=file_path, file_size_bytes=4)
    return run_id


def _tag(conn, target_id, run_id):
    run = db_module.get_backup_run(conn, run_id)
    for tier in retention.compute_tags_for_run(conn, target_id, run):
        db_module.add_backup_run_tag(conn, run_id, tier)


# --- compute_tags_for_run ---------------------------------------------------------


def test_first_run_gets_all_three_tiers(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)

    run_id = _make_success_run(conn, tid, "2026-03-10T02:00:00+00:00", str(tmp_path / "a.dump"))
    run = db_module.get_backup_run(conn, run_id)

    tags = retention.compute_tags_for_run(conn, tid, run)

    assert set(tags) == {"daily", "weekly", "monthly"}


def test_second_run_same_iso_week_gets_daily_only(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)

    run1 = _make_success_run(conn, tid, "2026-03-09T02:00:00+00:00", str(tmp_path / "a.dump"))
    _tag(conn, tid, run1)

    run2 = _make_success_run(conn, tid, "2026-03-10T02:00:00+00:00", str(tmp_path / "b.dump"))
    tags2 = retention.compute_tags_for_run(conn, tid, db_module.get_backup_run(conn, run2))

    assert tags2 == ["daily"]


def test_run_crossing_iso_year_week_boundary_gets_new_weekly_tag(tmp_path):
    """2025-12-28 is ISO week 52 of 2025 (a Sunday); 2025-12-29 is ISO week 1 of 2026 (a
    Monday). Same calendar month, one day apart, but a different ISO (year, week), exactly
    the case SQLite's non-ISO strftime('%W') week numbering would get wrong.
    """
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)

    run1 = _make_success_run(conn, tid, "2025-12-28T02:00:00+00:00", str(tmp_path / "a.dump"))
    _tag(conn, tid, run1)

    run2 = _make_success_run(conn, tid, "2025-12-29T02:00:00+00:00", str(tmp_path / "b.dump"))
    tags2 = retention.compute_tags_for_run(conn, tid, db_module.get_backup_run(conn, run2))

    assert "weekly" in tags2


def test_second_run_same_month_gets_no_new_monthly_tag(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)

    run1 = _make_success_run(conn, tid, "2026-03-02T02:00:00+00:00", str(tmp_path / "a.dump"))
    _tag(conn, tid, run1)

    run2 = _make_success_run(conn, tid, "2026-03-20T02:00:00+00:00", str(tmp_path / "b.dump"))
    tags2 = retention.compute_tags_for_run(conn, tid, db_module.get_backup_run(conn, run2))

    assert "monthly" not in tags2
    assert "daily" in tags2


# --- prune_target ------------------------------------------------------------------


def test_prune_target_keeps_exactly_n_per_tier(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)
    _set_retention(conn, tid, daily=2, weekly=100, monthly=100, confirmed=True)

    run_ids = []
    for i, day in enumerate(("2026-01-01", "2026-01-02", "2026-01-03")):
        path = str(tmp_path / f"run{i}.dump")
        run_id = _make_success_run(conn, tid, f"{day}T02:00:00+00:00", path)
        db_module.add_backup_run_tag(conn, run_id, "daily")
        run_ids.append(run_id)

    target = db_module.get_target(conn, tid)
    retention.prune_target(conn, target, str(tmp_path))

    tags = db_module.get_tags_for_runs(conn, run_ids)
    assert tags[run_ids[0]] == []
    assert tags[run_ids[1]] == ["daily"]
    assert tags[run_ids[2]] == ["daily"]


def test_prune_target_keeps_file_while_any_tag_remains(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)
    _set_retention(conn, tid, daily=1, weekly=100, monthly=100, confirmed=True)

    path_old = str(tmp_path / "old.dump")
    run_old = _make_success_run(conn, tid, "2026-01-01T02:00:00+00:00", path_old)
    db_module.add_backup_run_tag(conn, run_old, "daily")
    db_module.add_backup_run_tag(conn, run_old, "monthly")

    path_new = str(tmp_path / "new.dump")
    run_new = _make_success_run(conn, tid, "2026-01-02T02:00:00+00:00", path_new)
    db_module.add_backup_run_tag(conn, run_new, "daily")

    target = db_module.get_target(conn, tid)
    retention.prune_target(conn, target, str(tmp_path))

    # daily retention=1 drops run_old's daily tag, but it still holds "monthly"
    # (retention 100, never pruned), so the file must survive.
    assert db_module.has_any_tags(conn, run_old) is True
    assert os.path.exists(path_old)


def test_prune_target_removes_file_when_last_tag_expires(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)
    _set_retention(conn, tid, daily=1, weekly=100, monthly=100, confirmed=True)

    path_old = str(tmp_path / "old.dump")
    run_old = _make_success_run(conn, tid, "2026-01-01T02:00:00+00:00", path_old)
    db_module.add_backup_run_tag(conn, run_old, "daily")

    path_new = str(tmp_path / "new.dump")
    run_new = _make_success_run(conn, tid, "2026-01-02T02:00:00+00:00", path_new)
    db_module.add_backup_run_tag(conn, run_new, "daily")

    target = db_module.get_target(conn, tid)
    retention.prune_target(conn, target, str(tmp_path))

    assert db_module.has_any_tags(conn, run_old) is False
    assert not os.path.exists(path_old)
    assert os.path.exists(path_new)


def test_prune_target_tolerates_already_missing_file(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)
    _set_retention(conn, tid, daily=1, weekly=100, monthly=100, confirmed=True)

    path_old = str(tmp_path / "old.dump")
    run_old = _make_success_run(conn, tid, "2026-01-01T02:00:00+00:00", path_old)
    db_module.add_backup_run_tag(conn, run_old, "daily")
    os.remove(path_old)  # simulate an operator deleting it by hand

    path_new = str(tmp_path / "new.dump")
    run_new = _make_success_run(conn, tid, "2026-01-02T02:00:00+00:00", path_new)
    db_module.add_backup_run_tag(conn, run_new, "daily")

    target = db_module.get_target(conn, tid)
    retention.prune_target(conn, target, str(tmp_path))  # must not raise

    assert db_module.has_any_tags(conn, run_old) is False


# --- tag_and_prune's confirmation gate ----------------------------------------------


def test_tag_and_prune_skips_pruning_when_not_confirmed(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)
    _set_retention(conn, tid, daily=1, confirmed=False)

    path1 = str(tmp_path / "a.dump")
    run1 = _make_success_run(conn, tid, "2026-01-01T02:00:00+00:00", path1)
    retention.tag_and_prune(conn, db_module.get_target(conn, tid), run1, str(tmp_path))

    path2 = str(tmp_path / "b.dump")
    run2 = _make_success_run(conn, tid, "2026-01-02T02:00:00+00:00", path2)
    retention.tag_and_prune(conn, db_module.get_target(conn, tid), run2, str(tmp_path))

    # daily retention=1 would normally prune run1's daily tag once run2 exists, but
    # retention_confirmed is false, so nothing is ever pruned.
    assert os.path.exists(path1)
    assert os.path.exists(path2)


# --- reconcile_all -------------------------------------------------------------------


def test_reconcile_all_backfills_tags_but_does_not_prune_unconfirmed_target(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)
    _set_retention(conn, tid, daily=1)  # retention_confirmed stays at its default, false

    paths = []
    run_ids = []
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        path = str(tmp_path / f"{day}.dump")
        run_id = _make_success_run(conn, tid, f"{day}T02:00:00+00:00", path)
        paths.append(path)
        run_ids.append(run_id)
    conn.close()

    settings = FakeSettings(state_db, str(tmp_path))
    retention.reconcile_all(settings)

    conn = db_module.get_connection(state_db)
    for run_id in run_ids:
        assert db_module.has_any_tags(conn, run_id) is True
    for path in paths:
        assert os.path.exists(path)


def test_reconcile_all_prunes_confirmed_target(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)
    _set_retention(conn, tid, daily=1, confirmed=True)  # weekly/monthly stay at generous defaults

    paths = []
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):  # same ISO week and month
        path = str(tmp_path / f"{day}.dump")
        _make_success_run(conn, tid, f"{day}T02:00:00+00:00", path)
        paths.append(path)
    conn.close()

    settings = FakeSettings(state_db, str(tmp_path))
    retention.reconcile_all(settings)

    # Jan 1 is "first of week/month" so it keeps its weekly/monthly tag, file survives.
    # Jan 3 is the most recent daily (retention_daily=1), survives.
    # Jan 2 only ever held a daily tag and isn't the most recent, gets pruned.
    assert os.path.exists(paths[0])
    assert not os.path.exists(paths[1])
    assert os.path.exists(paths[2])


def test_reconcile_all_is_idempotent(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)
    _set_retention(conn, tid, daily=1, confirmed=True)

    paths = []
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        path = str(tmp_path / f"{day}.dump")
        _make_success_run(conn, tid, f"{day}T02:00:00+00:00", path)
        paths.append(path)
    conn.close()

    settings = FakeSettings(state_db, str(tmp_path))
    retention.reconcile_all(settings)
    retention.reconcile_all(settings)  # run again, must be a no-op

    conn = db_module.get_connection(state_db)
    tag_count = conn.execute("SELECT COUNT(*) AS c FROM backup_run_tags").fetchone()["c"]

    # weekly (Jan 1) + monthly (Jan 1) + daily (Jan 3, the survivor) = 3, unchanged by the
    # second pass.
    assert tag_count == 3
    assert os.path.exists(paths[0])
    assert not os.path.exists(paths[1])
    assert os.path.exists(paths[2])
