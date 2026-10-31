"""主题综述：把一个实体/概念散落在多份来源里的信息，压成一张跨来源的页。

这是"提炼"的第二层：
- 第一层（distill）从**一份**来源抽出实体/概念/主张
- 第二层（synthesize）把**同一个主题**的多份来源合起来，给出定义、时间线、分歧与未解问题
"""
from __future__ import annotations

import re
from typing import Any

from .db import Database, db
from .llm import llm
from .util import atomic_write, ensure_unique, normalize_key, slugify, today
from .vault import Vault, _frontmatter, wikilink

SYNTH_SYSTEM = """你是个人知识库的主编。把散落在多份来源里的同一主题，写成一张「主题综述页」。
只输出 JSON。中文。硬规则：
1. 只用给定材料，不编造；材料不足就留空数组。
2. 日期只能取自材料：只有年份就只写年份（如 2016），只有年月就写 2016-10，绝不补全到具体某一天；不确定写"时间未知"。
3. 有分歧就写进 conflicts，不要强行统一口径。
JSON：
{
 "definition": "一句话定义，≤70字",
 "timeline": [{"date": "YYYY-MM-DD 或 时间未知", "event": "≤40字"}],
 "perspectives": [{"who": "哪份材料/谁", "view": "≤50字"}],
 "conflicts": [{"a": "说法A", "b": "说法B", "note": "冲突点≤30字"}],
 "gaps": ["还没弄清楚的问题，≤40字"],
 "related": ["相关实体或概念名"]
}"""


def resolve(name: str, database: Database | None = None) -> dict[str, Any] | None:
    database = database or db()
    key = normalize_key(name)
    with database.connect() as c:
        for kind, table in (("entity", "entities"), ("concept", "concepts")):
            row = c.execute(f"SELECT id, name, page_path FROM {table} WHERE key=?", (key,)).fetchone()
            if row:
                return {"kind": kind, "id": int(row["id"]), "name": row["name"],
                        "path": row["page_path"]}
    with database.connect() as c:
        for kind, table in (("entity", "entities"), ("concept", "concepts")):
            row = c.execute(
                f"SELECT id, name, page_path FROM {table} WHERE name LIKE ?"
                f" ORDER BY length(name) LIMIT 1", (f"%{name}%",)
            ).fetchone()
            if row:
                return {"kind": kind, "id": int(row["id"]), "name": row["name"],
                        "path": row["page_path"]}
    return None


def _claims(text: str, limit: int = 6) -> list[str]:
    out, in_sec = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            in_sec = line.strip() == "## 关键主张"
            continue
        if in_sec and line.strip().startswith("- "):
            t = re.split(r"（置信度", line.strip()[2:])[0].strip()
            if t:
                out.append(t)
        if len(out) >= limit:
            break
    return out


def gather(database: Database, kind: str, oid: int, limit: int = 8) -> list[dict[str, Any]]:
    dst = "entity" if kind == "entity" else "concept"
    vault = Vault()
    with database.connect() as c:
        rows = c.execute(
            "SELECT DISTINCT n.title, n.summary, n.path, s.occurred_at, s.captured_at, s.kind"
            " FROM edges g JOIN notes n ON n.id=g.src_id AND n.kind='source'"
            " LEFT JOIN sources s ON s.id=n.source_id"
            " WHERE g.src_type='note' AND g.dst_type=? AND g.dst_id=?"
            " ORDER BY COALESCE(s.occurred_at, s.captured_at) DESC LIMIT ?",
            (dst, oid, limit),
        ).fetchall()
    out = []
    for r in rows:
        claims: list[str] = []
        if r["path"]:
            p = vault.root / r["path"]
            if p.exists():
                claims = _claims(p.read_text(encoding="utf-8", errors="ignore"))
        out.append({
            "title": r["title"], "summary": r["summary"] or "",
            "path": r["path"], "date": (r["occurred_at"] or r["captured_at"] or "")[:10],
            "kind": r["kind"], "claims": claims,
        })
    return out


def _material(name: str, items: list[dict[str, Any]]) -> str:
    blocks = []
    for i, it in enumerate(items, 1):
        claims = "\n".join(f"  · {c}" for c in it["claims"])
        blocks.append(f"[{i}] 《{it['title']}》（{it['date']}）\n  摘要：{it['summary'][:400]}"
                      + (f"\n  主张：\n{claims}" if claims else ""))
    return "\n\n".join(blocks)


