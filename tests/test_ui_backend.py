"""UI 后端契约：引用身份、图谱准入、任务优先级/取消/重试、观察事件、注意力中心。"""
from __future__ import annotations

from opc.actions import append_items
from opc.pipeline import build_context


def _source(database, name: str, title: str | None = None) -> int:
    return database.upsert_source("note", name, title or name, uri=f"inbox/{name}.md")


def test_citation_carries_type_and_ids(env):
    cfg, database, _ = env
    sid = _source(database, "s1")
    nid = database.put_note(source_id=sid, kind="source", title="S1", slug="s1",
                            path="wiki/sources/s1.md", summary="sum", body_md="body",
                            status="published")
    note_hit = {"type": "note", "id": nid, "title": "S1", "path": "wiki/sources/s1.md",
                "slug": "s1", "kind": "source", "body_md": "body"}
    chunk_hit = {"type": "chunk", "id": sid, "title": "S1", "path": "wiki/sources/s1.md",
                 "slug": "s1", "kind": "source", "snippet": "原文片段"}
    _, cites = build_context([note_hit, {"type": "chunk", "id": sid + 99,
                                         "title": "另一篇", "snippet": "x"}])
    assert cites[0]["note_id"] == nid and cites[0]["source_id"] is None
    # chunk 的 id 是 source_id，不是 note id
    _, cites2 = build_context([chunk_hit])
    assert cites2[0]["type"] == "chunk"
    assert cites2[0]["source_id"] == sid and cites2[0]["note_id"] is None
    assert cites2[0]["path"] == "wiki/sources/s1.md"


def test_graph_gates_single_source_concepts_and_keeps_source_path(env):
    cfg, database, _ = env
    s1 = _source(database, "g1", "来源一")
    s2 = _source(database, "g2", "来源二")
    c_multi = database.upsert_concept("multi", "多来源概念", "被两份来源覆盖")
    c_single = database.upsert_concept("single", "单来源概念", "只被一份来源覆盖")
    database.add_edge("source", s1, "concept", c_multi, "covers", source_id=s1)
    database.add_edge("source", s2, "concept", c_multi, "covers", source_id=s2)
    database.add_edge("source", s1, "concept", c_single, "covers", source_id=s1)
    database.put_note(source_id=s1, kind="source", title="来源一", slug="g1",
                      path="wiki/sources/g1.md", summary="", body_md="", status="published")

    g = database.graph(50)
    ids = {n["id"] for n in g["nodes"]}
    assert f"concept:{c_multi}" in ids
    assert f"concept:{c_single}" not in ids
    src = next(n for n in g["nodes"] if n["id"] == f"source:{s1}")
    assert src["path"] == "wiki/sources/g1.md"
    assert g["meta"]["gated_concepts"] >= 1


def test_job_priority_cancel_retry(env):
    cfg, database, _ = env
    low = database.enqueue("capture", {"text": "low"}, priority=1)
    high = database.enqueue("capture", {"text": "high"}, priority=10)
    claimed = database.claim_job()
    assert claimed["id"] == high
    assert database.claim_job()["id"] == low

    pending = database.enqueue("capture", {"text": "cancel me"}, priority=5)
    r = database.request_cancel(pending)
    assert r["ok"] and r["status"] == "cancelled"
    assert database.job(pending)["status"] == "cancelled"

    database.finish_job(low, False, "boom")
    assert database.job(low)["status"] == "failed"
    assert database.retry_job(low)["ok"]
    assert database.job(low)["status"] == "pending"


def test_ui_events_summary(env):
    cfg, database, _ = env
    database.add_ui_event("sess1", "app_open", {"standalone": False}, "test")
    database.add_ui_event("sess1", "ask_start", {"chars": 12}, "test")
    database.add_ui_event("sess2", "ask_done", {"tokens": 100}, "test")
    s = database.ui_event_summary(days=7)
    assert s["total"] == 3 and s["sessions"] == 2
    assert any(e["event"] == "ask_start" and e["n"] == 1 for e in s["events"])


