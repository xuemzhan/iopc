"""查询扩展：把用户的一句话变成几个"检索得动"的词。

中文短句里常夹着代词、口语和隐含意图（"上次那个会"），直接拿去做字面匹配会漏。
这里用一次很便宜的 LLM 调用生成近义/上位/缩写检索词，并按查询哈希缓存。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from .config import cfg
from .db import Database
from .llm import llm
from .util import now_iso, sha

EXPAND_SYSTEM = """你是检索助手。把用户的问题改写成 3-5 个用于「字面检索」的词组。
规则：
1. 每个词 2-8 个字，是原文里可能出现的说法（同义词、上位词、英文缩写、相关实体名）。
2. 不要输出整句，不要解释，不要标点。
3. 若问题里有具体人名/系统名，保留原名。
只输出 JSON：{"terms": ["", ""]}"""


def _cache_get(database: Database, key: str, days: int) -> list[str] | None:
    raw = database.get_meta(key)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        ts = datetime.fromisoformat(obj["ts"])
        now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
        if now - ts > timedelta(days=days):
            return None
        return [str(t) for t in obj.get("terms", [])]
    except Exception:
        return None


def expand(q: str, database: Database | None = None, *, use_cache: bool = True) -> list[str]:
    q = (q or "").strip()
    if len(q) < 4 or not cfg().get("search.expand_queries", True):
        return []
    if database is None:
        from .db import db as get_db

        database = get_db()
    key = f"qexp:{sha(q, 16)}"
    days = int(cfg().get("search.expand_cache_days", 30))
    if use_cache:
        cached = _cache_get(database, key, days)
        if cached is not None:
            return cached
    try:
        data = llm("ask").chat_json(
            [
                {"role": "system", "content": EXPAND_SYSTEM},
                {"role": "user", "content": q},
            ],
            max_tokens=220,
            temperature=0.1,
        )
        terms = [str(t).strip() for t in (data.get("terms") or []) if str(t).strip()]
    except Exception:
        terms = []
    terms = [t for t in terms if 1 < len(t) <= 12][:6]
    database.set_meta(key, json.dumps({"ts": now_iso(), "terms": terms}, ensure_ascii=False))
    return terms