def build(name: str, *, database: Database | None = None, kind: str | None = None,
          limit: int = 8) -> dict[str, Any]:
    database = database or db()
    target = resolve(name, database)
    if not target:
        raise ValueError(f"知识库里没有找到「{name}」这个实体或概念")
    if kind and target["kind"] != kind:
        raise ValueError(f"「{name}」是 {target['kind']}，不是 {kind}")
    items = gather(database, target["kind"], target["id"], limit=limit)
    if not items:
        raise ValueError(f"「{name}」还没有关联来源，无法综述")

    data = llm("brief").chat_json(
        [
            {"role": "system", "content": SYNTH_SYSTEM},
            {"role": "user", "content": f"主题：{target['name']}（{target['kind']}）\n\n"
                                       f"材料：\n{_material(target['name'], items)}"},
        ],
        max_tokens=1800,
    )

    vault = Vault()
    slug = ensure_unique(slugify(f"topic-{target['name']}", 50),
                         {p.stem for p in (vault.wiki / "synthetic").glob("*.md")})
    src_links = [f"- {wikilink('source', _stem(it['path']), it['title'])}" if it["path"] else f"- 《{it['title']}》（未发布页）" for it in items]
    body = [f"## 定义\n{str(data.get('definition', '')).strip() or '（材料不足）'}", ""]

    timeline = [t for t in (data.get("timeline") or []) if isinstance(t, dict) and t.get("event")]
    if timeline:
        body.append("## 时间线")
        for t in timeline[:12]:
            body.append(f"- `{str(t.get('date', '时间未知'))}` {t['event']}")
        body.append("")

    persp = [p for p in (data.get("perspectives") or []) if isinstance(p, dict) and p.get("view")]
    if persp:
        body.append("## 不同说法")
        for p in persp[:8]:
            body.append(f"- **{p.get('who', '')}**：{p['view']}")
        body.append("")

    conflicts = [c for c in (data.get("conflicts") or []) if isinstance(c, dict)]
    if conflicts:
        body.append("## 存在分歧")
        for c in conflicts[:6]:
            body.append(f"> [!warning] {c.get('note', '说法不一致')}\n> A：{c.get('a', '')}\n> B：{c.get('b', '')}\n")
        body.append("")

    gaps = [str(g) for g in (data.get("gaps") or []) if str(g).strip()]
    if gaps:
        body += ["## 未解", *[f"- {g}" for g in gaps[:8]], ""]

    body += ["## 来源", *src_links, ""] if src_links else []

    related = [str(r) for r in (data.get("related") or []) if str(r).strip()][:6]
    if related:
        rows = []
        for r in related:
            hit = resolve(r, database)
            if hit and hit.get("path"):
                rows.append(f"- {wikilink(hit['kind'], _stem(hit['path']), hit['name'])}")
            else:
                rows.append(f"- {r}")
        body += ["## 相关", *rows, ""]

    path = vault.wiki / "synthetic" / f"{slug}.md"
    fm = _frontmatter(
        title=f"主题：{target['name']}", type_="synthetic",
        tags=["topic", target["kind"]], sources=[it["path"].removesuffix(".md") for it in items if it["path"]],
        extra={"subject": target["name"], "subject_kind": target["kind"],
               "generated_at": today(), "source_count": str(len(items))},
    )
    atomic_write(path, fm + f"\n# 主题：{target['name']}\n\n" + "\n".join(body))

    # 回链到实体/概念页
    if target["path"]:
        p = vault.root / target["path"]
        if p.exists():
            label = "主题综述：" + target["name"]
            link = f"- {wikilink('synthetic', slug, label)}"
            text = p.read_text(encoding="utf-8")
            if f"[[synthetic/{slug}" not in text:
                if "## 综合" in text:
                    text = text.rstrip() + "\n" + link + "\n"
                else:
                    text = text.rstrip() + "\n\n## 综合\n" + link + "\n"
                atomic_write(p, text)

    vault.index_add("synthetic", slug, f"主题：{target['name']}", [], "- type: topic")
    vault.log([f"synthesize `{target['name']}` ← {len(items)} 份来源 → `{vault.rel(path)}`"])
    return {"path": vault.rel(path), "subject": target["name"], "kind": target["kind"],
            "sources": len(items), "definition": data.get("definition", ""),
            "timeline": len(timeline), "conflicts": len(conflicts)}


def _stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")
