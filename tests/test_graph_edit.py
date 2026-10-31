"""图谱编辑回归：重命名（DB + 页面标题）、连线、断线。"""
from __future__ import annotations

import pytest

from opc.graph_edit import create_edge, list_edits, remove_edge, rename_node, undo_last


def test_rename_entity_updates_db_and_page(env):
    cfg, database, vault = env
    page = vault / "wiki/entities/测试系统.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text('---\ntitle: "测试系统"\n---\n# 测试系统\n', encoding="utf-8")
    eid = database.upsert_entity("测试系统", "测试系统", "system",
                                page_path="wiki/entities/测试系统.md")
    res = rename_node(database, "entity", eid, name="新测试系统")
    assert res["name"] == "新测试系统"
    with database.connect() as c:
        assert c.execute("SELECT name FROM entities WHERE id=?", (eid,)).fetchone()["name"] == "新测试系统"
    text = page.read_text(encoding="utf-8")
    assert 'title: "新测试系统"' in text
    assert "# 新测试系统" in text


def test_rename_concept_does_not_touch_type(env):
    cfg, database, _ = env
    cid = database.upsert_concept("k", "概念甲", "定义")
    rename_node(database, "concept", cid, name="概念乙")
    with database.connect() as c:
        row = c.execute("SELECT name, definition FROM concepts WHERE id=?", (cid,)).fetchone()
    assert row["name"] == "概念乙" and row["definition"] == "定义"


def test_create_and_remove_edge(env):
    cfg, database, _ = env
    a = database.upsert_entity("a", "实体甲", "system")
    b = database.upsert_entity("b", "实体乙", "system")
    res = create_edge(database, src_type="entity", src_id=a, dst_type="entity", dst_id=b)
    eid = res["edge_id"]
    assert eid
    with database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM edges WHERE id=?", (eid,)).fetchone()[0] == 1
    remove_edge(database, eid)
    with database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM edges WHERE id=?", (eid,)).fetchone()[0] == 0


def test_create_edge_rejects_self_and_unknown(env):
    cfg, database, _ = env
    a = database.upsert_entity("a", "实体甲", "system")
    with pytest.raises(ValueError):
        create_edge(database, src_type="entity", src_id=a, dst_type="entity", dst_id=a)
    with pytest.raises(ValueError):
        create_edge(database, src_type="entity", src_id=a, dst_type="entity", dst_id=999999)


def test_rename_unknown_node_raises(env):
    cfg, database, _ = env
    with pytest.raises(ValueError):
        rename_node(database, "entity", 999999, name="x")


def test_rename_rejects_duplicate_name(env):
    cfg, database, _ = env
    a = database.upsert_entity("实体甲", "实体甲", "system")
    database.upsert_entity("实体乙", "实体乙", "system")
    with pytest.raises(ValueError):
        rename_node(database, "entity", a, name="实体乙")


def test_undo_rename_restores_name_and_page(env):
    cfg, database, vault = env
    page = vault / "wiki/entities/测试系统.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text('---\ntitle: "测试系统"\n---\n# 测试系统\n', encoding="utf-8")
    eid = database.upsert_entity("测试系统", "测试系统", "system",
                                page_path="wiki/entities/测试系统.md")
    rename_node(database, "entity", eid, name="改名后")
    assert "改名后" in page.read_text(encoding="utf-8")
    res = undo_last(database)
    assert res["undone"] == "rename"
    with database.connect() as c:
        assert c.execute("SELECT name FROM entities WHERE id=?", (eid,)).fetchone()["name"] == "测试系统"
    text = page.read_text(encoding="utf-8")
    assert 'title: "测试系统"' in text and "# 测试系统" in text


def test_undo_edge_add_and_delete(env):
    cfg, database, _ = env
    a = database.upsert_entity("a", "实体甲", "system")
    b = database.upsert_entity("b", "实体乙", "system")

    added = create_edge(database, src_type="entity", src_id=a, dst_type="entity", dst_id=b)
    assert added["created"] is True
    undo_last(database)
    with database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM edges WHERE id=?", (added["edge_id"],)).fetchone()[0] == 0

    again = create_edge(database, src_type="entity", src_id=a, dst_type="entity", dst_id=b)
    eid = again["edge_id"]
    remove_edge(database, eid)
    with database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM edges WHERE id=?", (eid,)).fetchone()[0] == 0
    undo_last(database)  # 撤销删除
    with database.connect() as c:
        row = c.execute("SELECT src_id, dst_id, rel FROM edges WHERE rel='related_to'").fetchone()
    assert row and row["src_id"] == a and row["dst_id"] == b


def test_duplicate_edge_is_idempotent(env):
    cfg, database, _ = env
    a = database.upsert_entity("a", "实体甲", "system")
    b = database.upsert_entity("b", "实体乙", "system")
    first = create_edge(database, src_type="entity", src_id=a, dst_type="entity", dst_id=b)
    second = create_edge(database, src_type="entity", src_id=a, dst_type="entity", dst_id=b)
    assert first["edge_id"] == second["edge_id"]
    assert second["created"] is False


def test_list_edits_and_empty_undo(env):
    cfg, database, _ = env
    with pytest.raises(ValueError):
        undo_last(database)
    a = database.upsert_entity("a", "实体甲", "system")
    rename_node(database, "entity", a, name="实体丙")
    items = list_edits(database)
    assert items and items[0]["kind"] == "rename"
