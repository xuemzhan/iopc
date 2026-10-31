"""数据本体：锁定（不可变基准）与冲突仲裁。

语义（P0-C §9）：
- 锁定条件 = 人工确认 × 模型 verdict=pass（AND）；--force 可人工强行锁定，审计必留痕。
- 锁定条目是后续数据的写入根源（anchor）：提炼命中锁定条目只挂接、不覆盖其属性。
- 新数据与锁定条目冲突 → conflicts 台账 → LLM 三裁（keep_locked / overturn / unsure）；
  LLM 无权直接修改锁定条目——overturn 与 unsure 都只是"需要人工"。
"""
from __future__ import annotations

from typing import Any

from .config import cfg
from .db import Database, db
from .llm import llm
from .util import from_json, normalize_key, now_iso, to_json

VERDICT_SYSTEM = """你是个人知识库的守门员。给你一个"锚定条目"（已经过人工与模型双重确认，
作为该名称的不可变基准）及其归纳证据，任务：判断它是否可靠到可以锁定/是否合理。
只输出 JSON：{"decision": "pass|unsure|fail", "reason": "≤50字", "confidence": 0.0~1.0}
- 当名称与类型、概述、来源相互一致且信息具体 → pass
- 信息太薄、类型可疑（与常识冲突）→ fail
- 证据不足以判断 → unsure。中文。"""

ADJUDICATE_SYSTEM = """你是个人知识库的冲突仲裁者。一条「锁定条目」是无法被普通流程修改的基准，
新提取的数据与它冲突。三选一：
只输出 JSON：{"decision": "keep_locked|overturn|unsure", "reason": "≤60字", "confidence": 0.0~1.0}
- keep_locked：新数据是抽取噪声/同词异义，锁定条目仍成立
- overturn：锁定条目本身明显错误（有充分理由相信抽取得准确）
- unsure：证据不足以裁决。注意：你不直接修改锁定条目，overturn 也只是把问题转给人工。中文。"""


def _kind_table(kind: str) -> str:
    if kind not in ("entity", "concept"):
        raise ValueError(f"未知对象类型 {kind}，应为 entity|concept")
    return "entities" if kind == "entity" else "concepts"


def snapshot(database: Database, kind: str, oid: int) -> dict[str, Any]:
    """给 verdict 用的锚定条目简报。"""
    table = _kind_table(kind)
    is_entity = kind == "entity"
    with database.connect() as c:
        row = c.execute(f"SELECT * FROM {table} WHERE id=?", (oid,)).fetchone()
        n_sources = c.execute(
            "SELECT COUNT(DISTINCT source_id) FROM edges"
            " WHERE dst_type=? AND dst_id=? AND rel=? AND src_type='source'",
            (kind, oid, "mentions" if is_entity else "covers"),
        ).fetchone()[0]
    if not row:
        raise ValueError(f"{kind} {oid} 不存在")
    keys = list(row.keys())
    return {
        "name": row["name"],
        "type": (row["type"] or "") if is_entity else "",
        "definition": row["definition"] if kind == "concept" else (row["summary"] or ""),
        "aliases": row["aliases"],
        "src_count": int(row["src_count"] or 0) if "src_count" in keys else int(n_sources),
        "link_sources": int(n_sources),
        "page_path": row["page_path"] or "",
    }


def verdict(database: Database, kind: str, oid: int) -> dict[str, Any]:
    """模型对一个条目「是否可靠」的判断。永不抛错——失败按 unsure 处理。"""
    snap = snapshot(database, kind, oid)
    try:
        out = llm("verdict").chat_json(
            [{"role": "system", "content": VERDICT_SYSTEM},
             {"role": "user", "content": to_json(snap)}],
            max_tokens=300, temperature=0.0,
        )
        decision = str(out.get("decision") or "").lower()
        if decision not in ("pass", "unsure", "fail"):
            decision = "unsure"
        conf = float(out.get("confidence") or 0)
        return {"decision": decision, "reason": str(out.get("reason") or ""),
                "confidence": max(0.0, min(1.0, conf))}
    except Exception as exc:  # noqa: BLE001
        return {"decision": "unsure", "reason": f"裁决模型不可用：{type(exc).__name__}",
                "confidence": 0.0}


