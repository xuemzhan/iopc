"""存量修复：把「同源全连」的旧共现边换成句级证据边。"""
from __future__ import annotations

from typing import Any

from .cooccur import graph_metrics, rebuild_for_source
from .db import Database, db


def _entities_of_source(database: Database, source_id: int) -> list[dict[str, Any]]:
    with database.connect() as c:
        rows = c.execute(
            "SELECT e.id, e.name, e.aliases FROM edges g JOIN entities e ON e.id=g.dst_id"
            " WHERE g.src_type='source' AND g.src_id=? AND g.dst_type='entity'",
            (source_id,),
        ).fetchall()
    out = []
    for r in rows:
        import json

        try:
            aliases = json.loads(r["aliases"] or "[]")
        except Exception:
            aliases = []
        out.append({"id": int(r["id"]), "name": r["name"], "aliases": aliases})
    return out


def redistill_cooccur(database: Database | None = None, top_n: int = 8) -> dict[str, Any]:
    database = database or db()
    before = graph_metrics(database)
    with database.tx() as c:
        c.execute("DELETE FROM edges WHERE rel='co_occurs'")

    sources = []
    with database.connect() as c:
        rows = c.execute(
            "SELECT s.id, d.text FROM sources s JOIN documents d ON d.source_id=s.id"
            " ORDER BY s.id"
        ).fetchall()
    added = 0
    for r in rows:
        ents = _entities_of_source(database, int(r["id"]))
        if len(ents) < 2:
            continue
        added += rebuild_for_source(database, int(r["id"]), r["text"] or "", ents, top_n=top_n)
        sources.append(int(r["id"]))

    after = graph_metrics(database)
    return {"before": before, "after": after, "sources": len(sources), "pairs": added}
