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
    assert lan.status_code == 403

    def with_token() -> Request:
        return Request({
            "type": "http", "method": "GET", "scheme": "http", "path": "/api/health",
            "query_string": b"", "client": ("192.168.1.9", 12345),
            "server": ("192.168.1.5", 5370),
            "headers": [(b"host", b"192.168.1.5:5370"), (b"x-opc-token", b"s3cret")],
        })

    assert asyncio.run(token_guard(with_token(), _next)).status_code == 200


def test_lan_without_token_is_rejected(monkeypatch):
    """没有配置令牌时不能对局域网裸奔（旧行为：任何 IP 都放行）。"""
    from opc.hub.app import token_guard

    monkeypatch.delenv("OPC_TOKEN", raising=False)
    monkeypatch.setattr(config_mod, "_cfg", Config({"security": {"token": ""}}))
    resp = asyncio.run(token_guard(_req("192.168.1.5:5370", client="192.168.1.9"), _next))
    assert resp.status_code == 403


def test_reverse_proxy_does_not_bypass_token(monkeypatch):
    """经本机反代进来的请求不能被当成本机（尊重 trust_proxy 开关）。"""
    from opc.hub.app import token_guard

    monkeypatch.setenv("OPC_TOKEN", "s3cret")
    monkeypatch.setattr(config_mod, "_cfg",
                        Config({"security": {"token": "s3cret", "trust_proxy": False}}))

    def proxied(host: str = "127.0.0.1:5370") -> Request:
        return Request({
            "type": "http", "method": "GET", "scheme": "http", "path": "/api/health",
            "query_string": b"", "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 5370),
            "headers": [(b"host", host.encode()), (b"x-forwarded-for", b"192.168.1.9")],
        })

    assert asyncio.run(token_guard(proxied(), _next)).status_code == 403

    # 显式信任代理 + 真实来源是本机：才放行
    monkeypatch.setattr(config_mod, "_cfg",
                        Config({"security": {"token": "s3cret", "trust_proxy": True}}))
    assert asyncio.run(token_guard(proxied(), _next)).status_code == 403  # XFF 是 LAN
    local = Request({
        "type": "http", "method": "GET", "scheme": "http", "path": "/api/health",
        "query_string": b"", "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 5370),
        "headers": [(b"host", b"127.0.0.1:5370"), (b"x-forwarded-for", b"127.0.0.1")],
    })
    assert asyncio.run(token_guard(local, _next)).status_code == 200


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


def test_draft_can_be_published_by_recapture(env):
    """草稿不能被"内容未变"永久卡住：显式再捕获（publish=True）应补成正式页。"""
    cfg, database, _ = env
    from opc.capture import capture

    a = capture(kind="note", external_id="d-2", title="草稿二", text="同一段内容",
                publish=False, database=database)
    b = capture(kind="note", external_id="d-2", title="草稿二", text="同一段内容",
                publish=True, database=database)
    assert b.get("skipped") is not True
    with database.connect() as c:
        row = c.execute("SELECT status FROM notes WHERE source_id=?",
                        (a["source_id"],)).fetchone()
    assert row["status"] == "published"


def test_rebuild_keeps_content_hash_for_idempotent_capture(env, tmp_path):
    """重建后同内容再捕获应 skipped（I2/I3）：content_hash 随页面 frontmatter 恢复。"""
    cfg, database, vault = env
    from opc.capture import capture
    from opc.db import Database
    from opc.rebuild import build_from_vault
    from opc.util import sha
    from opc.vault import Vault

    text = "重建正文内容"
    r = capture(kind="note", external_id="rh-1", title="重建来源", text=text,
                database=database)
    slug = r["path"].rsplit("/", 1)[-1].removesuffix(".md")
    page = (vault / r["path"]).read_text(encoding="utf-8")
    assert "content_hash" in page, "来源页必须持久化内容指纹"

    build_from_vault(Vault(), db_path=tmp_path / "rb.db")
    rebuilt = Database(tmp_path / "rb.db")
    with rebuilt.connect() as c:
        row = c.execute("SELECT content_hash FROM sources WHERE external_id=?",
                        (slug,)).fetchone()
    assert row and row["content_hash"] == sha(text)
    again = capture(kind="note", external_id=slug, title="重建来源", text=text,
                    database=rebuilt)
    assert again.get("skipped") is True


def test_file_external_id_is_stable_across_edits(env, tmp_path, monkeypatch):
    """FR-209：文件身份只用路径，改动内容不产生新来源。"""
    from opc.capture import capture as real_capture
    from opc.connectors import files as files_mod

    cfg, database, _ = env
    monkeypatch.setattr(files_mod, "capture",
                        lambda **kw: real_capture(database=database, **kw))
    p = tmp_path / "笔记.md"
    p.write_text("第一版内容", encoding="utf-8")
    conn = files_mod.FilesConnector({"enabled": True, "watch_dirs": [str(tmp_path)],
                                     "extensions": [".md"]})
    r1 = conn.ingest(p)
    p.write_text("第二版内容，改了很多", encoding="utf-8")
    r2 = conn.ingest(p)
    assert r1["source_id"] == r2["source_id"], "同一路径必须是同一来源"
    with database.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM sources WHERE kind='file'").fetchone()[0]
    assert n == 1


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
