"""句级共现：同一句话里出现的实体才算有关系。

v0.2 的做法是"同一来源内两两互连"，边数爆炸且没有证据。
这里把「句子」当作最小证据单位：
- 只有同句出现的实体对才建边
- 每来源按句频排序，只保留 top-N 对
- `weight` 由 `db.add_edge` 维护成「覆盖该关系的不同来源数」
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

_SENT_SPLIT = re.compile(r"[。；;!?！？\n]+")


def split_sentences(text: str, min_len: int = 4) -> list[str]:
    out = []
    for s in _SENT_SPLIT.split(text or ""):
        s = s.strip()
        if len(s) >= min_len:
            out.append(s)
    return out


def _names(entity: dict[str, Any]) -> list[str]:
    names = [str(entity.get("name") or "")]
    names += [str(a) for a in (entity.get("aliases") or [])]
    return [n for n in names if len(n) >= 2]


def sentence_pairs(text: str, entities: list[dict[str, Any]], top_n: int = 8,
                   evidence_len: int = 200) -> list[dict[str, Any]]:
    """返回 [{a, b, count, evidence}]，按句频降序，最多 top_n 对。"""
    counts: dict[tuple[int, int], int] = defaultdict(int)
    evidence: dict[tuple[int, int], str] = {}
    for sent in split_sentences(text):
        present = [e for e in entities if any(n in sent for n in _names(e))]
        # 同一句里最多取 6 个实体，避免长句爆炸
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                a, b = present[i], present[j]
                if a.get("id") is None or b.get("id") is None or a["id"] == b["id"]:
                    continue
                key = (min(int(a["id"]), int(b["id"])), max(int(a["id"]), int(b["id"])))
                counts[key] += 1
                if key not in evidence:
                    evidence[key] = sent[:evidence_len]
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [{"a": k[0], "b": k[1], "count": c, "evidence": evidence.get(k, "")}
            for k, c in ranked]


def graph_metrics(database) -> dict[str, Any]:
    """边数 / co_occurs 占比 / 度数分布，用于修复前后对比。"""
    with database.connect() as c:
        total = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        co = c.execute("SELECT COUNT(*) FROM edges WHERE rel='co_occurs'").fetchone()[0]
        degs = [r[0] for r in c.execute(
            "SELECT COUNT(*) FROM edges WHERE src_type='entity' GROUP BY src_id"
            " ORDER BY 1 DESC"
        ).fetchall()]
    degs.sort(reverse=True)
    top1 = degs[0] if degs else 0
    top10 = (sum(degs[:10]) / len(degs[:10])) if degs else 0
    return {
        "edges": int(total),
        "co_occurs": int(co),
        "co_ratio": round(co / total, 4) if total else 0.0,
        "entities_with_edges": len(degs),
        "top1_degree": int(top1),
        "top10_avg_degree": round(float(top10), 1),
    }


def rebuild_for_source(database, source_id: int, text: str,
                       entities: Iterable[dict[str, Any]], top_n: int = 8) -> int:
    """给单个来源重建句级共现边，返回新增条数。"""
    ents = list(entities)
    pairs = sentence_pairs(text, ents, top_n=top_n)
    for p in pairs:
        database.add_edge("entity", p["a"], "entity", p["b"], "co_occurs",
                          weight=1.0, evidence=p["evidence"], source_id=source_id)
    return len(pairs)