def approve(database: Database, kind: str, ident: int | str, *, force: bool = False,
            by: str = "human") -> dict[str, Any]:
    """人工确认 + 模型判断合理 = 锁定。"""
    database = database or db()
    row = database.object_row(kind, ident)
    if not row:
        raise ValueError(f"{kind} {ident} 不存在")
    oid = int(row["id"])
    if row["locked"]:
        return {"id": oid, "locked": True, "changed": False, "reason": "已锁定"}
    # --force 是人工 override：明确不再花一次模型调用（CLI help 的承诺）
    v = {"decision": "forced", "reason": "人工强制锁定（--force）", "confidence": 1.0} \
        if force else verdict(database, kind, oid)
    if v["decision"] == "pass" or force:
        tag = f"{by}-force" if force else f"{by}+model"
        database.set_locked(kind, oid, True, by=tag)
        _sync_lock_page(kind, dict(database.object_row(kind, oid) or {}), True, database)
        database.event("ontology.lock", {"kind": kind, "id": oid, "verdict": v,
                                         "override": force, "by": tag})
        return {"id": oid, "locked": True, "changed": True, "verdict": v,
                "override": force}
    database.event("ontology.rejected_lock", {"kind": kind, "id": oid, "verdict": v})
    return {"id": oid, "locked": False, "changed": False, "verdict": v,
            "reason": "模型判断未通过；如坚持请 --force"}


def _set_page_lock(page_path: str, locked: bool, *, by: str = "", at: str = "") -> bool:
    """把锁定态写进页面 frontmatter（重建后据此恢复；无页面则返回 False）。"""
    try:
        from .util import atomic_write
        from .vault import Vault

        vault = Vault()
        p = vault.safe_path(page_path or "")
        if p is None or not p.exists():
            return False
        text = p.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return False
        parts = text.split("---", 2)
        if len(parts) < 3:
            return False
        keep = [ln for ln in parts[1].strip("\n").splitlines()
                if not ln.split(":", 1)[0].strip() in ("locked", "locked_by", "locked_at")]
        keep.append(f"locked: {'true' if locked else 'false'}")
        if locked:
            keep.append(f'locked_by: "{by}"')
            keep.append(f'locked_at: "{at}"')
        atomic_write(p, "---\n" + "\n".join(keep) + "\n---" + parts[2])
        return True
    except Exception:  # noqa: BLE001 — 页面同步失败不影响库内锁定
        return False


def _sync_lock_page(kind: str, row: dict[str, Any], locked: bool,
                    database: Database | None = None) -> None:
    """锁定态落到页面；没有页面的条目先补一张最小页（否则重建必丢，FR-511）。"""
    page = str(row.get("page_path") or "")
    if not page:
        try:
            database = database or db()
            from .vault import Vault

            vault = Vault()
            name = str(row.get("name") or "")
            aliases = [str(a) for a in (from_json(row.get("aliases"), []) or [])]
            if kind == "entity":
                slug = vault.unique_slug("entity", name)
                p = vault.ensure_entity_page(
                    slug=slug, name=name, type_=str(row.get("type") or "other"),
                    description=str(row.get("summary") or ""),
                    aliases=aliases, source_link="")
            else:
                slug = vault.unique_slug("concept", name)
                p = vault.ensure_concept_page(
                    slug=slug, name=name, definition=str(row.get("definition") or ""),
                    aliases=aliases, source_link="")
            page = vault.rel(p)
            row["page_path"] = page
            table = "entities" if kind == "entity" else "concepts"
            with database.tx() as c:
                c.execute(f"UPDATE {table} SET page_path=? WHERE id=?",
                          (page, int(row["id"])))
        except Exception:  # noqa: BLE001 — 建页失败仍可锁库内条目
            return
    if page:
        _set_page_lock(page, locked, by=str(row.get("locked_by") or ""),
                       at=str(row.get("locked_at") or ""))


def lock(database: Database, kind: str, ident: int | str, *, by: str = "human",
         force: bool = False) -> dict[str, Any]:
    """直接锁定（CLI --force / API 调用方的快捷路径）。"""
    database = database or db()
    row = database.object_row(kind, ident)
    if not row:
        raise ValueError(f"{kind} {ident} 不存在")
    oid = int(row["id"])
    database.set_locked(kind, oid, True, by=by)
    _sync_lock_page(kind, dict(database.object_row(kind, oid) or {}), True, database)
    database.event("ontology.lock", {"kind": kind, "id": oid, "by": by, "direct": True})
    return {"id": oid, "locked": True, "changed": not bool(row["locked"])}


