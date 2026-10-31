"""混合检索 v2：多通道召回 + RRF 融合。

三个通道各有盲区：
- 别名通道（实体/概念名命中）→ 懂"说的是哪个东西"，但只覆盖图谱里有的
- trigram FTS → 懂中文子串，但怕长句和口语
- LIKE → 兜住 2 字短词与 FTS 排不掉的边角

单路排序会把某一通道的偏好强加给用户，所以这里用 Reciprocal Rank Fusion
把三路的**名次**融合（而不是分数），再叠加查询扩展的多组检索词。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .config import cfg
from .db import _terms

if TYPE_CHECKING:  # pragma: no cover
    from .db import Database

RRF_K = 60
CHANNEL_WEIGHT = {"alias": 1.2, "vec": 1.1, "fts": 1.0, "like": 0.5}
META_HINTS = ("日报", "周报", "复盘", "体检", "行动项", "lint")


def _title_terms(q: str) -> list[str]:
    return [t for t in _terms(q) if len(t) >= 2][:8]


def _fuse(channels: dict[str, list[dict[str, Any]]], limit: int,
          q: str = "", qterms: list[str] | None = None) -> list[dict[str, Any]]:
    scores: dict[tuple[str, int], float] = {}
    payload: dict[tuple[str, int], dict[str, Any]] = {}
    hit_channels: dict[tuple[str, int], list[str]] = {}
    qterms = qterms if qterms is not None else _title_terms(q)
    wants_meta = any(h in q for h in META_HINTS)

    for name, hits in channels.items():
        w = CHANNEL_WEIGHT.get(name, 1.0)
        for rank, h in enumerate(hits):
            key = (str(h.get("type", "note")), int(h["id"]))
            scores[key] = scores.get(key, 0.0) + w / (RRF_K + rank + 1)
            hit_channels.setdefault(key, []).append(name)
            cur = payload.get(key)
            # 笔记优先：笔记有提炼后的结构化摘要，比原文块更适合喂给 LLM
            if cur is None or (cur.get("type") != "note" and h.get("type") == "note") \
                    or (len(h.get("snippet") or "") > len(cur.get("snippet") or "")
                        and cur.get("type") == h.get("type")):
                payload[key] = dict(h)

    # 标题覆盖度：查询词真的出现在标题里，几乎一定是用户要的那页
    for key, sc in list(scores.items()):
        item = payload[key]
        title = str(item.get("title") or "")
        if qterms and title:
            cov = sum(1 for t in qterms if t in title) / len(qterms)
            sc *= 1 + 1.2 * cov
            if cov == 1.0:
                sc *= 1.15
        # 综述类页面会命中一切词，除非用户就是在问它，否则压一压
        kind = str(item.get("kind") or "")
        title_kind = kind if kind in ("synthetic",) else ""
        is_meta = title_kind == "synthetic" or title.startswith(("日报 ", "周复盘 ", "知识库体检"))
        if is_meta and not wants_meta:
            sc *= 0.55
        scores[key] = sc

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    out = []
    for key, sc in ranked:
        item = payload[key]
        item["score"] = round(sc, 6)
        item["channels"] = hit_channels.get(key, [])
        out.append(item)
    return out


def search(database: "Database", query: str, limit: int = 8, *,
           expansions: list[str] | None = None, use_alias: bool | None = None) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    use_alias = cfg().get("search.alias_boost", True) if use_alias is None else use_alias
    queries = [q] + [e for e in (expansions or []) if e and e != q]
    wide = max(limit * 3, 12)

    channels: dict[str, list[dict[str, Any]]] = {"fts": [], "like": []}
    if use_alias:
        channels["alias"] = database._alias_hits(q, max(2, limit // 2))
    from .semantic import enabled as _semantic_enabled

    if _semantic_enabled():
        try:
            from .semantic import search as vec_search

            channels["vec"] = vec_search(database, q, max(limit, 6))
        except Exception:  # noqa: BLE001 — 语义通道失败不影响字面检索
            channels["vec"] = []

    for i, qq in enumerate(queries):
        terms = _terms(qq)
        long_terms = [t for t in terms if len(t) >= 3][:8]
        if long_terms:
            fts_q = " OR ".join(f'"{t}"' for t in long_terms)
            channels["fts"] += database._fts(fts_q, wide)
        # LIKE 是全文扫描：只给原查询兜底，且只在 FTS 召回不足时跑
        if i == 0 and (not long_terms or len(channels["fts"]) < 3):
            like_terms = [t for t in terms if len(t) >= 2][:6] or [qq]
            channels["like"] += database._like(like_terms, wide)

    return _fuse(channels, limit, q=q)


def search_legacy(database: "Database", query: str, limit: int = 8) -> list[dict[str, Any]]:
    """v0.2 的串行版本，保留用于对照评测。"""
    q = (query or "").strip()
    if not q:
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    if cfg().get("search.alias_boost", True):
        for h in database._alias_hits(q, max(2, limit // 2)):
            key = (h["type"], int(h["id"]))
            if key not in seen:
                seen.add(key)
                out.append(h)
    terms = _terms(q)
    long_terms = [t for t in terms if len(t) >= 3][:8]
    if long_terms and len(out) < limit:
        for h in database._fts(" OR ".join(f'"{t}"' for t in long_terms), limit):
            key = (h["type"], int(h["id"]))
            if key not in seen:
                seen.add(key)
                out.append(h)
    if len(out) < limit:
        like_terms = [t for t in terms if len(t) >= 2][:6] or [q]
        for h in database._like(like_terms, limit - len(out)):
            key = (h["type"], int(h["id"]))
            if key not in seen:
                seen.add(key)
                out.append(h)
    return out[:limit]
