"""图谱编辑：改节点名/类型、删关系、加关系。

改的是「人给的判断」，不是抽取结果：
- 节点重命名只改显示名（key 保持稳定，避免以后同名实体重新分裂）
- 页面文件不改名，只更新 frontmatter title 与 H1（Obsidian 里的 [[链接]] 不会断）
"""
from __future__ import annotations

import re
from typing import Any

from .db import Database, db
from .util import atomic_write
from .vault import Vault

KINDS = {"entity": ("entities", "entities"), "concept": ("concepts", "concepts")}


def _table(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"未知节点类型：{kind}")
    return KINDS[kind][0]


def rename_node(database: Database, kind: str, node_id: int, *,
                name: str | None = None, type_: str | None = None) -> dict[str, Any]:
    table = _table(kind)
    with database.connect() as c:
        row = c.execute(f"SELECT * FROM {table} WHERE id=?", (node_id,)).fetchone()
    if not row:
        raise ValueError("节点不存在")
    new_name = (name or row["name"]).strip()
    if not new_name:
        raise ValueError("名字不能为空")
    with database.tx() as c:
        if kind == "entity":
            c.execute("UPDATE entities SET name=?, type=?, updated_at=datetime('now') WHERE id=?",
                      (new_name, type_ or row["type"], node_id))
        else:
            c.execute("UPDATE concepts SET name=?, updated_at=datetime('now') WHERE id=?",
                      (new_name, node_id))
    if row["page_path"]:
        _retitle_page(row["page_path"], new_name)
    return {"ok": True, "kind": kind, "id": node_id, "name": new_name,
            "type": (type_ or row["type"]) if kind == "entity" else None}


def _retitle_page(rel_path: str, name: str) -> None:
    vault = Vault()
    p = vault.safe_path(rel_path)
    if p is None or not p.exists():
        return
    text = p.read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r'^title:\s*".*?"\s*$', f'title: "{name}"', text, count=1, flags=re.M)
    text = re.sub(r"^#\s+.*$", f"# {name}", text, count=1, flags=re.M)
    atomic_write(p, text)


def remove_edge(database: Database, edge_id: int) -> dict[str, Any]:
    with database.tx() as c:
        cur = c.execute("DELETE FROM edges WHERE id=?", (edge_id,))
        if not cur.rowcount:
            raise ValueError("关系不存在")
    return {"ok": True, "deleted": edge_id}


def create_edge(database: Database, *, src_type: str, src_id: int,
                dst_type: str, dst_id: int, rel: str = "related_to") -> dict[str, Any]:
    if src_type not in ("entity", "concept") or dst_type not in ("entity", "concept"):
        raise ValueError("只支持实体/概念之间连线")
    if src_type == dst_type and src_id == dst_id:
        raise ValueError("不能连自己")
    for kind, node_id in ((src_type, src_id), (dst_type, dst_id)):
        table = _table(kind)
        with database.connect() as c:
            if not c.execute(f"SELECT 1 FROM {table} WHERE id=?", (node_id,)).fetchone():
                raise ValueError(f"{kind} #{node_id} 不存在")
    database.add_edge(src_type, src_id, dst_type, dst_id, rel, weight=1.0, evidence="人工连线")
    with database.connect() as c:
        row = c.execute(
            "SELECT id FROM edges WHERE src_type=? AND src_id=? AND dst_type=? AND dst_id=? AND rel=?",
            (src_type, src_id, dst_type, dst_id, rel),
        ).fetchone()
    return {"ok": True, "edge_id": int(row["id"]) if row else None}


def rename(database: Database | None = None, **kwargs: Any) -> dict[str, Any]:
    return rename_node(database or db(), **kwargs)