def unlock(database: Database, kind: str, ident: int | str) -> dict[str, Any]:
    """人工解锁（恢复可修改；必留审计）。"""
    database = database or db()
    row = database.object_row(kind, ident)
    if not row:
        raise ValueError(f"{kind} {ident} 不存在")
    oid = int(row["id"])
    database.set_locked(kind, oid, False, by="")
    _sync_lock_page(kind, dict(database.object_row(kind, oid) or {}), False, database)
    database.event("ontology.unlock", {"kind": kind, "id": oid})
    return {"id": oid, "locked": False, "changed": bool(row["locked"])}


def anchor(database: Database, kind: str, key: str, extracted: dict[str, Any],
           source_id: int | None) -> dict[str, Any] | None:
    """提炼挂接点：命中锁定行 → 返回锁定的权威字段；
    实体类型不一致 → 记冲突（幂等）。未锁定的行返回 None（走原 upsert 路径）。"""
    table = _kind_table(kind)
    with database.connect() as c:
        row = c.execute(
            f"SELECT * FROM {table} WHERE key=? AND locked=1", (normalize_key(key),)
        ).fetchone()
    if not row:
        return None
    oid = int(row["id"])
    is_entity = kind == "entity"
    keys = list(row.keys())
    authoritative = {
        "id": oid,
        "name": row["name"],
        "slug": (row["page_path"] or "x.md").rsplit("/", 1)[-1].rsplit(".", 1)[0],
        "type": (row["type"] or "") if (is_entity and "type" in keys) else "",
        "definition": row["definition"] if kind == "concept" else (row["summary"] or ""),
    }
    if kind == "entity" and source_id is not None:
        et = str(extracted.get("type") or "").strip().lower()
        et = et if et in ("person", "org", "system", "tool", "project", "place", "event", "other") else "other"
        if authoritative["type"] and et != "other" and et != authoritative["type"]:
            database.add_conflict("entity", oid, "type", authoritative["type"],
                                  et, source_id)
    return authoritative


def adjudicate(database: Database, cid: int) -> dict[str, Any]:
    """LLM 三裁一条冲突。任何结果都不直接修改锁定条目。"""
    database = database or db()
    conflict = database.conflict(cid)
    if not conflict:
        raise ValueError(f"冲突 {cid} 不存在")
    if conflict["status"] != "open":
        return {"id": cid, "status": conflict["status"], "changed": False}
    snap = snapshot(database, conflict["kind"], int(conflict["ref_id"]))
    payload = {
        "kind": conflict["kind"], "field": conflict["field"],
        "locked_value": conflict["locked_value"],
        "incoming_value": conflict["incoming_value"],
        "from_source_id": conflict["incoming_source"],
        "entry_context": {k: snap[k] for k in ("name", "type", "definition", "src_count")},
    }
    threshold = float(cfg().get("ontology.adjudicate_min_confidence", 0.7))
    try:
        out = llm("verdict").chat_json(
            [{"role": "system", "content": ADJUDICATE_SYSTEM},
             {"role": "user", "content": to_json(payload)}],
            max_tokens=400, temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        out = {"decision": "unsure", "reason": f"仲裁模型不可用：{type(exc).__name__}",
               "confidence": 0.0}
    decision = str(out.get("decision") or "unsure").lower()
    if decision not in ("keep_locked", "overturn", "unsure"):
        decision = "unsure"
    conf = float(out.get("confidence") or 0)

    if decision == "keep_locked" and conf >= threshold:
        status = "llm_rejected"
    elif decision == "overturn" and conf >= threshold:
        status = "llm_overturn"        # 需人工：锁定条目是否真的错了
    else:
        status = "needs_human"
    database.decide_conflict(cid, status, {"decision": decision, "reason": out.get("reason"),
                                           "confidence": conf})
    database.event("ontology.adjudicated", {"id": cid, "status": status,
                                            "verdict": out})
    return {"id": cid, "status": status, "changed": True,
            "verdict": {"decision": decision, "reason": out.get("reason"), "confidence": conf}}


def adjudicate_pending(database: Database | None = None, limit: int = 6) -> list[dict[str, Any]]:
    """批量仲裁 open 冲突（默认 6 条，与 lint 矛盾检查同预算量级）。"""
    database = database or db()
    return [adjudicate(database, c["id"]) for c in database.open_conflicts(limit=limit)]


def dismiss_conflict(database: Database, cid: int) -> dict[str, Any]:
    database = database or db()
    database.decide_conflict(cid, "dismissed")
    database.event("ontology.conflict_dismissed", {"id": cid})
    return {"id": cid, "status": "dismissed"}


# ======================================================================
# M1：类型校正 / 归并候选 / 归并 / 人工审阅（FR-502~505）
# ======================================================================

ENTITY_TYPES = ["person", "org", "system", "tool", "project", "place", "event", "other"]

# 规则通道：只做高置信的尾缀映射；命中不了交给 LLM，再不行保持原样
_TYPE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("集团", "公司", "院", "部门", "所", "局", "中心", "委员会", "部门",
      "组织", "协会", "学会", "事业部"), "org"),
    (("系统", "平台", "引擎", "框架", "服务", "中台", "网关", "数据库", "组件"), "system"),
    (("项目", "工程", "计划", "专项", "课题"), "project"),
    (("工具", "插件", "客户端", "扩展", "脚本", "命令行"), "tool"),
    (("城市", "地区", "省", "市", "区", "园区", "基地", "园区"), "place"),
    (("会议", "大会", "活动", "论坛", "峰会", "研讨会"), "event"),
]


