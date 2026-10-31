"""全面审查（REVIEW-FINDINGS.md）发现的回归测试。

- test_put_chunks_no_fts_orphans：P0-3（put_chunks/put_document 删 chunks 后 FTS 孤儿行）
- test_claim_job_atomic_claim：P1-4（claim_job 无认领守卫）
- test_capture_draft_second_call_skips：P1-5（publish=False 的草稿每次重提炼）
- test_refresh_counts_only_source_edges：P0-4 语义基准（note 边 source_id=NULL 不计入）
"""
from __future__ import annotations

import threading

import pytest

import opc.config as config_mod
import opc.pipeline as pipeline_mod
from opc.capture import capture
from opc.config import Config
from opc.db import Database
from opc.pipeline import distill_source


class StubLLM:
    model = "stub-model"

    def chat_json(self, *args, **kwargs):
        return {
            "title": "测试标题",
            "summary": "摘要",
            "key_points": ["要点"],
            "claims": [],
            "entities": [{"name": "测试系统", "type": "system", "description": "", "aliases": []}],
            "concepts": [],
            "actions": [],
            "tags": [],
            "open_questions": [],
        }

    def chat(self, *args, **kwargs):
        return "回答"


@pytest.fixture()
def renv(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "raw").mkdir()
    (vault / "wiki/_index.md").write_text("# Wiki Index\n\n## 实体\n\n## 来源\n", encoding="utf-8")
    cfg = Config({
        "vault": {"path": str(vault), "inbox": "inbox", "wiki": "wiki",
                  "write_raw": True, "auto_publish": True, "protect_existing": True},
        "ingest": {"index_enabled": True, "max_entities_per_doc": 24},
        "llm": {"max_input_chars": 24000},
    })
    monkeypatch.setattr(config_mod, "_cfg", cfg)
    monkeypatch.setattr(pipeline_mod, "llm", lambda *a, **k: StubLLM())
    database = Database(tmp_path / "t.db")
    import opc.db as db_mod

    monkeypatch.setattr(db_mod, "_db", database)  # capture() 默认走全局 db()
    return cfg, database, vault


def test_put_chunks_no_fts_orphans(renv):
    _, database, _ = renv
    sid = database.upsert_source("note", "x1", "标题甲", content_hash="h1")
    doc = database.put_document(sid, "旧内容关键词甲乙丙丁")
    database.put_chunks(doc, sid, ["旧内容关键词甲乙丙丁"])
    # 重提炼：覆盖同一 document，内容变化（会议半场→终稿的核心场景）
    database.put_document(sid, "新内容关键词丁戊己庚")
    database.put_chunks(doc, sid, ["新内容关键词丁戊己庚"])

    with database.connect() as c:
        n_chunks = c.execute("SELECT COUNT(*) FROM chunks WHERE document_id=?", (doc,)).fetchone()[0]
        n_fts = c.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        orphan = c.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '甲乙丙'").fetchall()
    assert n_chunks == 1
    assert n_fts == n_chunks, "FTS 行数必须与 chunks 对齐，不允许孤儿行"
    assert not orphan, "旧内容不应残留在 FTS 索引里"


def test_claim_job_atomic_claim(renv):
    _, database, _ = renv
    jid = database.enqueue("capture", {"x": 1})

    got = []

    def work(_store):
        d = Database(str(database.path))
        got.append(d.claim_job())

    t1 = threading.Thread(target=work, args=("a",))
    t2 = threading.Thread(target=work, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()

    claimed = [j for j in got if j]
    assert len(claimed) == 1, "同一任务只能被认领一次"
    assert claimed[0]["id"] == jid


def test_capture_draft_second_call_skips(renv):
    _, database, _ = renv
    text = "草稿内容一句话，用于验证幂等。"
    r1 = capture(kind="note", external_id="draft-1", title="草稿标题", text=text, publish=False,
                 database=database)
    r2 = capture(kind="note", external_id="draft-1", title="草稿标题", text=text, publish=False,
                 database=database)
    assert not r1.get("skipped")
    assert r2.get("skipped"), "同内容草稿第二次捕获必须跳过（值守目录注水修复）"


def test_refresh_counts_only_source_edges(renv):
    _, database, _ = renv
    sid = database.upsert_source("note", "s1", "标题", content_hash="h")
    nid = database.put_note(source_id=sid, kind="source", title="t", slug="t",
                            path="wiki/t.md", summary="", body_md="", status="published")
    eid = database.upsert_entity("k1", "实体甲", "person")
    database.add_edge("source", sid, "entity", eid, "mentions", source_id=sid)
    database.add_edge("note", nid, "entity", eid, "mentions")  # note 边，source_id=None
    n = database.refresh_entity_sources(eid, 2)
    assert n == 1, "note→entity 的 mentions 边不应计入来源数（其 source_id 为空）"
