"""注记系统（P0-F）：工作中随手记要点/疑问，带上下文锚点。

语义：
- 不触发 LLM，秒级落库（区别于 /api/capture 的全量提炼速记）；
- 锚点自动探测：iMeeting 正在录制 → 会议 + 最近转写段时间戳；
- 回应（reply）→ 父注记自动 answered + focused =「我关注的重点」；
- 会议入库时注记并进会议正文，随提炼写入知识页，建立链接。
"""
from __future__ import annotations

from typing import Any

from .db import Database, db

VALID_KINDS = ("note", "question", "reply")


def _connector():
    from .connectors import get as get_connector

    try:
        return get_connector("imeeting")
    except Exception:
        return None


def _fmt_ms(ms: Any) -> str:
    try:
        total = int(ms) // 1000
    except Exception:
        return ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def current_anchor(database: Database) -> dict[str, Any]:
    """探测当前最可能的锚点：录制中会议（取最近转写段时间戳）。

    探测失败一律安静返回 {anchor_kind: 'none'}，绝不阻塞输入（A2）。
    """
    conn = _connector()
    if conn is None or not getattr(conn, "enabled", True):
        return {"anchor_kind": "none"}
    try:
        # 短超时：iMeeting 没起/卡住时不能让「记一条」等半分钟
        meetings = [m for m in conn.list_meetings(limit=5, timeout=1.0)
                    if m.get("status") == "recording"]
        if not meetings:
            return {"anchor_kind": "none"}
        m = meetings[0]
        mid = str(m.get("id"))
        pos = ""
        try:
            full = conn.meeting(int(mid), timeout=1.0)
            segs = [s for s in (full.get("segments") or []) if isinstance(s, dict)]
            if segs:
                ms = segs[-1].get("end_ms") or segs[-1].get("start_ms") or 0
                pos = _fmt_ms(ms)
        except Exception:
            pass
        sid = _meeting_source_id(database, mid)
        return {
            "anchor_kind": "meeting", "anchor_ref": mid, "anchor_pos": pos,
            "anchor_source_id": sid,
            "anchor_title": (m.get("title") or f"会议 {mid}").strip()[:60],
        }
    except Exception:  # noqa: BLE001 — 探测永不阻塞
        return {"anchor_kind": "none"}


def _meeting_source_id(database: Database, meeting_id: str) -> int | None:
    """该会议若已入库，拿到 sources.id（知识链接的实体端）。"""
    with database.connect() as c:
        try:
            row = c.execute(
                "SELECT id FROM sources WHERE kind='meeting' AND external_id=?",
                (meeting_id,),
            ).fetchone()
        except Exception:
            return None
        return int(row["id"]) if row else None