def _table(kind: str) -> str:
    return _kind_table(kind)


def allowed_types(database: Database | None = None, kind: str = "entity") -> list[str]:
    """受控类型枚举：以 `ontology_types` 为准，表空时退回内置集合。"""
    database = database or db()
    with database.connect() as c:
        rows = c.execute(
            "SELECT name FROM ontology_types WHERE kind=? ORDER BY builtin DESC, name", (kind,)
        ).fetchall()
    names = [str(r["name"]) for r in rows]
    if names:
        return names
    return list(ENTITY_TYPES) if kind == "entity" else []


def types(database: Database | None = None, kind: str = "entity") -> dict[str, Any]:
    """类型分布（A1）：返回各类型计数、占比、枚举外占比与状态计数。"""
    database = database or db()
    table = _table(kind)
    allowed = set(allowed_types(database, kind))
    with database.connect() as c:
        rows = c.execute(
            f"SELECT COALESCE(type,'') AS t, COUNT(*) AS n FROM {table} GROUP BY t ORDER BY n DESC"
        ).fetchall()
        locked = c.execute(f"SELECT COUNT(*) FROM {table} WHERE locked=1").fetchone()[0]
        extra = {}
        if kind == "entity":
            extra = dict(c.execute(
                "SELECT COALESCE(status,''), COUNT(*) FROM entities GROUP BY status").fetchall())
            merged = c.execute(
                "SELECT COUNT(*) FROM entities WHERE merged_into IS NOT NULL").fetchone()[0]
        else:
            merged = 0
    total = sum(int(r["n"]) for r in rows)
    dist = [{"type": str(r["t"]) or "(空)", "count": int(r["n"]),
             "ratio": round(int(r["n"]) / total, 4) if total else 0.0} for r in rows]
    outside = sum(int(r["n"]) for r in rows if str(r["t"]) not in allowed)
    return {
        "kind": kind, "total": total, "types": dist, "allowed": sorted(allowed),
        "outside_enum": outside,
        "outside_ratio": round(outside / total, 4) if total else 0.0,
        "other_ratio": round(next((d["ratio"] for d in dist if d["type"] == "other"), 0.0), 4),
        "locked": int(locked), "merged": int(merged), "status": extra,
    }


def rule_type(name: str, allowed: set[str] | None = None) -> str | None:
    """尾缀规则：只对能唯一说清的类型下判断，其余返回 None。"""
    n = (name or "").strip()
    if not n or len(n) > 24:
        return None
    for suffixes, target in _TYPE_RULES:
        if (allowed is None or target in allowed) and n.endswith(tuple(suffixes)):
            return target
    return None


def _llm_typefix(rows: list[dict[str, Any]], allowed: list[str], batch: int = 50
                 ) -> dict[str, str]:
    """LLM 通道：每批 50 个，只发 name/type/summary，返回 {name: type}。"""
    out: dict[str, str] = {}
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        payload = [{"name": r["name"], "type": r["type"], "summary": (r["summary"] or "")[:80]}
                   for r in chunk]
        try:
            data = llm("brief").chat_json(
                [
                    {"role": "system", "content":
                        "你在校正个人知识库的实体类型。只能从这些类型里选："
                        + " / ".join(allowed) +
                        "。不确定就选 other。只输出 JSON："
                        '{"items": [{"name": "", "type": ""}]}，必须覆盖输入的全部 name。'},
                    {"role": "user", "content": to_json(payload)},
                ],
                max_tokens=1200, temperature=0.0,
            )
        except Exception:  # noqa: BLE001 — 模型不可用时只做规则通道
            continue
        for it in (data.get("items") or []):
            if not isinstance(it, dict):
                continue
            name, typ = str(it.get("name") or "").strip(), str(it.get("type") or "").strip()
            if name and typ in allowed:
                out[name] = typ
    return out


