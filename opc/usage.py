"""用量记账：每次 LLM 调用记一行，供 `opc stats` 汇总。

不追求精确计费（各家价格不同），只回答三个问题：
今天调了多少次、烧了多少 token、哪一天最贵。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import cfg
from .util import now_iso


def _path() -> Path:
    return cfg().data_dir / "usage.jsonl"


def record(model: str, usage: dict[str, Any], label: str = "") -> None:
    if not usage:
        return
    row = {
        "ts": now_iso(),
        "model": model,
        "label": label,
        "prompt": int(usage.get("prompt_tokens") or 0),
        "completion": int(usage.get("completion_tokens") or 0),
        "total": int(usage.get("total_tokens") or 0),
    }
    try:
        with _path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def summary(days: int = 7) -> dict[str, Any]:
    p = _path()
    buckets: dict[str, dict[str, int]] = {}
    total = {"calls": 0, "prompt": 0, "completion": 0, "total": 0}
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                row = json.loads(line)
                day = str(row.get("ts", ""))[:10]
                b = buckets.setdefault(day, {"calls": 0, "prompt": 0, "completion": 0, "total": 0})
                b["calls"] += 1
                for k in ("prompt", "completion", "total"):
                    b[k] += int(row.get(k) or 0)
                    total[k] += int(row.get(k) or 0)
                total["calls"] += 1
            except Exception:
                continue
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    recent = {d: v for d, v in sorted(buckets.items()) if d >= since}
    return {"today": buckets.get(date.today().isoformat(), {}), "recent": recent, "total": total}

def record_llm(*, task: str, provider: str, model: str, prompt: int, completion: int,
               total: int | None = None, sensitive: bool = False,
               source_id: int | None = None) -> None:
    """写 `llm_usage` 表（结构化、可预算），同时保留 jsonl 兼容 `opc stats`。"""
    ts = now_iso()
    try:
        from .db import db

        with db().tx() as c:
            c.execute(
                "INSERT INTO llm_usage(task, provider, model, in_tokens, out_tokens,"
                " source_id, sensitive, ts) VALUES(?,?,?,?,?,?,?,?)",
                (task, provider, model, int(prompt), int(completion),
                 source_id, 1 if sensitive else 0, ts),
            )
    except Exception:
        pass
    record(model, {"prompt_tokens": prompt, "completion_tokens": completion,
                   "total_tokens": total or (prompt + completion)}, label=task)


def today_tokens() -> int:
    try:
        from .db import db

        with db().connect() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(in_tokens + out_tokens), 0) FROM llm_usage"
                " WHERE substr(ts, 1, 10) = ?",
                (date.today().isoformat(),),
            ).fetchone()
        return int(row[0] or 0)
    except Exception:
        return 0


def usage_rows(days: int = 7) -> list[dict[str, Any]]:
    try:
        from .db import db

        since = (date.today() - timedelta(days=days - 1)).isoformat()
        with db().connect() as c:
            rows = c.execute(
                "SELECT task, provider, model, COUNT(*) n, SUM(in_tokens) i,"
                " SUM(out_tokens) o FROM llm_usage WHERE substr(ts,1,10) >= ?"
                " GROUP BY task, provider, model ORDER BY o DESC",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
