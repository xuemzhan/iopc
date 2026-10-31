"""REVIEW-FINDINGS 修复回归：P0-3/P0-4/P1-1/P1-2/P1-4/P1-5 + Host 校验。"""
from __future__ import annotations

import asyncio
import json

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

import opc.config as config_mod
import opc.pipeline as pipeline_mod
from opc.config import Config
from opc.db import Database
from opc.pipeline import distill_source
from opc.policy import mask_many
from opc.util import normalize_key


def _req(host: str, path: str = "/api/health", client: str = "127.0.0.1") -> Request:
    return Request({
        "type": "http", "method": "GET", "scheme": "http", "path": path,
        "query_string": b"", "headers": [(b"host", host.encode())],
        "client": (client, 12345), "server": ("127.0.0.1", 5370),
    })


async def _next(_request):
    return JSONResponse({"ok": True})


def test_host_guard_blocks_dns_rebinding(monkeypatch):
    from opc.hub.app import token_guard

    monkeypatch.delenv("OPC_TOKEN", raising=False)
    cfg = Config({"security": {"token": ""}})
    monkeypatch.setattr(config_mod, "_cfg", cfg)

    assert asyncio.run(token_guard(_req("127.0.0.1:5370"), _next)).status_code == 200
    assert asyncio.run(token_guard(_req("localhost:5370"), _next)).status_code == 200
    assert asyncio.run(token_guard(_req("evil.example.com"), _next)).status_code == 403


def test_token_required_from_non_loopback(monkeypatch):
    from opc.hub.app import token_guard

    monkeypatch.setenv("OPC_TOKEN", "s3cret")
    cfg = Config({"security": {"token": "s3cret"}})
    monkeypatch.setattr(config_mod, "_cfg", cfg)

    lan = asyncio.run(token_guard(_req("192.168.1.5:5370", client="192.168.1.9"), _next))
    assert lan.status_code == 401

    def with_token() -> Request:
        return Request({
            "type": "http", "method": "GET", "scheme": "http", "path": "/api/health",
            "query_string": b"", "client": ("192.168.1.9", 12345),
            "server": ("192.168.1.5", 5370),
            "headers": [(b"host", b"192.168.1.5:5370"), (b"x-opc-token", b"s3cret")],
        })

    assert asyncio.run(token_guard(with_token(), _next)).status_code == 200


def test_put_chunks_does_not_leak_fts(tmp_path):
    db = Database(tmp_path / "t.db")
    sid = db.upsert_source("note", "x", "X")
    doc = db.put_document(sid, "第一版")
    db.put_chunks(doc, sid, ["第一版"])
    db.put_chunks(doc, sid, ["第二版"])
    with db.connect() as c:
        chunks = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        fts = c.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    assert chunks == fts == 1


def test_source_count_ignores_note_edges(tmp_path):
    db = Database(tmp_path / "t.db")
    eid = db.upsert_entity("e", "实体", "system")
    db.add_edge("note", 1, "entity", eid, "mentions", source_id=None)
    db.add_edge("source", 1, "entity", eid, "mentions", source_id=1)
    assert db.refresh_entity_sources(eid, 2) == 1


def test_mask_tokens_shared_across_fields(monkeypatch):
    cfg = Config({"policy": {"sensitive_enabled": True, "keywords": ["示例机构"]}})
    monkeypatch.setattr(config_mod, "_cfg", cfg)
    masked, mapping = mask_many(["示例机构开会", "示例机构发文", "示例机构"])
    tokens = [t for t in mapping if t.startswith("ORG_")]
    assert len(tokens) == 1, mapping
    token = tokens[0]
    assert mapping[token] == "示例机构"
    assert all(token in m for m in masked)


def test_title_tokens_are_unmasked(env, monkeypatch):
    cfg, database, vault = env
    cfg._d["policy"] = {"sensitive_enabled": True, "keywords": ["示例机构"], "mask_entities": True}
    payload = {
        "title": "ORG_1 的会议", "summary": "ORG_1 开会", "key_points": [],
        "claims": [], "entities": [], "concepts": [], "actions": [], "tags": [],
        "open_questions": [],
    }

    class Spy:
        model = "stub"

        def chat_json(self, *a, **k):
            return payload

    monkeypatch.setattr(pipeline_mod, "llm", lambda *a, **k: Spy())
    from opc.capture import capture

    res = capture(kind="note", external_id="t-1", title="示例机构会议",
                  text="示例机构召开会议。", database=database)
    assert "示例机构" in res["title"], res["title"]
    assert "ORG_1" not in res["title"]


def test_draft_capture_is_skipped_on_repeat(env):
    cfg, database, _ = env
    from opc.capture import capture

    a = capture(kind="note", external_id="d-1", title="草稿", text="同一段内容",
                publish=False, database=database)
    b = capture(kind="note", external_id="d-1", title="草稿", text="同一段内容",
                publish=False, database=database)
    assert a.get("skipped") is False and b.get("skipped") is True


def test_claim_job_is_atomic_and_requeues_stale(tmp_path):
    db = Database(tmp_path / "t.db")
    j1 = db.enqueue("capture", {})
    j2 = db.enqueue("capture", {})
    a = db.claim_job()
    b = db.claim_job()
    assert {a["id"], b["id"]} == {j1, j2}
    assert db.claim_job() is None

    with db.tx() as c:
        c.execute("UPDATE jobs SET status='running', started_at='2000-01-01T00:00:00+0800'"
                  " WHERE id=?", (j1,))
    assert db.requeue_stale(30) == 1
    with db.connect() as c:
        row = c.execute("SELECT status FROM jobs WHERE id=?", (j1,)).fetchone()
    assert row["status"] == "pending"
