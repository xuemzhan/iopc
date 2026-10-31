"""思维导图：把一条知识页转成 Obsidian Canvas（可在库内直接打开、拖动、继续长）。"""
from __future__ import annotations

import json
import uuid
from typing import Any

from .db import Database, db
from .util import atomic_write, slugify
from .vault import Vault

COLORS = ["1", "2", "3", "4", "5", "6"]


def _nid() -> str:
    return uuid.uuid4().hex[:16]


def build_canvas(database: Database, source_id: int, max_points: int = 6,
                 max_entities: int = 10, max_concepts: int = 8) -> dict[str, Any]:
    with database.connect() as c:
        src = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        note = c.execute(
            "SELECT * FROM notes WHERE source_id=? AND kind='source'", (source_id,)
        ).fetchone()
        ents = c.execute(
            "SELECT e.id, e.name, e.type FROM edges g JOIN entities e ON e.id=g.dst_id"
            " WHERE g.src_type='source' AND g.src_id=? AND g.dst_type='entity'"
            " GROUP BY e.id ORDER BY SUM(g.weight) DESC LIMIT ?",
            (source_id, max_entities),
        ).fetchall()
        cons = c.execute(
            "SELECT c.id, c.name FROM edges g JOIN concepts c ON c.id=g.dst_id"
            " WHERE g.src_type='source' AND g.src_id=? AND g.dst_type='concept'"
            " GROUP BY c.id ORDER BY SUM(g.weight) DESC LIMIT ?",
            (source_id, max_concepts),
        ).fetchall()
    if not src:
        raise ValueError(f"source {source_id} 不存在")

    title = src["title"]
    summary = (note["summary"] if note else "") or ""
    points = _key_points(note["body_md"] if note else "", max_points)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def add(text: str, x: int, y: int, color: str = "", w: int = 300, h: int = 70) -> str:
        nid = _nid()
        nodes.append({
            "id": nid, "type": "text", "text": text,
            "x": x, "y": y, "width": w, "height": h, "color": color,
        })
        return nid

    def link(a: str, b: str) -> None:
        edges.append({
            "id": _nid(), "fromNode": a, "fromSide": "right", "toNode": b, "toSide": "left",
        })

    root = add(f"# {title}" + (f"\n\n{summary[:120]}" if summary else ""), 0, 0, "6", 360, 110)

    y = -((len(points) + 2) * 110) // 2
    for i, p in enumerate(points):
        pid = add(p, 420, y + i * 110, COLORS[i % 6], 300, 70)
        link(root, pid)

    if ents:
        base = y + len(points) * 110 + 40
        ehead = add("## 实体", 420, base, "4", 220, 50)
        link(root, ehead)
        for i, e in enumerate(ents):
            nid = add(f"{e['name']}（{e['type']}）", 760, base + i * 70, "1", 240, 50)
            link(ehead, nid)

    if cons:
        base = y + len(points) * 110 + 40 + (len(ents) + 1) * 70 + 60
        chead = add("## 概念", 420, base, "5", 220, 50)
        link(root, chead)
        for i, c in enumerate(cons):
            nid = add(c["name"], 760, base + i * 70, "3", 240, 50)
            link(chead, nid)

    return {"nodes": nodes, "edges": edges}


def write_canvas(database: Database | None = None, source_id: int = 1) -> str:
    database = database or db()
    vault = Vault()
    canvas = build_canvas(database, source_id)
    with database.connect() as c:
        src = c.execute("SELECT title FROM sources WHERE id=?", (source_id,)).fetchone()
    name = slugify(src["title"] if src else f"mindmap-{source_id}", 50)
    path = vault.wiki / "synthetic" / f"{name}.canvas"
    atomic_write(path, json.dumps(canvas, ensure_ascii=False, indent=1))
    vault.log([f"mindmap `{src['title'] if src else source_id}` → `{vault.rel(path)}`"])
    return vault.rel(path)


def _key_points(body: str, limit: int) -> list[str]:
    if not body:
        return []
    out: list[str] = []
    in_section = False
    for line in body.splitlines():
        if line.startswith("## "):
            in_section = line.strip() in ("## 要点", "## 关键主张")
            continue
        if in_section and line.strip().startswith("- "):
            text = line.strip()[2:].strip()
            text = text.split("（置信度")[0].strip()
            if text:
                out.append(text[:80])
        if len(out) >= limit:
            break
    return out
