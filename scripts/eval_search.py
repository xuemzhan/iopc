"""检索质量评测：串行(v0.2) vs RRF(v0.3) vs RRF+查询扩展。

用法： python scripts/eval_search.py [--no-expand]
指标： Recall@1 / Recall@3 / MRR（命中 = 结果标题包含任一期望片段）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opc.db import db  # noqa: E402
from opc.query_expand import expand  # noqa: E402
from opc.retrieval import search as search_v2  # noqa: E402
from opc.retrieval import search_legacy  # noqa: E402

QUERIES: list[tuple[str, list[str]]] = [
    ("总体能力", ["总体能力"]),
    ("总部能力是管理向下的延伸吗", ["总体能力"]),
    ("软实力是谁提出的", ["总体能力"]),
    ("架构 决策", ["架构决定"]),
    ("OPC 中枢是怎么做的", ["架构决定"]),
    ("只读采集", ["架构决定", "工具栈"]),
    ("WorkBuddy 权限边界", ["工具栈"]),
    ("李示例负责验收", ["工具栈"]),
    ("今天日报说了什么", ["日报"]),
    ("张示例讲了什么", ["总体能力"]),
    ("智能化转型", ["总体能力"]),
    ("iMeeting 转写怎么进来", ["工具栈", "架构决定"]),
]


def rank_of(hits: list[dict], expected: list[str]) -> int:
    for i, h in enumerate(hits, 1):
        title = str(h.get("title") or "")
        if any(e in title for e in expected):
            return i
    return 0


def evaluate(label: str, fn) -> tuple[float, float, float]:
    r1 = r3 = 0
    mrr = 0.0
    rows = []
    for q, exp in QUERIES:
        hits = fn(q)
        r = rank_of(hits, exp)
        rows.append((q, r, [h.get("title") for h in hits[:3]]))
        if r == 1:
            r1 += 1
        if 1 <= r <= 3:
            r3 += 1
        if r:
            mrr += 1.0 / r
    n = len(QUERIES)
    print(f"\n== {label} ==")
    for q, r, titles in rows:
        mark = "✓" if 1 <= r <= 3 else "×"
        print(f" {mark} {q:<22} rank={r or '-'}  {titles[:2]}")
    print(f" Recall@1 {r1}/{n} = {r1 / n:.2f} | Recall@3 {r3}/{n} = {r3 / n:.2f} | MRR {mrr / n:.3f}")
    return r1 / n, r3 / n, mrr / n


def main() -> None:
    database = db()
    use_expand = "--no-expand" not in sys.argv
    results = {}
    results["legacy"] = evaluate("v0.2 串行", lambda q: search_legacy(database, q, 6))
    results["rrf"] = evaluate("v0.3 RRF", lambda q: search_v2(database, q, 6))
    if use_expand:
        cache: dict[str, list[str]] = {}

        def with_expand(q: str):
            if q not in cache:
                cache[q] = expand(q, database)
            return search_v2(database, q, 6, expansions=cache[q])

        results["rrf_expand"] = evaluate("v0.3 RRF + 查询扩展", with_expand)
    print("\n== 汇总 ==")
    for k, (a, b, c) in results.items():
        print(f" {k:<12} R@1={a:.2f}  R@3={b:.2f}  MRR={c:.3f}")


if __name__ == "__main__":
    main()