def typefix(database: Database | None = None, *, apply: bool = False,
            use_llm: bool = True, limit: int | None = None, by: str = "human") -> dict[str, Any]:
    """存量类型校正（A1/A5）：默认 dry-run；`apply=True` 落库并写审计。"""
    database = database or db()
    allowed_list = allowed_types(database, "entity")
    allowed = set(allowed_list)
    before = types(database, "entity")
    with database.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, key, name, type, summary FROM entities"
            " WHERE COALESCE(type,'') NOT IN ("
            + ",".join("?" * len(allowed)) + ") OR COALESCE(type,'')='other'"
            " ORDER BY src_count DESC, id",
            tuple(sorted(allowed)),
        ).fetchall()]
    if limit:
        rows = rows[: int(limit)]

    changes: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for r in rows:
        new = rule_type(r["name"], allowed)
        if new and new != r["type"]:
            changes.append({"id": int(r["id"]), "name": r["name"],
                            "from": r["type"] or "", "to": new, "by": "rule"})
        elif not new:
            unresolved.append(r)

    if use_llm and unresolved:
        mapping = _llm_typefix(unresolved, allowed_list)
        for name, typ in mapping.items():
            hit = next((r for r in unresolved if r["name"] == name), None)
            if hit and typ != hit["type"]:
                changes.append({"id": int(hit["id"]), "name": hit["name"],
                                "from": hit["type"] or "", "to": typ, "by": "llm"})

    applied = 0
    if apply and changes:
        with database.tx() as c:
            for ch in changes:
                c.execute(
                    "UPDATE entities SET type=?, updated_at=? WHERE id=?",
                    (ch["to"], now_iso(), ch["id"]),
                )
                applied += 1
        database.event("ontology.typefix", {
            "by": by, "applied": applied,
            "rule": sum(1 for ch in changes if ch["by"] == "rule"),
            "llm": sum(1 for ch in changes if ch["by"] == "llm"),
            "changes": changes[:50],
        })
    after = types(database, "entity") if apply else None
    return {"kind": "entity", "apply": bool(apply), "scanned": len(rows),
            "proposed": len(changes), "applied": applied,
            "changes": changes, "unresolved": len(unresolved),
            "before": before, "after": after}


def _entity_rows(database: Database) -> list[dict[str, Any]]:
    with database.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, key, name, aliases, src_count, status, locked, merged_into"
            " FROM entities WHERE COALESCE(status,'') NOT IN ('merged','rejected')"
        ).fetchall()]
    for r in rows:
        r["aliases"] = [str(a) for a in (from_json(r["aliases"], []) or [])]
    return rows


def _neighbor_index(database: Database) -> dict[int, set[int]]:
    adj: dict[int, set[int]] = {}
    with database.connect() as c:
        rows = c.execute(
            "SELECT src_id, dst_id FROM edges WHERE rel='co_occurs'"
            " AND src_type='entity' AND dst_type='entity'"
        ).fetchall()
    for a, b in rows:
        adj.setdefault(int(a), set()).add(int(b))
        adj.setdefault(int(b), set()).add(int(a))
    return adj


