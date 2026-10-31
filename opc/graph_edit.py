"""图谱编辑：改节点名/类型、删关系、加关系，以及一次撤销。

改的是「人给的判断」，不是抽取结果：
- 节点重命名只改显示名（key 保持稳定，避免以后同名实体重新分裂）
- 页面文件不改名，只更新 frontmatter title 与 H1（Obsidian 里的 [[链接]] 不会断）
- 每次编辑写一条 `graph_edits`，可以撤销上一步
"""
from __future__ import annotations

import json
import re
from typing import Any

from .db import Database, db
from .util import atomic_write, normalize_key, now_iso
from .vault import Vault

KINDS = {"entity": "entities", "concept": "concepts"}


def _table(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"未知节点类型：{kind}")
    return KINDS[kind]


def _record(database: Database, kind: str, payload: dict[str, Any]) -> None:
    with database.tx() as c:
        c.execute(
            "INSERT INTO graph_edits(ts, kind, payload, undone) VALUES(?,?,?,0)",
            (now_iso(), kind, json.dumps(payload, ensure_ascii=False)),
        )


def _rename_raw(database: Database, kind: str, node_id: int, name: str,
                type_: str | None) -> None:
    table = _table(kind)
    with database.tx() as c:
        if kind == "entity":
            c.execute(
                "UPDATE entities SET name=?, type=COALESCE(?, type), updated_at=datetime('now')"
                " WHERE id=?", (name, type_, node_id))
        else:
            c.execute(
                "UPDATE concepts SET name=?, updated_at=datetime('now') WHERE id=?",
                (name, node_id))


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
    if new_name != row["name"]:
        with database.connect() as c:
            dup = c.execute(
                f"SELECT id FROM {table} WHERE key=? AND id<>?",
                (normalize_key(new_name), node_id),
            ).fetchone()
        if dup:
            raise ValueError(f"已有同名节点（#{dup['id']}），不能重名")

    keys = row.keys()
    _record(database, "rename", {
        "kind": kind, "id": node_id, "name": row["name"],
        "type": row["type"] if "type" in keys else None,
        "page_path": row["page_path"] if "page_path" in keys else None,
    })
    _rename_raw(database, kind, node_id, new_name, type_)
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
    with database.connect() as c:
        row = c.execute("SELECT * FROM edges WHERE id=?", (edge_id,)).fetchone()
    if not row:
        raise ValueError("关系不存在")
    _record(database, "edge_delete", dict(row))
    with database.tx() as c:
        c.execute("DELETE FROM edge_sources WHERE edge_id=?", (edge_id,))
        c.execute("DELETE FROM edges WHERE id=?", (edge_id,))
    return {"ok": True, "deleted": edge_id}


def create_edge(database: Database, *, src_type: str, src_id: int,
                dst_type: str, dst_id: int, rel: str = "related_to") -> dict[str, Any]:
    if src_type not in KINDS or dst_type not in KINDS:
        raise ValueError("只支持实体/概念之间连线")
    if src_type == dst_type and src_id == dst_id:
        raise ValueError("不能连自己")
    for kind, node_id in ((src_type, src_id), (dst_type, dst_id)):
        with database.connect() as c:
            if not c.execute(f"SELECT 1 FROM {_table(kind)} WHERE id=?", (node_id,)).fetchone():
                raise ValueError(f"{kind} #{node_id} 不存在")
    with database.connect() as c:
        existing = c.execute(
            "SELECT id FROM edges WHERE src_type=? AND src_id=? AND dst_type=? AND dst_id=? AND rel=?",
            (src_type, src_id, dst_type, dst_id, rel),
        ).fetchone()
    if existing:
        return {"ok": True, "edge_id": int(existing["id"]), "created": False}

    database.add_edge(src_type, src_id, dst_type, dst_id, rel, weight=1.0, evidence="人工连线")
    with database.connect() as c:
        row = c.execute(
            "SELECT id FROM edges WHERE src_type=? AND src_id=? AND dst_type=? AND dst_id=? AND rel=?",
            (src_type, src_id, dst_type, dst_id, rel),
        ).fetchone()
    edge_id = int(row["id"]) if row else None
    if edge_id:
        _record(database, "edge_add", {"edge_id": edge_id})
    return {"ok": True, "edge_id": edge_id, "created": True}


def list_edits(database: Database | None = None, limit: int = 20) -> list[dict[str, Any]]:
    database = database or db()
    with database.connect() as c:
        rows = c.execute(
            "SELECT id, ts, kind, payload, undone FROM graph_edits"
            " WHERE undone=0 ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except Exception:
            payload = {}
        out.append({"id": r["id"], "ts": r["ts"], "kind": r["kind"], "payload": payload})
    return out


def undo_last(database: Database | None = None) -> dict[str, Any]:
    database = database or db()
    with database.connect() as c:
        row = c.execute(
            "SELECT * FROM graph_edits WHERE undone=0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise ValueError("没有可撤销的操作")
    payload = json.loads(row["payload"])

    if row["kind"] == "rename":
        _rename_raw(database, payload["kind"], int(payload["id"]),
                    payload["name"], payload.get("type"))
        if payload.get("page_path"):
            _retitle_page(payload["page_path"], payload["name"])
    elif row["kind"] == "edge_add":
        with database.tx() as c:
            c.execute("DELETE FROM edge_sources WHERE edge_id=?", (payload["edge_id"],))
            c.execute("DELETE FROM edges WHERE id=?", (payload["edge_id"],))
    elif row["kind"] == "edge_delete":
        with database.tx() as c:
            cur = c.execute(
                "INSERT INTO edges(src_type, src_id, dst_type, dst_id, rel, weight,"
                " evidence, source_id, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (payload["src_type"], payload["src_id"], payload["dst_type"],
                 payload["dst_id"], payload["rel"], payload["weight"],
                 payload.get("evidence"), payload.get("source_id"),
                 now_iso()),
            )
            if payload.get("source_id") is not None:
                c.execute(
                    "INSERT OR IGNORE INTO edge_sources(edge_id, source_id) VALUES(?,?)",
                    (int(cur.lastrowid), payload["source_id"]),
                )
    else:
        raise ValueError(f"未知编辑类型：{row['kind']}")

    with database.tx() as c:
        c.execute("UPDATE graph_edits SET undone=1 WHERE id=?", (row["id"],))
    return {"ok": True, "undone": row["kind"], "edit_id": int(row["id"])}


def rename(database: Database | None = None, **kwargs: Any) -> dict[str, Any]:
    return rename_node(database or db(), **kwargs)
