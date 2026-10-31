"""WP-08：重建原子替换（swap_in）与任务失败/重试。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from opc.db import Database
from opc.rebuild import swap_in


def _make_db(path: Path, value: str) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE marker(v TEXT)")
    con.execute("INSERT INTO marker(v) VALUES(?)", (value,))
    con.commit()
    con.close()


def _read(path: Path) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT v FROM marker").fetchone()[0]
    finally:
        con.close()


def test_swap_in_backs_up_then_replaces(tmp_path):
    target = tmp_path / "opc.db"
    _make_db(target, "old")
    rebuild_tmp = tmp_path / "opc-rebuild.db"
    _make_db(rebuild_tmp, "new")

    backup = swap_in(target, tmp_path)

    assert Path(backup).exists(), "必须先留备份"
    assert _read(Path(backup)) == "old"
    assert _read(target) == "new", "目标库应换成重建库"
    assert not Path(str(target) + "-wal").exists()
    assert not Path(str(target) + "-shm").exists()
    assert not rebuild_tmp.exists(), "临时库应被原子移走"


def test_swap_in_without_existing_target(tmp_path):
    target = tmp_path / "opc.db"
    rebuild_tmp = tmp_path / "opc-rebuild.db"
    _make_db(rebuild_tmp, "new")
    backup = swap_in(target, tmp_path)
    assert not Path(backup).exists(), "目标不存在时不产生空备份"
    assert _read(target) == "new"


def test_job_failure_then_retry_then_done(tmp_path):
    db = Database(tmp_path / "jobs.db")
    jid = db.enqueue("distill", {"source_id": 1}, priority=8)
    first = db.claim_job()
    assert first["id"] == jid and first["attempts"] == 1
    db.finish_job(jid, ok=False, error="boom")
    with db.connect() as c:
        row = c.execute("SELECT status, error, attempts FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "failed" and row["error"] == "boom" and row["attempts"] == 1

    assert db.retry_job(jid)["ok"] is True
    second = db.claim_job()
    assert second["id"] == jid and second["attempts"] == 2, "重试应重新认领并累计 attempts"
    db.finish_job(jid, ok=True, result={"note_id": 9})
    with db.connect() as c:
        row = c.execute("SELECT status, result FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "done" and "note_id" in (row["result"] or "")


def test_retry_rejects_done_job(tmp_path):
    db = Database(tmp_path / "jobs2.db")
    jid = db.enqueue("capture", {})
    db.claim_job()
    db.finish_job(jid, ok=True)
    assert db.retry_job(jid) == {"ok": False, "reason": "done"}
