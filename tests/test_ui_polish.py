"""WP-10：UI 收尾的后端契约（相关笔记 / 预算 / 反馈聚合）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from opc.hub import app as hub


@pytest.fixture()
def client(env, monkeypatch):
    _, database, _ = env
    monkeypatch.setattr(hub, "db", lambda: database)
    return TestClient(hub.app), database


def _note(database, title, source_id=None):
    return database.put_note(source_id=source_id, kind="source", title=title,
                             slug=title, path=f"wiki/sources/{title}.md",
                             summary=title, body_md=title, status="published")


def test_related_notes_share_entities(client):
    c, database = client
    a = _note(database, "笔记甲")
    b = _note(database, "笔记乙")
    cnote = _note(database, "笔记丙")
    e = database.upsert_entity("共享实体", "共享实体", "system")
    database.add_edge("note", a, "entity", e, "mentions", weight=1.0)
    database.add_edge("note", b, "entity", e, "mentions", weight=1.0)
    database.add_edge("note", cnote, "entity", e, "mentions", weight=1.0)
    r = c.get(f"/api/related?note_id={a}&limit=5")
    assert r.status_code == 200
    ids = [x["id"] for x in r.json()["items"]]
    assert b in ids and cnote in ids and a not in ids


def test_pulse_reports_budget(client):
    c, _ = client
    body = c.get("/api/pulse").json()
    assert "budget" in body
    assert {"used", "max", "ratio", "exhausted"} <= set(body["budget"])


def test_feedback_is_aggregated(client):
    c, database = client
    database.add_ui_event("s1", "feedback", {"rating": "up"}, "feedback")
    database.add_ui_event("s1", "feedback", {"rating": "up"}, "feedback")
    database.add_ui_event("s2", "feedback", {"rating": "down"}, "feedback")
    body = c.get("/api/telemetry/summary?days=7").json()
    assert body["feedback"].get("up") == 2 and body["feedback"].get("down") == 1