def test_attention_aggregates(env):
    cfg, database, _ = env
    jid = database.enqueue("capture", {"text": "x"})
    database.finish_job(jid, False, "bad")
    sid = _source(database, "a1")
    database.put_note(source_id=sid, kind="source", title="草稿", slug="draft1",
                      path=None, summary="", body_md="", status="draft")
    database.add_conflict("entity", 1, "name", "旧", "新", sid)
    aid = database.add_annotation("需要确认")
    database.set_annotation_focus(aid, True)
    a = database.attention()
    assert a["counts"]["jobs_failed"] == 1
    assert a["counts"]["drafts"] == 1
    assert a["counts"]["conflicts"] == 1
    assert a["counts"]["annotations"] == 1
    assert a["total"] >= 4


def test_conversation_rename(env):
    cfg, database, _ = env
    cid = database.create_conversation("旧标题")
    database.rename_conversation(cid, "新标题")
    assert database.conversation(cid)["title"] == "新标题"
    mid = database.add_message(cid, "assistant", "")
    database.update_message(mid, "部分回答…", [{"n": 1, "title": "x"}])
    msgs = database.conversation(cid)["messages"]
    assert msgs[-1]["content"] == "部分回答…"
    assert msgs[-1]["citations"][0]["n"] == 1


def test_append_actions_dedup(env):
    cfg, database, vault = env
    r1 = append_items(["- [ ] 跟进甲", "跟进乙", "跟进甲"])
    assert r1["added"] == 2
    r2 = append_items(["跟进甲"])
    assert r2["added"] == 0
    page = vault / "行动项.md"
    assert page.exists()
    text = page.read_text(encoding="utf-8")
    assert text.count("跟进甲") == 1 and "跟进乙" in text


def test_save_answer_writes_synthetic(env):
    from opc.pipeline import save_synthetic

    cfg, database, vault = env
    saved = save_synthetic("测试问题", "测试回答", [
        {"n": 1, "title": "来源甲", "slug": "src-a", "path": "wiki/sources/src-a.md"},
    ], database)
    assert saved["path"].startswith("wiki/synthetic/")
    page = vault / saved["path"]
    assert page.exists()
    text = page.read_text(encoding="utf-8")
    assert "测试回答" in text and "[[sources/src-a|来源甲]]" in text


def _client(env_db, monkeypatch):
    from fastapi.testclient import TestClient

    from opc.hub import app as hub

    monkeypatch.setattr(hub, "db", lambda: env_db)
    return TestClient(hub.app)


def test_telemetry_drops_free_text(env, monkeypatch):
    """FR-708：非枚举字符串不入库，只留长度（不存文件名/正文/查询词）。"""
    _, database, _ = env
    client = _client(database, monkeypatch)
    r = client.post("/api/telemetry", json={"events": [{
        "event": "ask_error",
        "props": {"message": "C:\\Users\\me\\secret.md 打不开", "chars": 12,
                  "action": "copy"},
    }]})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    with database.connect() as c:
        row = c.execute("SELECT event, props FROM ui_events").fetchone()
    import json as _json

    props = _json.loads(row["props"])
    assert "message" not in props and props["message_len"] > 0
    assert props["chars"] == 12 and props["action"] == "copy"
    assert "secret.md" not in row["props"]


def test_telemetry_can_be_disabled_server_side(env, monkeypatch):
    import opc.config as config_mod
    from opc.config import Config

    _, database, _ = env
    monkeypatch.setattr(config_mod, "_cfg", Config({"policy": {"telemetry_enabled": False}}))
    client = _client(database, monkeypatch)
    r = client.post("/api/telemetry", json={"events": [{"event": "app_open", "props": {}}]})
    assert r.json() == {"accepted": 0, "disabled": True}


def test_upload_enforces_size_and_type(env, monkeypatch, tmp_path):
    """FR-204/809：8MB 上限 + 可读文本类型白名单。"""
    cfg, database, _ = env
    cfg._d["data"] = {"dir": str(tmp_path)}
    client = _client(database, monkeypatch)

    r = client.post("/api/upload", files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 415
    big = b"a" * (8 * 1024 * 1024 + 1)
    r = client.post("/api/upload", files={"file": ("big.md", big, "text/markdown")})
    assert r.status_code == 413
    r = client.post("/api/upload", files={"file": ("ok.md", "内容".encode(), "text/markdown")})
    assert r.status_code == 200 and r.json()["bytes"] > 0