def candidates(database: Database | None = None, *, limit: int = 100,
               min_jaccard: float = 0.7, min_neighbors: int = 3,
               min_overlap: int = 2, min_src: int = 2) -> dict[str, Any]:
    """归并候选（A2/FR-503）：同 key/别名/邻居 Jaccard 三通道，只提议不合并。

    邻居通道只对已准入实体（src_count ≥ min_src）生效；Jaccard 默认 0.7 而不是
    P0-C 的 0.5——实测 0.5~0.6 在单会议小图上全是"泛称同现"噪声（A单位/b单位…）。
    """
    database = database or db()
    ents = _entity_rows(database)
    by_id = {int(e["id"]): e for e in ents}
    pair: dict[tuple[int, int], dict[str, Any]] = {}

    def bump(a: int, b: int, score: float, reason: str) -> None:
        if a == b:
            return
        key = (min(a, b), max(a, b))
        cur = pair.get(key)
        if cur is None or score > cur["score"]:
            pair[key] = {"a": key[0], "b": key[1], "score": score, "reason": reason}

    norm = lambda s: normalize_key(str(s or ""))  # noqa: E731
    names = {int(e["id"]): {norm(e["name"])} | {norm(a) for a in e["aliases"]} for e in ents}
    for i, a in enumerate(ents):
        for b in ents[i + 1:]:
            ai, bi = int(a["id"]), int(b["id"])
            common = names[ai] & names[bi]
            if not common:
                continue
            if norm(a["name"]) == norm(b["name"]):
                bump(ai, bi, 0.95, f"同名：{norm(a['name'])}")
            elif norm(a["name"]) in names[bi] or norm(b["name"]) in names[ai]:
                bump(ai, bi, 0.9, "名称被对方收为别名")
            else:
                bump(ai, bi, 0.85, f"别名相同：{sorted(common)[0]}")

    adj = _neighbor_index(database)
    shared: dict[tuple[int, int], int] = {}
    inv: dict[int, list[int]] = {}
    for u, ns in adj.items():
        for v in ns:
            inv.setdefault(v, []).append(u)
    for v, nodes in inv.items():
        for i, u in enumerate(nodes):
            for w in nodes[i + 1:]:
                key = (min(u, w), max(u, w))
                shared[key] = shared.get(key, 0) + 1
    for (u, w), inter in shared.items():
        if inter < min_overlap:
            continue
        if len(adj.get(u, set())) < min_neighbors or len(adj.get(w, set())) < min_neighbors:
            continue
        if int(by_id[u].get("src_count") or 0) < min_src:
            continue
        if int(by_id[w].get("src_count") or 0) < min_src:
            continue
        union = len(adj.get(u, set()) | adj.get(w, set()))
        jac = inter / union if union else 0.0
        if jac >= min_jaccard:
            bump(u, w, round(jac, 4), f"共现邻居 {inter} 个重叠（Jaccard {jac:.2f}）")

    open_pairs = []
    with database.tx() as c:
        for p in pair.values():
            c.execute(
                "INSERT INTO merge_candidates(kind, a_id, b_id, score, reason, status,"
                " created_at, updated_at) VALUES('entity',?,?,?,?,'open',?,?)"
                " ON CONFLICT(kind, a_id, b_id) DO UPDATE SET"
                " score=excluded.score, reason=excluded.reason, updated_at=excluded.updated_at"
                " WHERE merge_candidates.status='open'",
                (p["a"], p["b"], p["score"], p["reason"], now_iso(), now_iso()),
            )
        # 本轮不再成立的 open 候选标记 rejected（阈值调整后不残留噪声）
        current = {(int(p["a"]), int(p["b"])) for p in pair.values()}
        for r in c.execute(
            "SELECT id, a_id, b_id FROM merge_candidates"
            " WHERE kind='entity' AND status='open'"
        ).fetchall():
            if (int(r["a_id"]), int(r["b_id"])) not in current:
                c.execute("UPDATE merge_candidates SET status='rejected', updated_at=?"
                          " WHERE id=?", (now_iso(), r["id"]))
        open_pairs = [dict(r) for r in c.execute(
            "SELECT * FROM merge_candidates WHERE kind='entity' AND status='open'"
            " ORDER BY score DESC, id LIMIT ?", (int(limit),)
        ).fetchall()]
    database.event("ontology.candidates", {"generated": len(pair), "open": len(open_pairs)})
    for r in open_pairs:
        a, b = by_id.get(int(r["a_id"])), by_id.get(int(r["b_id"]))
        r["a_name"] = a["name"] if a else f"#{r['a_id']}"
        r["b_name"] = b["name"] if b else f"#{r['b_id']}"
    return {"generated": len(pair), "open": open_pairs}


def _prepend_warning(page: str, block: str) -> bool:
    """在页面 frontmatter 之后插入一次警告块；已存在则不重复。"""
    try:
        from .vault import Vault

        vault = Vault()
        p = vault.safe_path(page)
        if p is None or not p.exists():
            return False
        text = p.read_text(encoding="utf-8")
        if block.splitlines()[0] in text:
            return False
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                new = f"---{parts[1]}---\n{block}\n{parts[2].lstrip(chr(10))}"
            else:
                new = block + "\n" + text
        else:
            new = block + "\n" + text
        from .util import atomic_write

        atomic_write(p, new)
        return True
    except Exception:  # noqa: BLE001 — 页面提示失败不影响库内归并
        return False


