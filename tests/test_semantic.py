"""WP-07：语义检索（向量索引 / 余弦召回 / RRF 通道 / 敏感跳过）。"""
from __future__ import annotations

import pytest

import opc.config as config_mod
import opc.semantic as semantic
from opc.config import Config


class FakeEmbed:
    model = "fake-embed"

    def embed(self, texts):
        return [[1.0, 0.1] if "甲" in t else [0.1, 1.0] for t in texts]


@pytest.fixture()
def senv(env, monkeypatch):
    cfg, database, _ = env
    monkeypatch.setattr(semantic, "llm", lambda *a, **k: FakeEmbed())
    return cfg, database


def _note(database, title, summary="", body="", source_id=None):
    return database.put_note(source_id=source_id, kind="source", title=title,
                             slug=title, path=f"wiki/sources/{title}.md",
                             summary=summary, body_md=body, status="published")


def test_index_is_idempotent_and_rebuildable(senv):
    cfg, database = senv
    a = _note(database, "甲主题", "甲的摘要", "甲正文")
    b = _note(database, "乙主题", "乙的摘要", "乙正文")
    first = semantic.index_pending(database)
    assert first["embedded"] == 2 and first["total_vectors"] == 2
    again = semantic.index_pending(database)
    assert again["embedded"] == 0, "已嵌入的不重复调用"
    rebuild = semantic.index_pending(database, rebuild=True)
    assert rebuild["embedded"] == 2
    assert database.embedding_ids("fake-embed") == {a, b}


def test_search_ranks_by_cosine(senv):
    cfg, database = senv
    a = _note(database, "甲主题", "甲", "甲")
    _note(database, "乙主题", "乙", "乙")
    semantic.index_pending(database)
    hits = semantic.search(database, "甲", limit=2)
    assert hits and hits[0]["id"] == a
    assert hits[0]["type"] == "note" and hits[0]["score"] > 0.9


def test_sensitive_sources_are_not_embedded(senv):
    cfg, database = senv
    sid = database.upsert_source("note", "s-sensitive", "敏感来源",
                                 meta={"sensitive": True})
    _note(database, "敏感笔记", "不该进向量", "机密", source_id=sid)
    _note(database, "普通笔记", "可以进向量", "公开")
    res = semantic.index_pending(database)
    assert res["scanned"] == 1 and res["embedded"] == 1


def test_vector_channel_joins_rrf(senv, monkeypatch):
    cfg, database = senv
    a = _note(database, "甲主题", "甲", "甲")
    cfg._d.setdefault("search", {})["semantic"] = True
    monkeypatch.setattr(config_mod, "_cfg", cfg)
    monkeypatch.setattr(
        semantic, "search",
        lambda db, q, limit=6: [{"type": "note", "id": a, "title": "甲主题",
                                 "slug": "甲主题", "path": "x", "kind": "source",
                                 "snippet": "甲", "score": 0.99}])
    from opc.retrieval import search as hybrid_search

    hits = hybrid_search(database, "完全不字面匹配的问法", limit=5)
    assert hits and hits[0]["id"] == a, "向量命中应通过 RRF 进入结果"
    assert "vec" in hits[0]["channels"]


def test_vector_channel_failure_degrades(senv, monkeypatch):
    cfg, database = senv
    cfg._d.setdefault("search", {})["semantic"] = True
    monkeypatch.setattr(config_mod, "_cfg", cfg)

    def boom(*a, **k):
        raise RuntimeError("no key")

    monkeypatch.setattr(semantic, "search", boom)
    from opc.retrieval import search as hybrid_search

    assert hybrid_search(database, "任意查询", limit=5) == [], "语义失败不影响字面检索"


def test_semantic_enabled_honours_both_keys(monkeypatch):
    import opc.config as config_mod
    from opc.config import Config
    from opc.semantic import enabled

    monkeypatch.setattr(config_mod, "_cfg", Config({"search": {"semantic": True}}))
    assert enabled() is True

    monkeypatch.setattr(config_mod, "_cfg", Config({"search": {"embeddings": True}}))
    assert enabled() is True, "旧键 search.embeddings 必须继续生效"

    monkeypatch.setattr(config_mod, "_cfg", Config({"search": {}}))
    assert enabled() is False
