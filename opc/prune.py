"""清理伪实体：文件名、路径、URL 被抽成实体时，连同页面对一起删掉。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .db import Database, db
from .pipeline import valid_name
from .util import normalize_key
from .vault import Vault


def scan(database: Database | None = None) -> list[dict[str, Any]]:
    database = database or db()
    out: list[dict[str, Any]] = []
    with database.connect() as c:
        for kind, table in (("entity", "entities"), ("concept", "concepts")):
            for r in c.execute(f"SELECT id, name, page_path FROM {table}").fetchall():
                if not valid_name(r["name"]):
                    out.append({"kind": kind, "id": int(r["id"]), "name": r["name"],
                                "path": r["page_path"] or ""})
    return out


def _page_matches(p: Path, name: str) -> bool:
    if normalize_key(p.stem) == normalize_key(name):
        return True
    head = p.read_text(encoding="utf-8", errors="ignore")[:400]
    m = re.search(r'^title:\s*"?(.*?)"?\s*$', head, re.M)
    return bool(m and normalize_key(m.group(1)) == normalize_key(name))


def prune(apply: bool = False, database: Database | None = None) -> dict[str, Any]:
    database = database or db()
    vault = Vault()
    items = scan(database)
    removed = []
    for it in items:
        if not apply:
            removed.append({**it, "would_remove": True})
            continue
        table = "entities" if it["kind"] == "entity" else "concepts"
        with database.tx() as c:
            c.execute("DELETE FROM edges WHERE (src_type=? AND src_id=?) OR (dst_type=? AND dst_id=?)",
                      (it["kind"], it["id"], it["kind"], it["id"]))
            c.execute(f"DELETE FROM {table} WHERE id=?", (it["id"],))
        p = vault.safe_path(it["path"]) if it["path"] else None
        if p and p.exists() and _page_matches(p, it["name"]):
            p.unlink()
        elif p and p.exists():
            # 页面名字与伪实体不一致：只清数据库，不动文件，避免误删正常页面
            it["kept_file"] = True
        removed.append(it)
    if apply and removed:
        vault.log([f"prune 删除伪实体 {len(removed)} 个：" + "、".join(r["name"] for r in removed[:10])])
    return {"applied": apply, "count": len(removed), "items": removed}
