"""FR-408：问答作用域（按来源 / 类型 / 时间窗过滤命中）。"""
from __future__ import annotations

import pytest

import opc.pipeline as pipeline_mod


@pytest.fixture()
def scope_env(env, monkeypatch):
    cfg, database, _ = env
    s1 = database.upsert_source("meeting", "m-1", "会议一", occurred_at="2026-10-01T10:00:00")
    s2 = database.upsert_source("web", "w-1", "网页一", occurred_at="2026-10-20T10:00:00")
    n1 = database.put_note(source_id=s1, kind="source", title="会议一纪要", slug="m-1",
                           path="wiki/sources/m-1.md", summary="会议内容", body_md="会议内容",
                           status="published")
    n2 = database.put_note(source_id=s2, kind="source", title="网页一文", slug="w-1",
                           path="wiki/sources/w-1.md", summary="网页内容", body_md="网页内容",
                           status="published")
    return database, s1, s2, n1, n2


def test_scope_filter_by_source_kind_and_date(scope_env):
    database, s1, s2, n1, n2 = scope_env
    hits = [{"type": "note", "id": n1}, {"type": "note", "id": n2}]
    assert [h["id"] for h in pipeline_mod._scope_hits(database, hits, {"source_id": s1})] == [n1]
    assert [h["id"] for h in pipeline_mod._scope_hits(database, hits, {"kind": "web"})] == [n2]
    assert [h["id"] for h in pipeline_mod._scope_hits(
        database, hits, {"since": "2026-10-10"})] == [n2]
    assert [h["id"] for h in pipeline_mod._scope_hits(
        database, hits, {"until": "2026-10-10"})] == [n1]
    assert len(pipeline_mod._scope_hits(database, hits, {})) == 2


def test_prepare_ask_returns_empty_when_scope_excludes_all(scope_env, monkeypatch):
    database, s1, s2, n1, n2 = scope_env

    def fake_search(db, q, limit=6, **kw):
        return [{"type": "note", "id": n1, "title": "会议一纪要", "slug": "m-1",
                 "path": "wiki/sources/m-1.md", "kind": "source", "score": 1.0},
                {"type": "note", "id": n2, "title": "网页一文", "slug": "w-1",
                 "path": "wiki/sources/w-1.md", "kind": "source", "score": 0.9}]

    import opc.retrieval as retrieval_mod

    monkeypatch.setattr(retrieval_mod, "search", fake_search)
    prepared = pipeline_mod.prepare_ask("讲了什么", database=database,
                                        scope={"source_id": 999999})
    assert prepared["empty"] is True

    prepared2 = pipeline_mod.prepare_ask("讲了什么", database=database,
                                         scope={"kind": "web"})
    assert prepared2["empty"] is False
    tail = prepared2["messages"][-1]["content"]
    assert "网页一文" in tail and "会议一纪要" not in tail
