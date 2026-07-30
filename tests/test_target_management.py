from app import db as db_module
from app import jobs


def _make_target(conn, name="t1"):
    return db_module.create_target(conn, name, "postgres", f"{name}-c", "u", "d")


def setup_function(_):
    jobs._in_progress.clear()


def test_count_and_list_target_files_only_include_existing_files(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)

    existing_path = str(tmp_path / "exists.dump")
    with open(existing_path, "wb") as f:
        f.write(b"dump")
    missing_path = str(tmp_path / "pruned.dump")  # never created, simulates a prior retention prune

    run1 = db_module.create_backup_run(conn, tid, status="running")
    db_module.finish_backup_run(conn, run1, "success", file_path=existing_path, file_size_bytes=4)
    run2 = db_module.create_backup_run(conn, tid, status="running")
    db_module.finish_backup_run(conn, run2, "success", file_path=missing_path, file_size_bytes=4)

    assert db_module.count_target_files(conn, tid) == 1
    assert db_module.list_target_file_paths(conn, tid) == [existing_path]


def test_count_target_files_is_zero_with_no_runs(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)

    assert db_module.count_target_files(conn, tid) == 0
    assert db_module.list_target_file_paths(conn, tid) == []


def test_delete_target_cascades_tags_runs_and_target_row(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)

    run_id = db_module.create_backup_run(conn, tid, status="running")
    db_module.finish_backup_run(
        conn, run_id, "success", file_path=str(tmp_path / "a.dump"), file_size_bytes=1
    )
    db_module.add_backup_run_tag(conn, run_id, "daily")

    db_module.delete_target(conn, tid)

    assert db_module.get_target(conn, tid) is None
    assert db_module.list_backup_runs(conn, tid) == []
    tag_count = conn.execute(
        "SELECT COUNT(*) AS c FROM backup_run_tags WHERE backup_run_id = ?", (run_id,)
    ).fetchone()["c"]
    assert tag_count == 0


def test_delete_target_cascades_restore_runs(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)

    run_id = db_module.create_backup_run(conn, tid, status="running")
    db_module.finish_backup_run(conn, run_id, "success", file_path=str(tmp_path / "a.dump"), file_size_bytes=1)
    restore_run_id = db_module.create_restore_run(conn, tid, run_id, status="success")

    db_module.delete_target(conn, tid)

    assert conn.execute(
        "SELECT COUNT(*) AS c FROM restore_runs WHERE id = ?", (restore_run_id,)
    ).fetchone()["c"] == 0


def test_create_and_finish_restore_run(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)
    backup_run_id = db_module.create_backup_run(conn, tid, status="running")
    db_module.finish_backup_run(conn, backup_run_id, "success", file_path="/tmp/x.dump", file_size_bytes=1)

    restore_run_id = db_module.create_restore_run(conn, tid, backup_run_id, status="running")
    run = conn.execute("SELECT * FROM restore_runs WHERE id = ?", (restore_run_id,)).fetchone()
    assert run["status"] == "running"
    assert run["finished_at"] is None

    db_module.finish_restore_run(conn, restore_run_id, "success", stopped_container=True, error_message=None)
    run = conn.execute("SELECT * FROM restore_runs WHERE id = ?", (restore_run_id,)).fetchone()
    assert run["status"] == "success"
    assert run["stopped_container"] == 1
    assert run["finished_at"] is not None