def merge(database: Database | None = None, a: int | str = "", b: int | str = "",
          *, kind: str = "entity", by: str = "human", reason: str = "") -> dict[str, Any]:
    """执行归并（A3/FR-504）：别名并入、边重指、撞键合并来源、状态 merged、审计。"""
    database = database or db()
    table = _table(kind)
    if kind != "entity":
        raise ValueError("M1 只支持实体归并（概念归并见 WP-04）")
    row_a = database.object_row(kind, a)
    row_b = database.object_row(kind, b)
    if not row_a or not row_b:
        raise ValueError("归并对象不存在")
    a_id, b_id = int(row_a["id"]), int(row_b["id"])
    if a_id == b_id:
        raise ValueError("不能与自己归并")
    if row_b["merged_into"]:
        if int(row_b["merged_into"]) == a_id:
            return {"changed": False, "reason": "已并入该条目", "a": a_id, "b": b_id}
        raise ValueError(f"该条目已并入 #{row_b['merged_into']}")
    if row_a["merged_into"]:
        raise ValueError(f"目标条目已并入 #{row_a['merged_into']}，请直接归并到它")
    if row_b["locked"]:
        raise ValueError("锁定条目不可作为被归并端（先 unlock）")

    a_aliases = [str(x) for x in (from_json(row_a["aliases"], []) or [])]
    b_aliases = [str(x) for x in (from_json(row_b["aliases"], []) or [])]
    a_name = str(row_a["name"])
    alias_set = list(dict.fromkeys(
        [x for x in a_aliases + b_aliases + [str(row_b["name"])] if x and x != a_name]
        + [x for x in a_aliases if x]
    ))
    min_src = int(cfg().get("vault.min_sources_for_page", 2))

    moved = collapsed = dropped = 0
    with database.tx() as c:
        edges = [dict(r) for r in c.execute(
            "SELECT * FROM edges WHERE (src_type=? AND src_id=?) OR (dst_type=? AND dst_id=?)",
            (kind, b_id, kind, b_id),
        ).fetchall()]
        for e in edges:
            src = (kind, a_id) if (e["src_type"] == kind and int(e["src_id"]) == b_id) \
                else (e["src_type"], int(e["src_id"]))
            dst = (kind, a_id) if (e["dst_type"] == kind and int(e["dst_id"]) == b_id) \
                else (e["dst_type"], int(e["dst_id"]))
            if src == dst:
                c.execute("DELETE FROM edge_sources WHERE edge_id=?", (e["id"],))
                c.execute("DELETE FROM edges WHERE id=?", (e["id"],))
                dropped += 1
                continue
            exist = c.execute(
                "SELECT id, evidence FROM edges WHERE src_type=? AND src_id=?"
                " AND dst_type=? AND dst_id=? AND rel=?",
                (*src, *dst, e["rel"]),
            ).fetchone()
            if exist:
                c.execute(
                    "INSERT OR IGNORE INTO edge_sources(edge_id, source_id)"
                    " SELECT ?, source_id FROM edge_sources WHERE edge_id=?",
                    (exist["id"], e["id"]),
                )
                if e["rel"] in ("mentions", "covers", "co_occurs"):
                    cnt = c.execute(
                        "SELECT COUNT(*) FROM edge_sources WHERE edge_id=?", (exist["id"],)
                    ).fetchone()[0]
                    c.execute("UPDATE edges SET weight=? WHERE id=?", (float(cnt), exist["id"]))
                longer = (e["evidence"] if len(e["evidence"] or "") > len(exist["evidence"] or "")
                          else exist["evidence"])
                c.execute("UPDATE edges SET evidence=? WHERE id=?", (longer, exist["id"]))
                c.execute("DELETE FROM edge_sources WHERE edge_id=?", (e["id"],))
                c.execute("DELETE FROM edges WHERE id=?", (e["id"],))
                collapsed += 1
                continue
            c.execute(
                "UPDATE edges SET src_type=?, src_id=?, dst_type=?, dst_id=? WHERE id=?",
                (*src, *dst, e["id"]),
            )
            moved += 1

        c.execute(
            f"UPDATE {table} SET aliases=?, updated_at=? WHERE id=?",
            (to_json(alias_set), now_iso(), a_id),
        )
        src_count = c.execute(
            "SELECT COUNT(DISTINCT source_id) FROM edges WHERE src_type='source'"
            " AND dst_type=? AND dst_id=? AND rel='mentions' AND source_id IS NOT NULL",
            (kind, a_id),
        ).fetchone()[0]
        status = "confirmed" if int(src_count) >= min_src else (row_a["status"] or "proposed")
        c.execute(f"UPDATE {table} SET src_count=?, status=?, updated_at=? WHERE id=?",
                  (int(src_count), status, now_iso(), a_id))
        c.execute(f"UPDATE {table} SET status='merged', merged_into=?, updated_at=? WHERE id=?",
                  (a_id, now_iso(), b_id))
        c.execute(
            "UPDATE merge_candidates SET status='merged', updated_at=?"
            " WHERE kind=? AND status='open' AND a_id=? AND b_id=?",
            (now_iso(), kind, min(a_id, b_id), max(a_id, b_id)),
        )
        c.execute(
            "UPDATE merge_candidates SET status='rejected', updated_at=?"
            " WHERE kind=? AND status='open'"
            " AND (a_id IN (?,?) OR b_id IN (?,?))",
            (now_iso(), kind, a_id, b_id, a_id, b_id),
        )

    a_slug = (row_a["page_path"] or "").rsplit("/", 1)[-1].rsplit(".", 1)[0] or row_a["key"]
    warned = False
    if row_b["page_path"]:
        warned = _prepend_warning(
            str(row_b["page_path"]),
            f"> [!warning] 已并入 [[entities/{a_slug}|{a_name}]]（{now_iso()[:10]}）",
        )
    database.event("ontology.merge", {
        "kind": kind, "a": a_id, "b": b_id, "by": by, "reason": reason,
        "edges_moved": moved, "edges_collapsed": collapsed, "self_loops": dropped,
        "page_warned": warned,
    })
    left = 0
    with database.connect() as c:
        left = c.execute(
            "SELECT COUNT(*) FROM edges WHERE (src_type=? AND src_id=?) OR (dst_type=? AND dst_id=?)",
            (kind, b_id, kind, b_id),
        ).fetchone()[0]
    return {"changed": True, "a": a_id, "b": b_id, "a_name": a_name,
            "b_name": str(row_b["name"]), "aliases": len(alias_set),
            "edges_moved": moved, "edges_collapsed": collapsed, "self_loops": dropped,
            "edges_to_merged": int(left), "src_count": int(src_count),
            "status": status, "page_warned": warned}


