"""WP-09：MCP 工具面（本体 / 注记 / 图谱编辑）与配置输出。"""
from __future__ import annotations

import json

import pytest

import opc.mcp_server as mcp
from opc.util import normalize_key


@pytest.fixture()
def mcp_env(env, monkeypatch):
    cfg, database, vault = env
    monkeypatch.setattr(mcp, "db", lambda: database)
    return database


def _ent(database, name, type_="system", aliases=None):
    eid = database.upsert_entity(normalize_key(name), name, type_)
    if aliases:
        with database.tx() as c:
            c.execute("UPDATE entities SET aliases=? WHERE id=?",
                      (json.dumps(aliases, ensure_ascii=False), eid))
    return eid


def test_tools_list_covers_governance(mcp_env):
    names = {t["name"] for t in mcp.TOOLS}
    assert {"opc_ontology_status", "opc_ontology_merge", "opc_ontology_review",
            "opc_note", "opc_notes", "opc_graph_edit"} <= names
    assert len(mcp.TOOLS) >= 15
    resp = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert len(resp["result"]["tools"]) == len(mcp.TOOLS)


def test_ontology_status_tool(mcp_env):
    database = mcp_env
    _ent(database, "甲系统", "system")
    _ent(database, "乙系统", "other")
    res = mcp._call("opc_ontology_status", {})
    assert res["types"]["total"] == 2
    assert "proposed" in res and "candidates_open" in res


def test_note_tools_roundtrip(mcp_env):
    database = mcp_env
    made = mcp._call("opc_note", {"text": "这个结论的证据在哪？", "kind": "question"})
    assert made["id"] > 0 and "anchor_kind" in made
    listed = mcp._call("opc_notes", {"scope": "recent", "limit": 5})
    assert any(it["id"] == made["id"] for it in listed["items"])
    with database.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
    assert n == 1


def test_ontology_review_and_merge_tools(mcp_env):
    database = mcp_env
    a = _ent(database, "丙系统", "system")
    b = _ent(database, "丙系统旧", "system", aliases=["老丙"])
    c = _ent(database, "丁系统", "system")
    database.add_edge("entity", a, "entity", c, "co_occurs", source_id=1)
    database.add_edge("entity", b, "entity", c, "co_occurs", source_id=2)
    rejected = mcp._call("opc_ontology_review", {"ident": str(b), "action": "reject"})
    assert rejected["status"] == "rejected"
    # 退场后仍可归并（合并不要求 confirmed）
    merged = mcp._call("opc_ontology_merge", {"a": str(a), "b": str(b), "reason": "同义"})
    assert merged["changed"] is True and merged["edges_to_merged"] == 0
    with database.connect() as conn:
        w = conn.execute(
            "SELECT weight FROM edges WHERE src_type='entity' AND src_id=?"
            " AND dst_type='entity' AND dst_id=? AND rel='co_occurs'", (a, c)).fetchone()[0]
    assert w == 2


def test_graph_edit_tools(mcp_env):
    database = mcp_env
    a = _ent(database, "戊系统", "system")
    b = _ent(database, "己系统", "system")
    renamed = mcp._call("opc_graph_edit", {"action": "rename", "kind": "entity",
                                           "node_id": a, "name": "戊系统新版"})
    assert renamed["name"] == "戊系统新版"
    linked = mcp._call("opc_graph_edit", {
        "action": "link", "src_type": "entity", "src_id": a,
        "dst_type": "entity", "dst_id": b, "rel": "related_to"})
    assert linked["ok"] is True
    with database.connect() as c:
        eid = c.execute("SELECT id FROM edges WHERE src_id=? AND dst_id=?", (a, b)).fetchone()["id"]
    cut = mcp._call("opc_graph_edit", {"action": "unlink", "edge_id": eid})
    assert cut["ok"] is True


def test_tools_call_returns_content(mcp_env):
    _ent(mcp_env, "庚系统", "system")
    resp = mcp.handle({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "opc_ontology_status", "arguments": {}},
    })
    assert resp["id"] == 7
    text = resp["result"]["content"][0]["text"]
    assert "types" in text
