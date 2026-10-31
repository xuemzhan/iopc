"""M1 本体治理验收（FR-502~505，A1/A3/A5/A6）。

- types：分布与枚举外占比
- typefix：规则 + LLM 通道，默认 dry-run，apply 写审计，幂等
- candidates：别名包含 / 共现邻居 Jaccard，只提议不合并
- merge：边重指、来源集合合并、别名并入、状态 merged、页面提示、幂等
- review：reject 后从图里消失但库内保留
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import opc.config as config_mod
import opc.ontology as ontology_mod
from opc.config import Config
from opc.db import Database
from opc.util import normalize_key


class TypeStub:
    """类型校正模型：把输入全部判成 tool（测试里可替换 payload）。"""

    model = "stub-type"
    mapping: dict[str, str] = {}

    def chat_json(self, messages, *args, **kwargs):
        payload = json.loads(messages[-1]["content"])
        return {"items": [{"name": p["name"],
                           "type": self.mapping.get(p["name"], "tool")} for p in payload]}

    def chat(self, *args, **kwargs):  # pragma: no cover
        return "ok"


@pytest.fixture()
def menv(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "wiki" / "entities").mkdir(parents=True)
    (vault / "wiki" / "concepts").mkdir()
    (vault / "raw").mkdir()
    cfg = Config({
        "vault": {"path": str(vault), "inbox": "inbox", "wiki": "wiki",
                  "write_raw": True, "auto_publish": True, "protect_existing": True,
                  "min_sources_for_page": 2},
        "ingest": {"index_enabled": True},
        "ontology": {"adjudicate_min_confidence": 0.7},
    })
    monkeypatch.setattr(config_mod, "_cfg", cfg)
    monkeypatch.setattr(ontology_mod, "llm", lambda *a, **k: TypeStub())
    return {"cfg": cfg, "database": Database(tmp_path / "m1.db"), "vault": vault}


def _ent(database, name, type_="system", aliases=None):
    eid = database.upsert_entity(normalize_key(name), name, type_)
    if aliases:
        with database.tx() as c:
            c.execute("UPDATE entities SET aliases=? WHERE id=?",
                      (json.dumps(aliases, ensure_ascii=False), eid))
    return eid


def _edge_sources(database, a, b):
    with database.connect() as c:
        row = c.execute(
            "SELECT id, weight FROM edges WHERE src_type='entity' AND src_id=?"
            " AND dst_type='entity' AND dst_id=? AND rel='co_occurs'", (a, b)).fetchone()
        n = c.execute("SELECT COUNT(*) FROM edge_sources WHERE edge_id=?",
                      (row["id"],)).fetchone()[0] if row else None
    return (row["weight"], n) if row else (None, None)


def test_types_distribution_counts_outside_enum(menv):
    database = menv["database"]
    _ent(database, "甲系统", "system")
    _ent(database, "乙系统", "system")
    _ent(database, "丙产品", "product")   # 枚举外
    _ent(database, "丁", "other")
    res = ontology_mod.types(database)
    assert res["total"] == 4
    assert res["outside_enum"] == 1
    assert res["other_ratio"] == 0.25
    assert res["allowed"] == sorted(ontology_mod.ENTITY_TYPES)


def test_typefix_rule_channel_dry_run_then_apply(menv):
    database = menv["database"]
    eid = _ent(database, "某某研究院", "thing")
    dry = ontology_mod.typefix(database, use_llm=False)
    assert dry["proposed"] >= 1
    assert database.object_row("entity", eid)["type"] == "thing", "dry-run 不落库"
    with database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM events WHERE kind='ontology.typefix'"
                         ).fetchone()[0] == 0

    done = ontology_mod.typefix(database, apply=True, use_llm=False)
    assert done["applied"] >= 1
    assert database.object_row("entity", eid)["type"] == "org"
    with database.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM events WHERE kind='ontology.typefix'").fetchone()[0]
    assert n == 1

    again = ontology_mod.typefix(database, apply=True, use_llm=False)
    assert again["applied"] == 0, "同一条目不应重复改动"


def test_typefix_llm_channel(menv):
    database = menv["database"]
    TypeStub.mapping = {"星云": "tool"}
    eid = _ent(database, "星云", "product")     # 规则命中不了
    res = ontology_mod.typefix(database, apply=True, use_llm=True)
    assert any(ch["by"] == "llm" for ch in res["changes"])
    assert database.object_row("entity", eid)["type"] == "tool"


def test_candidates_find_alias_match(menv):
    database = menv["database"]
    a = _ent(database, "总体能力", "other", aliases=["总体能力建设"])
    b = _ent(database, "总体能力建设", "project")
    res = ontology_mod.candidates(database)
    pairs = {(r["a_id"], r["b_id"]) for r in res["open"]}
    assert (min(a, b), max(a, b)) in pairs
    with database.connect() as c:
        row = c.execute("SELECT reason FROM merge_candidates WHERE a_id=? AND b_id=?",
                        (min(a, b), max(a, b))).fetchone()
    assert "别名" in row["reason"] or "相同" in row["reason"]


def test_candidates_find_neighbor_jaccard(menv):
    database = menv["database"]
    a = _ent(database, "甲", "system")
    b = _ent(database, "乙", "system")
    c = _ent(database, "丙", "system")
    d = _ent(database, "丁", "system")
    for u, v in ((a, b), (a, c), (a, d), (b, c), (b, d)):
        database.add_edge("entity", u, "entity", v, "co_occurs", source_id=1)
    with database.tx() as conn:  # 邻居通道只对已准入实体生效
        conn.execute("UPDATE entities SET src_count=2")
    res = ontology_mod.candidates(database, min_jaccard=0.5)
    hit = [r for r in res["open"] if {r["a_id"], r["b_id"]} == {a, b}]
    assert hit and "Jaccard" in hit[0]["reason"]


def test_merge_repoints_edges_and_merges_sources(menv):
    database = menv["database"]
    a = _ent(database, "甲系统", "system")
    b = _ent(database, "甲系统旧", "system", aliases=["老甲"])
    c = _ent(database, "丙系统", "system")
    database.add_edge("entity", a, "entity", c, "co_occurs", source_id=1)
    database.add_edge("entity", b, "entity", c, "co_occurs", source_id=2)
    database.add_edge("entity", a, "entity", b, "co_occurs", source_id=1)
    with database.tx() as conn:
        conn.execute("INSERT INTO merge_candidates(kind,a_id,b_id,score,reason,status,"
                     "created_at,updated_at) VALUES('entity',?,?,0.9,'test','open','x','x')",
                     (min(a, b), max(a, b)))

    res = ontology_mod.merge(database, a, b)
    assert res["changed"] is True and res["edges_to_merged"] == 0
    assert res["self_loops"] == 1, "a-b 边归并后成自环应删除"
    weight, n = _edge_sources(database, a, c)
    assert weight == 2 and n == 2, "撞键后按来源集合合并 weight"
    row_b = database.object_row("entity", b)
    assert row_b["status"] == "merged" and int(row_b["merged_into"]) == a
    aliases = json.loads(database.object_row("entity", a)["aliases"] or "[]")
    assert "甲系统旧" in aliases and "老甲" in aliases
    with database.connect() as conn:
        st = conn.execute("SELECT status FROM merge_candidates WHERE a_id=? AND b_id=?",
                          (min(a, b), max(a, b))).fetchone()[0]
        ev = conn.execute("SELECT COUNT(*) FROM events WHERE kind='ontology.merge'").fetchone()[0]
    assert st == "merged" and ev == 1


def test_merge_is_idempotent_and_warns_page(menv):
    database, vault = menv["database"], menv["vault"]
    a = _ent(database, "乙系统", "system")
    b = _ent(database, "乙系统旧", "system")
    page = vault / "wiki" / "entities" / "b.md"
    page.write_text("---\ntitle: \"乙系统旧\"\ntype: entity\n---\n# 乙系统旧\n\n手工正文\n",
                    encoding="utf-8")
    with database.tx() as conn:
        conn.execute("UPDATE entities SET page_path=? WHERE id=?",
                     ("wiki/entities/b.md", b))
    first = ontology_mod.merge(database, a, b)
    assert first["page_warned"] is True
    text = page.read_text(encoding="utf-8")
    assert "已并入" in text and "手工正文" in text
    second = ontology_mod.merge(database, a, b)
    assert second["changed"] is False


def test_merge_refuses_locked_victim(menv):
    database = menv["database"]
    a = _ent(database, "丁系统", "system")
    b = _ent(database, "丁系统旧", "system")
    database.set_locked("entity", b, True, by="human")
    with pytest.raises(ValueError):
        ontology_mod.merge(database, a, b)


def test_lock_pageless_entity_creates_page(menv, tmp_path):
    """FR-511：没有页面的条目在锁定时补一张最小页，重建后锁定态不丢。"""
    from opc.rebuild import build_from_vault
    from opc.vault import Vault

    database = menv["database"]
    eid = _ent(database, "无页实体", "system")
    assert not database.object_row("entity", eid)["page_path"]
    ontology_mod.lock(database, "entity", eid)
    row = database.object_row("entity", eid)
    assert row["page_path"], "锁定时应补页"
    page = menv["vault"] / row["page_path"]
    assert page.exists() and "locked: true" in page.read_text(encoding="utf-8")

    out = build_from_vault(Vault(), db_path=tmp_path / "rb-lock.db")
    assert out["counts"]["locked"] == 1


def test_review_reject_hides_entity_but_keeps_row(menv):
    database = menv["database"]
    eid = _ent(database, "戊系统", "system")
    with database.tx() as c:
        c.execute("UPDATE entities SET src_count=3, status='confirmed' WHERE id=?", (eid,))
    def in_graph():
        return any(n["group"] == "entity" and n["ref"] == eid
                   for n in database.graph(50)["nodes"])

    assert in_graph()
    res = ontology_mod.review(database, "entity", eid, action="reject")
    assert res["status"] == "rejected"
    assert not in_graph()
    assert database.object_row("entity", eid)["name"] == "戊系统"
    with database.connect() as c:
        ev = c.execute("SELECT COUNT(*) FROM events WHERE kind='ontology.review'").fetchone()[0]
    assert ev == 1


def _with_page(menv, eid, slug: str, name: str) -> object:
    vault = menv["vault"]
    page = vault / "wiki" / "entities" / f"{slug}.md"
    page.write_text(
        f"---\ntitle: \"{name}\"\ntype: entity\nentity_type: \"system\"\n---\n"
        f"# {name}\n\n## Overview\n手工正文\n", encoding="utf-8")
    with menv["database"].tx() as c:
        c.execute("UPDATE entities SET page_path=? WHERE id=?",
                  (f"wiki/entities/{slug}.md", eid))
    return page


def test_lock_persists_to_frontmatter(menv):
    database = menv["database"]
    eid = _ent(database, "锁定实体", "system")
    page = _with_page(menv, eid, "locked-one", "锁定实体")
    ontology_mod.lock(database, "entity", eid, by="tester")
    text = page.read_text(encoding="utf-8")
    assert "locked: true" in text and 'locked_by: "tester"' in text
    assert "手工正文" in text, "写 frontmatter 不能丢正文"
    ontology_mod.unlock(database, "entity", eid)
    text = page.read_text(encoding="utf-8")
    assert "locked: false" in text and "locked: true" not in text


def test_rebuild_restores_locked_from_page(menv, tmp_path):
    from opc.rebuild import build_from_vault
    from opc.vault import Vault

    vault = menv["vault"]
    page = vault / "wiki" / "entities" / "keep-lock.md"
    page.write_text(
        "---\ntitle: \"保留锁\"\ntype: entity\nentity_type: \"system\"\n"
        "locked: true\nlocked_by: \"tester\"\n---\n# 保留锁\n\n正文\n", encoding="utf-8")
    out = build_from_vault(Vault(), db_path=tmp_path / "rb.db")
    assert out["counts"]["locked"] == 1
    from opc.db import Database as DB

    rebuilt = DB(tmp_path / "rb.db")
    with rebuilt.connect() as c:
        row = c.execute("SELECT locked, locked_by FROM entities WHERE name='保留锁'").fetchone()
    assert row["locked"] == 1 and row["locked_by"] == "tester"


def test_relock_recovers_db_from_pages(menv):
    database = menv["database"]
    eid = _ent(database, "页上已锁", "system")
    page = _with_page(menv, eid, "page-locked", "页上已锁")
    text = page.read_text(encoding="utf-8").replace(
        "entity_type: \"system\"", "entity_type: \"system\"\nlocked: true")
    page.write_text(text, encoding="utf-8")
    dry = ontology_mod.relock(database)
    assert dry["found"] == 1 and dry["changed"] == 1
    assert database.object_row("entity", eid)["locked"] == 0, "默认只统计"
    done = ontology_mod.relock(database, apply=True)
    assert done["changed"] == 1
    assert database.object_row("entity", eid)["locked"] == 1
    with database.connect() as c:
        ev = c.execute("SELECT COUNT(*) FROM events WHERE kind='ontology.relock'").fetchone()[0]
    assert ev == 1
