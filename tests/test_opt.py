"""优化批次回归：原子写、FK、动作解析。"""
from __future__ import annotations

import sqlite3

import pytest

from opc.actions import collect
from opc.db import Database
from opc.util import atomic_write


def test_atomic_write_leaves_no_tmp(tmp_path):
    p = tmp_path / "a.md"
    atomic_write(p, "第一版")
    atomic_write(p, "第二版")
    assert p.read_text(encoding="utf-8") == "第二版"
    assert not list(tmp_path.glob("*.tmp-*")), "临时文件必须清理"


def test_foreign_keys_enforced(tmp_path):
    db = Database(tmp_path / "t.db")
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx() as c:
            c.execute(
                "INSERT INTO notes(source_id, kind, title, slug, created_at, updated_at)"
                " VALUES(999999, 'source', 'x', 'x', 'now', 'now')"
            )


def test_actions_owner_split_on_last_marker(tmp_path):
    db = Database(tmp_path / "t.db")
    body = "## 行动项\n- [ ] 写报告 —— @李四 —— 周五前\n"
    db.put_note(source_id=None, kind="source", title="t", slug="t", path="wiki/t.md",
                summary="", body_md=body, tags=[], status="published", model="stub")
    items = collect(db)
    assert items and items[0]["text"] == "写报告"
    assert "@" not in items[0]["text"]
