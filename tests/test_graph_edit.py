"""图谱编辑回归：重命名（DB + 页面标题）、连线、断线。"""
from __future__ import annotations

import pytest

from opc.graph_edit import create_edge, remove_edge, rename_node


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
