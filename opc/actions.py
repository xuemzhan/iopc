"""行动项：把散落在各知识页里的 `- [ ]` 汇总成一张可执行清单。

- 汇总：从 notes.body_md 的「## 行动项」段提取
- 保状态：已勾选的项在重新生成时保留
- 回写：会议类来源可把行动项推回 iMeeting（需显式开启）
"""
from __future__ import annotations

import re
from typing import Any

from .config import cfg
from .connectors import get as get_connector
from .db import Database, db
from .util import today
from .vault import Vault, _frontmatter, wikilink

ACTION_LINE = re.compile(r"^- \[ \] (?P<text>.+?)(?: —— @(?P<owner>.+))?$")


def collect(database: Database | None = None, limit: int = 200) -> list[dict[str, Any]]:
    database = database or db()
    out: list[dict[str, Any]] = []
    with database.connect() as c:
        rows = c.execute(
            "SELECT n.id, n.title, n.slug, n.path, n.body_md, n.source_id, s.kind, s.external_id"
            " FROM notes n LEFT JOIN sources s ON s.id=n.source_id"
            " WHERE n.kind='source' AND n.body_md LIKE '%## 行动项%'"
            " ORDER BY n.updated_at DESC LIMIT 80"
        ).fetchall()
    for r in rows:
        body = r["body_md"] or ""
        in_sec = False
        for line in body.splitlines():
            if line.startswith("## "):
                in_sec = line.strip() == "## 行动项"
                continue
            if not in_sec:
                continue
            m = re.match(r"^- \[ \] (.+?)(?: —— @(.+))?$", line.strip())
            if not m:
                continue
            out.append({
                "text": m.group(1).strip(),
                "owner": (m.group(2) or "").strip(),
                "note": r["title"],
                "slug": r["slug"],
                "source_id": r["source_id"],
                "kind": r["kind"],
                "external_id": r["external_id"],
            })
            if len(out) >= limit:
                return out
    return out


def _existing_states(vault: Vault) -> dict[str, bool]:
    """读现有清单，保留已勾选状态。"""
    page = vault.root / cfg().get("actions.page", "行动项.md")
    if not page.exists():
        return {}
    out: dict[str, bool] = {}
    for line in page.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"^- \[( |x|X)\] (.+?)(?: —— .*)?$", line.strip())
        if m:
            out[m.group(2).strip()] = m.group(1).lower() == "x"
    return out


def sync_page(database: Database | None = None) -> dict[str, Any]:
    database = database or db()
    vault = Vault()
    items = collect(database)
    states = _existing_states(vault)
    seen: set[str] = set()
    todo, done = [], []
    for it in items:
        key = it["text"]
        if key in seen:
            continue
        seen.add(key)
        src = wikilink("source", it["slug"], it["note"])
        owner = f" @{it['owner']}" if it["owner"] else ""
        line = f"- [{'x' if states.get(key) else ' '}] {it['text']} —— {src}{owner}"
        (done if states.get(key) else todo).append(line)

    page = vault.root / cfg().get("actions.page", "行动项.md")
    fm = _frontmatter(title="行动项", type_="synthetic", tags=["actions", "todo"],
                      sources=[], extra={"generated_at": today(), "count": len(items)})
    body = [
        fm, "", "# 行动项", "",
        "> 自动汇总自各知识页的「行动项」段。勾选状态在重新生成时保留；用 Obsidian Tasks 插件可按日期/标签筛选。",
        "", f"## 待办（{len(todo)}）", *(todo or ["- 暂无"]), "",
        f"## 已完成（{len(done)}）", *(done or ["- 暂无"]), "",
    ]
    page.write_text("\n".join(body), encoding="utf-8", newline="\n")
    vault.log([f"actions 同步：待办 {len(todo)} / 完成 {len(done)} → `{vault.rel(page)}`"])
    return {"path": vault.rel(page), "todo": len(todo), "done": len(done), "items": len(items)}


def push_to_imeeting(source_id: int, database: Database | None = None) -> dict[str, Any]:
    """把一个会议来源的行动项推回 iMeeting。"""
    database = database or db()
    with database.connect() as c:
        src = c.execute("SELECT kind, external_id FROM sources WHERE id=?", (source_id,)).fetchone()
    if not src or src["kind"] != "meeting":
        return {"error": "只有会议来源支持回写"}
    items = [i for i in collect(database) if i["source_id"] == source_id]
    conn = get_connector("imeeting")
    results = []
    for it in items:
        results.append({"text": it["text"], "resp": conn.push_action(int(src["external_id"]), it["text"], it["owner"])})
    return {"pushed": len(results), "results": results}