def test_list_restore_runs_includes_backup_started_at_and_is_target_scoped(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid1 = _make_target(conn, "t1")
    tid2 = _make_target(conn, "t2")

    backup_run_id = db_module.create_backup_run(conn, tid1, status="running")
    db_module.finish_backup_run(conn, backup_run_id, "success", file_path="/tmp/x.dump", file_size_bytes=1)
    restore_run_id = db_module.create_restore_run(conn, tid1, backup_run_id, status="success")

    other_backup_run_id = db_module.create_backup_run(conn, tid2, status="running")
    db_module.finish_backup_run(conn, other_backup_run_id, "success", file_path="/tmp/y.dump", file_size_bytes=1)
    db_module.create_restore_run(conn, tid2, other_backup_run_id, status="success")

    runs = db_module.list_restore_runs(conn, tid1)
    assert len(runs) == 1
    assert runs[0]["id"] == restore_run_id
    assert runs[0]["backup_started_at"] is not None


def test_list_eligible_backups_for_restore_only_includes_existing_successful_files(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)

    existing_path = str(tmp_path / "exists.dump")
    with open(existing_path, "wb") as f:
        f.write(b"dump")
    pruned_path = str(tmp_path / "pruned.dump")  # never created, simulates a prior prune

    good_run = db_module.create_backup_run(conn, tid, status="running")
    db_module.finish_backup_run(conn, good_run, "success", file_path=existing_path, file_size_bytes=4)

    pruned_run = db_module.create_backup_run(conn, tid, status="running")
    db_module.finish_backup_run(conn, pruned_run, "success", file_path=pruned_path, file_size_bytes=4)

    failed_run = db_module.create_backup_run(conn, tid, status="running")
    db_module.finish_backup_run(conn, failed_run, "failure", error_message="boom")

    eligible = db_module.list_eligible_backups_for_restore(conn, tid)
    eligible_ids = {r["id"] for r in eligible}
    assert eligible_ids == {good_run}


def test_delete_target_does_not_touch_other_targets(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid1 = _make_target(conn, "t1")
    tid2 = _make_target(conn, "t2")

    run_id = db_module.create_backup_run(conn, tid2, status="running")
    db_module.finish_backup_run(
        conn, run_id, "success", file_path=str(tmp_path / "b.dump"), file_size_bytes=1
    )
    db_module.add_backup_run_tag(conn, run_id, "daily")

    db_module.delete_target(conn, tid1)

    assert db_module.get_target(conn, tid2) is not None
    assert len(db_module.list_backup_runs(conn, tid2)) == 1
    tag_count = conn.execute(
        "SELECT COUNT(*) AS c FROM backup_run_tags WHERE backup_run_id = ?", (run_id,)
    ).fetchone()["c"]
    assert tag_count == 1


def test_update_target_connection_persists_new_values(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)

    db_module.update_target_connection(conn, tid, "new-c", "newuser", "newdb", None)

    target = db_module.get_target(conn, tid)
    assert target["container_name"] == "new-c"
    assert target["db_user"] == "newuser"
    assert target["db_name"] == "newdb"
    assert target["engine"] == "postgres"  # untouched


def test_update_target_enabled_flips_flag(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    tid = _make_target(conn)

    target = db_module.get_target(conn, tid)
    assert target["enabled"] == 1

    db_module.update_target_enabled(conn, tid, False)
    assert db_module.get_target(conn, tid)["enabled"] == 0

    db_module.update_target_enabled(conn, tid, True)
    assert db_module.get_target(conn, tid)["enabled"] == 1


def test_jobs_is_in_progress_is_read_only():
    assert jobs.is_in_progress(555) is False
    jobs.try_claim(555)
    assert jobs.is_in_progress(555) is True
    assert jobs.is_in_progress(555) is True  # peeking again doesn't release it
    jobs.release(555)
    assert jobs.is_in_progress(555) is False


def test_create_and_get_agent(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)

    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    agent = db_module.get_agent(conn, agent_id)

    assert agent["name"] == "remote-host"
    assert agent["base_url"] == "http://remote-host:8000"
    assert agent["token"] == "tok"
    assert agent["offsite"] == 0
    assert agent["last_contact_at"] is None


def test_list_agents_ordered_by_name(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    db_module.create_agent(conn, "zzz-host", "http://z:8000", "t")
    db_module.create_agent(conn, "aaa-host", "http://a:8000", "t")

    names = [a["name"] for a in db_module.list_agents(conn)]
    assert names == ["aaa-host", "zzz-host"]


def test_update_agent_changes_all_fields(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://old:8000", "old-tok", offsite=False)

    db_module.update_agent(conn, agent_id, "renamed", "http://new:8000", "new-tok", True)

    agent = db_module.get_agent(conn, agent_id)
    assert agent["name"] == "renamed"
    assert agent["base_url"] == "http://new:8000"
    assert agent["token"] == "new-tok"
    assert agent["offsite"] == 1


def test_update_agent_contact_records_status_and_error(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")

    db_module.update_agent_contact(conn, agent_id, "error", "connection refused")
    agent = db_module.get_agent(conn, agent_id)
    assert agent["last_contact_status"] == "error"
    assert agent["last_contact_error"] == "connection refused"
    assert agent["last_contact_at"] is not None

    db_module.update_agent_contact(conn, agent_id, "ok", None)
    agent = db_module.get_agent(conn, agent_id)
    assert agent["last_contact_status"] == "ok"
    assert agent["last_contact_error"] is None


def test_count_targets_for_agent(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    other_agent_id = db_module.create_agent(conn, "cottage", "http://cottage:8000", "tok")

    assert db_module.count_targets_for_agent(conn, agent_id) == 0

    db_module.create_target(conn, "remote-a", "postgres", "c1", "u", "d", agent_id=agent_id)
    db_module.create_target(conn, "remote-b", "postgres", "c2", "u", "d", agent_id=agent_id)
    db_module.create_target(conn, "remote-c", "postgres", "c3", "u", "d", agent_id=other_agent_id)

    assert db_module.count_targets_for_agent(conn, agent_id) == 2
    assert db_module.count_targets_for_agent(conn, other_agent_id) == 1


def test_delete_agent_removes_row(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")

    db_module.delete_agent(conn, agent_id)

    assert db_module.get_agent(conn, agent_id) is None


def test_create_target_with_agent_id_and_get_target_exposes_agent_fields(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok", offsite=True)

    target_id = db_module.create_target(conn, "remote-db", "postgres", "c", "u", "d", agent_id=agent_id)

    target = db_module.get_target(conn, target_id)
    assert target["agent_id"] == agent_id
    assert target["agent_name"] == "remote-host"
    assert target["agent_offsite"] == 1


def test_local_target_has_null_agent_fields(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    target_id = _make_target(conn)

    target = db_module.get_target(conn, target_id)
    assert target["agent_id"] is None
    assert target["agent_name"] is None
    assert not target["agent_offsite"]


def test_update_target_connection_preserves_agent_id_when_passed(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    target_id = db_module.create_target(conn, "remote-db", "postgres", "c", "u", "d", agent_id=agent_id)

    db_module.update_target_connection(conn, target_id, "new-c", "newuser", "newdb", None, agent_id=agent_id)

    target = db_module.get_target(conn, target_id)
    assert target["container_name"] == "new-c"
    assert target["agent_id"] == agent_id


def test_list_targets_and_list_all_targets_expose_agent_offsite(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "cottage", "http://cottage:8000", "tok", offsite=True)
    db_module.create_target(conn, "remote-db", "postgres", "c", "u", "d", agent_id=agent_id)

    listed = db_module.list_targets(conn)
    all_targets = db_module.list_all_targets(conn)

    assert listed[0]["agent_offsite"] == 1
    assert listed[0]["agent_name"] == "cottage"
    assert all_targets[0]["agent_offsite"] == 1


def test_list_targets_for_agent_returns_only_that_agents_rows(tmp_path):
    state_db = str(tmp_path / "state.db")
    db_module.init_db(state_db)
    conn = db_module.get_connection(state_db)
    agent_id = db_module.create_agent(conn, "remote-host", "http://remote-host:8000", "tok")
    other_agent_id = db_module.create_agent(conn, "cottage", "http://cottage:8000", "tok")
    db_module.create_target(conn, "remote-a", "postgres", "c1", "u", "d", agent_id=agent_id)
    db_module.create_target(conn, "remote-b", "postgres", "c2", "u", "d", agent_id=agent_id)
    db_module.create_target(conn, "remote-c", "postgres", "c3", "u", "d", agent_id=other_agent_id)
    _make_target(conn, "local")

    rows = db_module.list_targets_for_agent(conn, agent_id)

    assert {r["name"] for r in rows} == {"remote-a", "remote-b"}
    assert all(r["agent_id"] == agent_id for r in rows)