def review(database: Database | None = None, kind: str = "entity", ident: int | str = 0,
           *, action: str = "confirm", by: str = "human") -> dict[str, Any]:
    """人工审阅（A4/FR-505）：confirmed / rejected；rejected 保留行、从图与检索隐藏。"""
    database = database or db()
    if kind != "entity":
        raise ValueError("M1 只支持实体审阅")
    if action not in ("confirm", "reject"):
        raise ValueError("action 只能是 confirm|reject")
    row = database.object_row(kind, ident)
    if not row:
        raise ValueError(f"{kind} {ident} 不存在")
    if row["locked"]:
        raise ValueError("锁定条目请先 unlock 再审阅")
    if row["merged_into"]:
        raise ValueError("已归并条目不能再审阅")
    status = "confirmed" if action == "confirm" else "rejected"
    with database.tx() as c:
        c.execute("UPDATE entities SET status=?, updated_at=? WHERE id=?",
                  (status, now_iso(), int(row["id"])))
    database.event("ontology.review", {"kind": kind, "id": int(row["id"]),
                                       "action": action, "by": by})
    return {"id": int(row["id"]), "name": row["name"], "status": status,
            "changed": row["status"] != status}


def relock(database: Database | None = None, *, apply: bool = False) -> dict[str, Any]:
    """从页面 frontmatter 恢复锁定态（FR-511）：重建后/手工改页后跑一次。

    页面上的 `locked: true` 是锁定态的第二份持久化；数据库丢了也能按页恢复。
    """
    from .rebuild import _split_frontmatter
    from .vault import Vault

    database = database or db()
    vault = Vault()
    found = changed = missing = 0
    for kind, folder, table in (("entity", "entities", "entities"),
                                ("concept", "concepts", "concepts")):
        for p in sorted((vault.wiki / folder).glob("*.md")):
            fm, _ = _split_frontmatter(p.read_text(encoding="utf-8", errors="ignore"))
            if str(fm.get("locked", "")).strip().lower() not in ("true", "1", "yes"):
                continue
            rel = p.relative_to(vault.root).as_posix()
            with database.connect() as c:
                row = c.execute(f"SELECT * FROM {table} WHERE page_path=?", (rel,)).fetchone()
            if not row:
                missing += 1
                continue
            found += 1
            if not row["locked"]:
                changed += 1
                if apply:
                    database.set_locked(kind, int(row["id"]), True,
                                        by=str(fm.get("locked_by") or "relock"))
                    database.event("ontology.relock", {
                        "kind": kind, "id": int(row["id"]), "page": rel})
    return {"found": found, "changed": changed,
            "missing_rows": missing, "apply": bool(apply)}
