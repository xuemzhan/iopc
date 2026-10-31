"""综述：把一天/一周的碎片压成「今天真正学到了什么」。

产物写进 wiki/synthetic/，并在 _index.md 的「综合」段登记，方便在 Obsidian 里回看。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .db import Database, db
from .llm import llm
from .util import atomic_write, today
from .vault import Vault, _frontmatter, bulletize, wikilink

DAILY_SYSTEM = """你是用户的知识库主编。基于给定的「今日新增素材摘要」写一份极简日报。
要求：
1. 只输出 JSON，不要代码围栏。
2. 忠于素材，不编造素材里没有的事实。
3. 中文，短句，信息密度高；不要写"今天我们学习了"这类空话。
JSON：
{
 "headline": "一句话主线，≤40字",
 "points": [{"text": "今天值得记住的结论，≤60字", "why": "为什么重要，≤30字"}],
 "threads": ["还没想清楚、值得继续追的问题，≤50字"],
 "tomorrow": ["明天最该做的一件事，≤40字"],
 "tags": ["2-4个标签"]
}"""

WEEKLY_SYSTEM = """你是用户的复盘教练。基于一周的日报，写一份周复盘。
只输出 JSON。中文。要指出「这周的主线」「反复出现的概念」「被忽略的线索」。
JSON：
{
 "headline": "本周主线，≤40字",
 "wins": ["本周真正的进展"],
 "patterns": ["反复出现的主题/概念，以及说明"],
 "blindspots": ["被忽略但重要的线索"],
 "next_week": ["下周建议聚焦的 2-3 件事"],
 "tags": []
}"""


def _day_sources(day: str, database: Database) -> list[dict[str, Any]]:
    with database.connect() as c:
        rows = c.execute(
            "SELECT s.id, s.kind, s.title, s.captured_at, n.summary, n.path, n.slug"
            " FROM sources s LEFT JOIN notes n ON n.source_id=s.id AND n.kind='source'"
            " WHERE substr(s.captured_at,1,10)=? ORDER BY s.id",
            (day,),
        ).fetchall()
    return [dict(r) for r in rows]


def _day_delta(day: str, database: Database) -> dict[str, Any]:
    with database.connect() as c:
        new_entities = c.execute(
            "SELECT name FROM entities WHERE substr(created_at,1,10)=? LIMIT 20", (day,)
        ).fetchall()
        new_concepts = c.execute(
            "SELECT name FROM concepts WHERE substr(created_at,1,10)=? LIMIT 20", (day,)
        ).fetchall()
        edges = c.execute(
            "SELECT COUNT(*) FROM edges WHERE substr(created_at,1,10)=?", (day,)
        ).fetchone()[0]
    return {
        "entities": [r["name"] for r in new_entities],
        "concepts": [r["name"] for r in new_concepts],
        "edges": int(edges),
    }


def _material(rows: list[dict[str, Any]], limit_chars: int = 9000) -> str:
    blocks, used = [], 0
    for r in rows:
        summary = (r.get("summary") or "").strip()[:700]
        block = f"- [{r['kind']}] {r['title']}\n  {summary}"
        if used + len(block) > limit_chars:
            break
        used += len(block)
        blocks.append(block)
    return "\n".join(blocks)


def daily(day: str | None = None, *, force: bool = False,
          database: Database | None = None) -> dict[str, Any]:
    database = database or db()
    day = day or today()
    vault = Vault()
    out_path = vault.wiki / "synthetic" / f"daily-{day}.md"
    if out_path.exists() and not force:
        return {"skipped": True, "reason": "今天已有日报", "path": vault.rel(out_path)}

    rows = _day_sources(day, database)
    if not rows:
        return {"skipped": True, "reason": f"{day} 没有新增来源", "path": None}

    data = llm("brief").chat_json(
        [
            {"role": "system", "content": DAILY_SYSTEM},
            {"role": "user", "content": f"日期：{day}\n\n今日新增素材：\n{_material(rows)}"},
        ],
        max_tokens=1600,
    )
    delta = _day_delta(day, database)

    headline = str(data.get("headline", "")).strip()
    points = [p for p in (data.get("points") or []) if isinstance(p, dict) and p.get("text")]
    threads = [str(t) for t in (data.get("threads") or []) if str(t).strip()]
    tomorrow = [str(t) for t in (data.get("tomorrow") or []) if str(t).strip()]
    tags = [str(t) for t in (data.get("tags") or []) if str(t).strip()][:6]

    src_lines = []
    for r in rows:
        if r.get("slug"):
            src_lines.append(f"- {wikilink('source', r['slug'], r['title'])}")
        else:
            src_lines.append(f"- {r['title']}（未提炼）")

    body = [f"## 一句话\n{headline or '（未生成）'}", ""]
    body.append("## 今日新增")
    body.append(f"- 来源 {len(rows)} 条：会议 {sum(1 for r in rows if r['kind']=='meeting')}、"
                f"笔记 {sum(1 for r in rows if r['kind']=='note')}、"
                f"会话 {sum(1 for r in rows if r['kind']=='chat')}、"
                f"文件/网页 {sum(1 for r in rows if r['kind'] in ('file','web'))}")
    body += src_lines
    body.append("")
    if points:
        body.append("## 要点")
        for p in points[:10]:
            why = f"（{p['why']}）" if p.get("why") else ""
            body.append(f"- {p['text']}{why}")
        body.append("")
    body.append("## 知识增量")
    body.append(f"- 新实体 {len(delta['entities'])} 个：" + "、".join(delta["entities"][:12]))
    body.append(f"- 新概念 {len(delta['concepts'])} 个：" + "、".join(delta["concepts"][:10]))
    body.append(f"- 新增关系边 {delta['edges']} 条")
    body.append("")
    if threads:
        body += ["## 待追", bulletize(threads, 8), ""]
    if tomorrow:
        body += ["## 明日"] + [f"- [ ] {t}" for t in tomorrow[:3]] + [""]

    fm = _frontmatter(
        title=f"日报 {day}", type_="synthetic", tags=(tags or ["daily"]) + ["daily"],
        sources=[f"wiki/sources/{r['slug']}" for r in rows if r.get("slug")],
        extra={"period": day, "granularity": "daily"},
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(out_path, fm + f"\n# 日报 {day}\n\n" + "\n".join(body))
    vault.index_add("synthetic", f"daily-{day}", f"日报 {day}", [day], "- type: daily")
    vault.log([f"digest `{day}` → `{vault.rel(out_path)}`（{len(rows)} 条来源）"])
    database.put_note(
        source_id=None, kind="synthetic", title=f"日报 {day}", slug=f"daily-{day}",
        path=vault.rel(out_path), summary=headline, body_md="\n".join(body),
        tags=tags or ["daily"], status="published", model=llm("brief").model,
    )
    return {"skipped": False, "path": vault.rel(out_path), "headline": headline,
            "points": len(points), "sources": len(rows)}


def weekly(anchor: str | None = None, *, force: bool = False,
           database: Database | None = None) -> dict[str, Any]:
    database = database or db()
    end = datetime.strptime(anchor, "%Y-%m-%d") if anchor else datetime.now()
    start = end - timedelta(days=6)
    vault = Vault()
    slug = end.strftime("weekly-%G-W%V")
    out_path = vault.wiki / "synthetic" / f"{slug}.md"
    if out_path.exists() and not force:
        return {"skipped": True, "reason": "本周已有复盘", "path": vault.rel(out_path)}

    dailies = []
    for i in range(7):
        d = (end - timedelta(days=i)).strftime("%Y-%m-%d")
        p = vault.wiki / "synthetic" / f"daily-{d}.md"
        if p.exists():
            dailies.append(p.read_text(encoding="utf-8"))
    if not dailies:
        return {"skipped": True, "reason": "本周没有日报可复盘", "path": None}

    with database.connect() as c:
        top = c.execute(
            "SELECT e.name, COUNT(*) n FROM edges g JOIN entities e ON e.id=g.dst_id"
            " WHERE g.dst_type='entity' AND substr(g.created_at,1,7)>=? GROUP BY e.id"
            " ORDER BY n DESC LIMIT 12",
            (start.strftime("%Y-%m"),),
        ).fetchall()
    hot = [{"name": r["name"], "n": r["n"]} for r in top]

    material = "\n\n".join(d[:2500] for d in dailies[:7])
    data = llm("brief").chat_json(
        [
            {"role": "system", "content": WEEKLY_SYSTEM},
            {"role": "user", "content": f"周期：{start:%Y-%m-%d} ~ {end:%Y-%m-%d}\n"
                                       f"高频实体：{', '.join(h['name'] for h in hot)}\n\n{material}"},
        ],
        max_tokens=1800,
    )
    body = [f"## 主线\n{data.get('headline', '')}", ""]
    for key, title in (("wins", "进展"), ("patterns", "模式"), ("blindspots", "盲点")):
        items = [str(x) for x in (data.get(key) or []) if str(x).strip()]
        if items:
            body += [f"## {title}", bulletize(items, 8), ""]
    if data.get("next_week"):
        body += ["## 下周"] + [f"- [ ] {x}" for x in data["next_week"][:3]] + [""]
    if hot:
        body.append("## 本周高频实体")
        body.append("、".join(f"{h['name']}({h['n']})" for h in hot))
        body.append("")
    body.append("## 覆盖的日报")
    body.append("\n".join(f"- {wikilink('synthetic', f'daily-{(end - timedelta(days=i)):%Y-%m-%d}', f'{(end - timedelta(days=i)):%m-%d}')}"
                          for i in range(7)))

    fm = _frontmatter(
        title=f"周复盘 {start:%m.%d}–{end:%m.%d}", type_="synthetic",
        tags=["weekly", "review"], sources=[], extra={"period": slug, "granularity": "weekly"},
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(out_path, fm + f"\n# 周复盘 {start:%Y-%m-%d} ~ {end:%Y-%m-%d}\n\n"
                 + "\n".join(body))
    vault.index_add("synthetic", slug, f"周复盘 {start:%m.%d}–{end:%m.%d}", [], "- type: weekly")
    vault.log([f"weekly `{slug}` → `{vault.rel(out_path)}`"])
    database.put_note(
        source_id=None, kind="synthetic", title=f"周复盘 {start:%m.%d}–{end:%m.%d}", slug=slug,
        path=vault.rel(out_path), summary=str(data.get("headline", "")), body_md="\n".join(body),
        tags=["weekly"], status="published", model=llm("brief").model,
    )
    return {"skipped": False, "path": vault.rel(out_path), "headline": data.get("headline", "")}