def add_note(
    database: Database, text: str, *, kind: str = "note", auto_anchor: bool = True,
    anchor_kind: str | None = None, anchor_ref: str | None = None,
    anchor_pos: str | None = None, anchor_source_id: int | None = None,
    anchor_title: str | None = None, context: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """新增注记。未显式给锚点时自动探测；给 anchor_source_id 时自动补标题/兜底 ref。

    context 记「当时的其他工作」（前台窗口标题等），一条注记因此同时挂在
    时间轴（会议 timestamp）和当时在做的事上。
    """
    database = database or db()
    if kind not in VALID_KINDS:
        raise ValueError(f"kind 只支持 {'/'.join(VALID_KINDS)}，收到 {kind}")
    meta = dict(meta or {})
    if context and context.strip():
        meta["ctx"] = context.strip()[:200]
    manual = any(v not in (None, "") for v in
                 (anchor_kind, anchor_ref, anchor_pos, anchor_source_id, anchor_title))
    a: dict[str, Any] = {"anchor_kind": anchor_kind, "anchor_ref": anchor_ref,
                         "anchor_pos": anchor_pos,
                         "anchor_source_id": anchor_source_id or None,
                         "anchor_title": anchor_title}
    if not manual:
        a.update(current_anchor(database))
    elif a.get("anchor_source_id") and not a.get("anchor_title"):
        # 手动给了来源：补来源标题（视频/论文/文件的展示名）
        with database.connect() as c:
            row = c.execute("SELECT title, kind FROM sources WHERE id=?",
                            (a["anchor_source_id"],)).fetchone()
        if row:
            a["anchor_title"] = f"{row['title']}（{row['kind']}）"[:70]
            if not a.get("anchor_kind"):
                a["anchor_kind"], a["anchor_ref"] = "source", str(a["anchor_source_id"])
    aid = database.add_annotation(
        text, kind=kind, anchor_kind=a.get("anchor_kind") or "none",
        anchor_ref=a.get("anchor_ref"), anchor_pos=a.get("anchor_pos"),
        anchor_source_id=a.get("anchor_source_id"),
        anchor_title=a.get("anchor_title"), meta=meta,
    )
    database.event("annotate", {"id": aid, "kind": kind,
                                "anchor_kind": a.get("anchor_kind") or "none",
                                "anchor_ref": a.get("anchor_ref")})
    return database.annotation(aid)  # type: ignore[return-value]


def reply(database: Database, parent_id: int, text: str) -> dict[str, Any]:
    """回应：子注记 + 父注记 answered+focused（「有人回答/我主动输入 → 重点」）。"""
    database = database or db()
    rid, parent = database.reply_annotation(parent_id, text)
    database.event("annotate.reply", {"id": rid, "for": parent_id})
    return parent


def focus(database: Database, aid: int, on: bool) -> dict[str, Any]:
    database = database or db()
    database.set_annotation_focus(aid, on)
    database.event("annotate.focus", {"id": aid, "on": on})
    return database.annotation(aid)  # type: ignore[return-value]


def archive(database: Database, aid: int) -> dict[str, Any]:
    database = database or db()
    database.archive_annotation(aid)
    database.event("annotate.archive", {"id": aid})
    return database.annotation(aid)  # type: ignore[return-value]


def meeting_notes(database: Database, meeting_id: int | str) -> list[dict[str, Any]]:
    """某会议的全部有效注记（父注记 + 回应），按 id 升序。"""
    database = database or db()
    items = database.list_annotations(anchor=f"meeting:{meeting_id}", scope="recent", limit=200)
    keep = [x for x in items if x.get("focused") or x.get("status") in ("open", "answered")]
    return sorted(keep, key=lambda x: int(x["id"]))


def render_for_meeting(database: Database, meeting_id: int | str,
                       meeting_title: str | None = None) -> str:
    """会议入库文本注入块：有注记才返回非空（含 timestamp 与回应）。"""
    notes = meeting_notes(database, meeting_id)
    if not notes:
        return ""
    del meeting_title  # 仅预留扩展
    replies: dict[int, list[dict[str, Any]]] = {}
    top: list[dict[str, Any]] = []
    for n in notes:
        (replies.setdefault(n["parent_id"], []) if n.get("parent_id") else top).append(n)
    lines = ["", "## 我的注记", ""]
    for a in top:
        pos = a.get("anchor_pos") or ""
        flag = " ⭐" if a.get("focused") else ""
        tag = "❓" if a["kind"] == "question" else "📝"
        ctx = (a.get("meta") or {}).get("ctx") or ""
        ctx_txt = f"（同时：{ctx}）" if ctx else ""
        lines.append(f"- {tag} [{pos}] {a['text']}{flag}{ctx_txt}")
        for r in replies.get(a["id"], []):
            lines.append(f"  ↳ {r['text']}")
    return "\n".join(lines).strip()


def meeting_stats(database: Database, meeting_id: int | str) -> dict[str, int]:
    """注记聚合：open/answered/focused/replies（给 UI 与日报用）。"""
    database = database or db()
    items = database.list_annotations(anchor=f"meeting:{meeting_id}", scope="recent", limit=200)
    out = {"open": 0, "answered": 0, "focused": 0, "replies": 0}
    for x in items:
        if x["kind"] == "reply":
            out["replies"] += 1
            continue
        if x["status"] in out and x["status"] in ("open", "answered"):
            out[x["status"]] += 1
        if x.get("focused"):
            out["focused"] += 1
    return out
