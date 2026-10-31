"""语义检索（WP-07）：笔记向量化 + 余弦召回，作为 RRF 的一个通道。

设计取舍：
- 只嵌「已发布、且来源不敏感」的笔记：敏感来源不送云端 embedding（隐私优先）。
- 向量存 float32 blob（`embeddings` 表），按 model 区分；换模型需重建。
- 查询侧每次一次 embedding 调用；向量全量载入内存做余弦（10 万级前够用，见 L6）。
- 任何失败（缺 key/网络/无向量）都安静退化为纯 FTS，不影响问答。
"""
from __future__ import annotations

import math
from typing import Any

from .config import cfg
from .db import Database, db
from .llm import llm


def _note_text(title: str, summary: str, body: str) -> str:
    body = (body or "").strip()
    return f"{title}\n{summary or ''}\n{body[:600]}".strip()


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = math.fsum(x * y for x, y in zip(a, b))
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _note_rows(database: Database, limit: int | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT n.id, n.title, COALESCE(n.summary,'') AS summary,"
        " COALESCE(n.body_md,'') AS body_md"
        " FROM notes n LEFT JOIN sources s ON s.id = n.source_id"
        " WHERE n.status='published' AND n.kind IN ('source','synthetic')"
        " AND COALESCE(json_extract(s.meta, '$.sensitive'), 0) = 0"
        " ORDER BY n.id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    with database.connect() as c:
        return [dict(r) for r in c.execute(sql).fetchall()]


def enabled() -> bool:
    """语义通道是否开启：优先 search.semantic，兼容旧键 search.embeddings。"""
    from .config import cfg

    return bool(cfg().get("search.semantic", cfg().get("search.embeddings", False)))


def index_pending(database: Database | None = None, *, limit: int | None = None,
                  rebuild: bool = False, batch: int = 16,
                  on_progress=None) -> dict[str, Any]:
    """给缺失向量的笔记补 embedding；`rebuild=True` 时全部重嵌。"""
    database = database or db()
    model = llm("embed").model
    rows = _note_rows(database, limit=limit)
    have = set() if rebuild else database.embedding_ids(model)
    todo = [r for r in rows if int(r["id"]) not in have]
    embedded = 0
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        texts = [_note_text(r["title"], r["summary"], r["body_md"]) for r in chunk]
        vectors = llm("embed").embed(texts)
        if len(vectors) != len(chunk):
            raise RuntimeError("embedding 返回条数与输入不一致")
        for r, vec in zip(chunk, vectors):
            database.put_embedding("note", int(r["id"]), model, vec)
            embedded += 1
        if on_progress:
            pct = int(embedded / max(len(todo), 1) * 100)
            on_progress(pct, f"已嵌入 {embedded}/{len(todo)}")
    total = len(database.embedding_ids(model))
    return {"model": model, "scanned": len(rows), "pending": len(todo),
            "embedded": embedded, "total_vectors": total, "rebuild": rebuild}


def search(database: Database | None = None, query: str = "", limit: int = 8,
           min_score: float = 0.0) -> list[dict[str, Any]]:
    """向量召回：返回与 db.search 相同形状的 note 命中（score=余弦）。"""
    database = database or db()
    q = (query or "").strip()
    if not q:
        return []
    model = llm("embed").model
    max_vec = int(cfg().get("search.semantic_max_vectors", 50000))
    vectors = database.embeddings(model)[:max_vec]
    if not vectors:
        return []
    qvec = llm("embed").embed([q])[0]
    scored = [(t["target_id"], _cosine(qvec, t["vec"])) for t in vectors]
    scored = [x for x in scored if x[1] > min_score]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[: max(1, int(limit))]
    if not top:
        return []
    ids = [tid for tid, _ in top]
    marks = ",".join("?" * len(ids))
    with database.connect() as c:
        rows = {int(r["id"]): dict(r) for r in c.execute(
            f"SELECT id, title, slug, path, kind, COALESCE(summary,'') AS summary"
            f" FROM notes WHERE id IN ({marks})", ids).fetchall()}
    out: list[dict[str, Any]] = []
    for tid, score in top:
        row = rows.get(tid)
        if not row:
            continue
        out.append({
            "type": "note", "id": tid, "title": row["title"], "slug": row["slug"],
            "path": row["path"], "kind": row["kind"],
            "snippet": (row["summary"] or "")[:200], "score": round(score, 6),
        })
    return out
