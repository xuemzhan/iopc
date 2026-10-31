"""补全空壳页：用已关联的来源摘要，把实体/概念的 Key Facts 填上。

原则：只用库里已有的素材，素材不够就少写，绝不编造。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .db import Database, db
from .llm import llm
from .vault import Vault

ENRICH_SYSTEM = """你在补全个人知识库的实体/概念页。基于给出的素材写两段内容：
1. 一句话概述（≤80字，说明它是什么、在本知识库里指什么）
2. 3-5 条关键事实（每条≤50字，必须是素材里明确出现的信息）
只输出 JSON：{"summary": "", "facts": ["", ""], "related": ["相关实体或概念名"]}
硬规则：
- 素材没有的信息不要写；宁可 facts 只有 1 条。
- 不写"可能""据说"这类不确定表述。
- 中文。"""


def _material(database: Database, kind: str, oid: int, limit: int = 4) -> list[dict[str, Any]]:
    dst = "entity" if kind == "entity" else "concept"
    with database.connect() as c:
        rows = c.execute(
            "SELECT DISTINCT n.title, n.summary, n.path FROM edges g"
            " JOIN notes n ON n.id=g.src_id AND n.kind='source'"
            " WHERE g.src_type='note' AND g.dst_type=? AND g.dst_id=?"
            " ORDER BY n.updated_at DESC LIMIT ?",
            (dst, oid, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def stubs(database: Database | None = None, limit: int = 20,
          kind: str | None = None) -> list[dict[str, Any]]:
    database = database or db()
    vault = Vault()
    out: list[dict[str, Any]] = []
    kinds = [kind] if kind else ["entity", "concept"]
    with database.connect() as c:
        for k in kinds:
            table = "entities" if k == "entity" else "concepts"
            col = "summary" if k == "entity" else "definition"
            rows = c.execute(
                f"SELECT id, name, COALESCE({col},'') AS summary, page_path FROM {table}"
                f" ORDER BY updated_at DESC"
            ).fetchall()
            for r in rows:
                page_path = r["page_path"] or ""
                p = vault.root / page_path if page_path else None
                if not p or not p.exists():
                    continue
                text = p.read_text(encoding="utf-8", errors="ignore")
                if "（待补充）" in text or re.search(r"## Key Facts\s*\n\s*-\s*\n", text):
                    out.append({"kind": k, "id": int(r["id"]), "name": r["name"],
                                "path": page_path, "has_summary": bool(r["summary"])})
                if len(out) >= limit:
                    return out
    return out


def _set_overview(text: str, summary: str) -> str:
    if "（待补充）" not in text:
        return text
    return text.replace("（待补充）", summary, 1)


def _set_key_facts(text: str, facts: list[str]) -> str:
    if not facts:
        return text
    bullets = "\n".join(f"- {f}" for f in facts)
    pat = re.compile(r"(## Key Facts\s*\n)(.*?)(?=\n## |\Z)", re.S)
    if pat.search(text):
        return pat.sub(lambda m: m.group(1) + bullets + "\n", text, count=1)
    return text


def _add_related(text: str, related: list[str], kind: str) -> str:
    if not related:
        return text
    links = []
    folder = "concepts" if kind == "concept" else "entities"
    for name in related[:4]:
        slug = re.sub(r"\s+", "-", name.strip())
        link = f"[[{folder}/{slug}|{name}]]"
        if link not in text and f"[[{name}]]" not in text:
            links.append(f"- {link}")
    if not links:
        return text
    if "\n## Related" in text:
        return text.rstrip() + "\n" + "\n".join(links) + "\n"
    return text.rstrip() + "\n\n## Related\n" + "\n".join(links) + "\n"


def enrich_one(database: Database, item: dict[str, Any]) -> dict[str, Any]:
    material = _material(database, item["kind"], item["id"])
    if not material:
        return {"skipped": "没有关联来源", **item}
    ctx = "\n".join(f"- 《{m['title']}》：{(m['summary'] or '')[:400]}" for m in material)
    data = llm("enrich").chat_json(
        [
            {"role": "system", "content": ENRICH_SYSTEM},
            {"role": "user", "content": f"页面：{item['name']}（{'实体' if item['kind']=='entity' else '概念'}）\n\n素材：\n{ctx}"},
        ],
        max_tokens=900,
    )
    summary = str(data.get("summary", "")).strip()
    facts = [str(f).strip() for f in (data.get("facts") or []) if str(f).strip()][:5]
    related = [str(r).strip() for r in (data.get("related") or []) if str(r).strip()][:4]

    vault = Vault()
    p = vault.root / item["path"]
    text = p.read_text(encoding="utf-8")
    new = _set_overview(text, summary)
    new = _set_key_facts(new, facts)
    new = _add_related(new, related, item["kind"])
    if new != text:
        p.write_text(new, encoding="utf-8", newline="\n")

    table = "entities" if item["kind"] == "entity" else "concepts"
    col = "summary" if item["kind"] == "entity" else "definition"
    if summary:
        with database.tx() as c:
            c.execute(f"UPDATE {table} SET {col}=?, updated_at=datetime('now') WHERE id=?",
                      (summary, item["id"]))
    return {"kind": item["kind"], "name": item["name"], "path": item["path"],
            "facts": facts, "summary": summary, "skipped": False}


def run(limit: int = 8, kind: str | None = None,
        database: Database | None = None) -> dict[str, Any]:
    database = database or db()
    todo = stubs(database, limit=limit, kind=kind)
    done, skipped = [], []
    for item in todo:
        try:
            res = enrich_one(database, item)
            (skipped if res.get("skipped") else done).append(res)
        except Exception as exc:  # noqa: BLE001
            skipped.append({**item, "error": f"{type(exc).__name__}: {exc}"})
    if done:
        Vault().log([f"enrich 补全 {len(done)} 页：" + "、".join(d["name"] for d in done[:8])])
    return {"enriched": done, "skipped": skipped, "remaining": len(stubs(database, limit=200))}
