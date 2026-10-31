"""本体 M2 REST 契约（WP-04）：types / candidates / queue / typefix / merge / approve。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import opc.ontology as ontology_mod
from opc.hub import app as hub
from opc.util import normalize_key


class TypeStub:
    model = "stub"

    def chat_json(self, messages, *args, **kwargs):
        payload = json.loads(messages[-1]["content"])
        return {"items": [{"name": p["name"], "type": "tool"} for p in payload]}

    def chat(self, *args, **kwargs):  # pragma: no cover
        return "ok"


@pytest.fixture()
def client(env, monkeypatch):
    cfg, database, vault = env
    monkeypatch.setattr(hub, "db", lambda: database)
    monkeypatch.setattr(ontology_mod, "llm", lambda *a, **k: TypeStub())
    return TestClient(hub.app)


def _ent(database, name, type_="system", aliases=None):
    eid = database.upsert_entity(normalize_key(name), name, type_)
    if aliases:
        with database.tx() as c:
            c.execute("UPDATE entities SET aliases=? WHERE id=?",
                      (json.dumps(aliases, ensure_ascii=False), eid))
    return eid


def test_ontology_types_endpoint(client, env):
    _, database, _ = env
    _ent(database, "某系统", "system")
    _ent(database, "某产品", "product")
    r = client.get("/api/ontology/types?kind=entity")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and body["outside_enum"] == 1


def test_ontology_queue_lists_proposed(client, env):
    _, database, _ = env
    eid = _ent(database, "待审实体", "system")
    with database.tx() as c:
        c.execute("UPDATE entities SET status='proposed' WHERE id=?", (eid,))
    body = client.get("/api/ontology/queue").json()
    assert any(it["id"] == eid for it in body["items"])


def test_ontology_approve_reject_endpoint(client, env):
    _, database, _ = env
    eid = _ent(database, "退场实体", "system")
    r = client.post("/api/ontology/approve",
                    json={"kind": "entity", "ident": str(eid), "action": "reject"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert database.object_row("entity", eid)["status"] == "rejected"


def test_ontology_merge_endpoint(client, env):
    _, database, _ = env
    a = _ent(database, "甲平台", "system")
    b = _ent(database, "甲平台旧", "system", aliases=["老甲"])
    c = _ent(database, "丙平台", "system")
    database.add_edge("entity", a, "entity", c, "co_occurs", source_id=1)
    database.add_edge("entity", b, "entity", c, "co_occurs", source_id=2)
    r = client.post("/api/ontology/merge", json={"a": str(a), "b": str(b)})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True and body["edges_to_merged"] == 0
    with database.connect() as conn:
        w = conn.execute(
            "SELECT weight FROM edges WHERE src_type='entity' AND src_id=?"
            " AND dst_type='entity' AND dst_id=? AND rel='co_occurs'", (a, c)).fetchone()[0]
    assert w == 2


def test_ontology_typefix_dry_run_endpoint(client, env):
    _, database, _ = env
    eid = _ent(database, "星云", "product")
    r = client.post("/api/ontology/typefix", json={"apply": False, "use_llm": True})
    assert r.status_code == 200
    body = r.json()
    assert body["proposed"] >= 1
    assert database.object_row("entity", eid)["type"] == "product", "dry-run 不改库"
    done = client.post("/api/ontology/typefix", json={"apply": True, "use_llm": True}).json()
    assert done["applied"] >= 1
    assert database.object_row("entity", eid)["type"] == "tool"


def test_ontology_candidate_reject_endpoint(client, env):
    _, database, _ = env
    a = _ent(database, "乙系统", "system", aliases=["乙系统旧"])
    b = _ent(database, "乙系统旧", "system")
    client.get("/api/ontology/candidates")
    with database.connect() as c:
        row = c.execute("SELECT id FROM merge_candidates WHERE a_id=? AND b_id=?",
                        (min(a, b), max(a, b))).fetchone()
    assert row is not None
    r = client.post(f"/api/ontology/candidates/{row['id']}/reject")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
