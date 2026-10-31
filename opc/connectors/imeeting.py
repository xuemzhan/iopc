"""iMeeting 连接器：本地会议转写服务（FastAPI，默认 127.0.0.1:8000）。

能力：列会议 / 取转写 / 取纪要 / 取思维导图 / 一键入库提炼 / 回写行动项。
"""
from __future__ import annotations

from typing import Any

import httpx

from ..capture import capture
from .base import Connector, Status

SPEAKER_UNKNOWN = "未标注"


def _fmt_ts(ms: Any) -> str:
    try:
        total = int(ms) // 1000
    except Exception:
        return ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class IMeetingConnector(Connector):
    name = "imeeting"
    kind = "http"

    @property
    def base(self) -> str:
        return (self.conf.get("base_url") or "http://127.0.0.1:8000").rstrip("/")

    def _headers(self) -> dict[str, str]:
        tok = self.conf.get("token") or ""
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    def _get(self, path: str, timeout: float = 30.0) -> Any:
        with httpx.Client(base_url=self.base + "/api/v1", timeout=timeout) as c:
            r = c.get(path, headers=self._headers())
            r.raise_for_status()
            return r.json()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        with httpx.Client(base_url=self.base + "/api/v1", timeout=30.0) as c:
            r = c.post(path, headers=self._headers(), json=body)
            r.raise_for_status()
            return r.json() if r.content else {}

    # ---------- 读 ----------
    def health(self) -> dict[str, Any]:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(self.base + "/api/v1/health")
            r.raise_for_status()
            return r.json()

    def list_meetings(self, limit: int = 20) -> list[dict[str, Any]]:
        data = self._get(f"/meetings?limit={int(limit)}")
        if isinstance(data, dict):
            for k in ("items", "meetings", "data"):
                if isinstance(data.get(k), list):
                    return data[k]
            return []
        return data or []

    def meeting(self, meeting_id: int) -> dict[str, Any]:
        return self._get(f"/meetings/{meeting_id}")

    def minutes(self, meeting_id: int) -> dict[str, Any] | None:
        try:
            return self._get(f"/meetings/{meeting_id}/minutes")
        except httpx.HTTPError:
            return None

    def mindmap(self, meeting_id: int) -> dict[str, Any] | None:
        try:
            return self._get(f"/meetings/{meeting_id}/mindmap")
        except httpx.HTTPError:
            return None

    def actions(self, meeting_id: int) -> list[dict[str, Any]]:
        try:
            data = self._get(f"/meetings/{meeting_id}/actions")
            return data if isinstance(data, list) else data.get("items", [])
        except httpx.HTTPError:
            return []

    # ---------- 组装 ----------
    def render(self, meeting_id: int) -> dict[str, Any]:
        m = self.meeting(meeting_id)
        mins = self.minutes(meeting_id)
        mm = self.mindmap(meeting_id)
        acts = self.actions(meeting_id)
        title = (m.get("title") or f"会议 {meeting_id}").strip()

        lines: list[str] = [f"# {title}", ""]
        meta_bits = []
        if m.get("meeting_type"):
            meta_bits.append(f"类型：{m['meeting_type']}")
        if m.get("created_at"):
            meta_bits.append(f"开始：{m['created_at']}")
        if m.get("duration_ms"):
            meta_bits.append(f"时长：{int(m['duration_ms']) // 60000} 分钟")
        if m.get("participants"):
            meta_bits.append(f"参会：{m['participants']}")
        if meta_bits:
            lines += ["- " + "；".join(meta_bits), ""]
        if m.get("agenda"):
            lines += ["## 议题", str(m["agenda"]), ""]

        if mins and mins.get("summary"):
            lines += ["## 系统纪要", str(mins["summary"]), ""]
        if mins and isinstance(mins.get("topics"), list):
            lines.append("## 议题结论")
            for i, t in enumerate(mins["topics"], 1):
                if not isinstance(t, dict):
                    continue
                lines.append(f"### {i}. {t.get('title', '')}")
                if t.get("detail"):
                    lines.append(str(t["detail"]))
                if t.get("conclusion"):
                    lines.append(f"**结论**：{t['conclusion']}")
                lines.append("")
        if acts:
            lines += ["## 系统抽取的行动项"]
            for a in acts:
                if isinstance(a, dict):
                    txt = a.get("text") or a.get("content") or a.get("title") or ""
                    owner = a.get("owner") or a.get("assignee") or ""
                    lines.append(f"- {txt}" + (f"（{owner}）" if owner else ""))
            lines.append("")

        segs = m.get("segments") or []
        if segs:
            lines.append("## 会议转写")
            for s in segs:
                if not isinstance(s, dict):
                    continue
                ts = _fmt_ts(s.get("start_ms", s.get("start", 0)))
                spk = s.get("speaker") or s.get("speaker_name") or SPEAKER_UNKNOWN
                txt = (s.get("text") or s.get("content") or "").strip()
                if txt:
                    lines.append(f"[{ts}] {spk}：{txt}")
        if mm and mm.get("root"):
            lines += ["", "## 会议导图（大纲）"]
            lines += _outline(mm["root"], 0)

        text = "\n".join(lines).strip()
        occurred = m.get("created_at") or None
        return {
            "title": title,
            "text": text,
            "occurred_at": occurred,
            "meta": {
                "meeting_id": meeting_id,
                "duration_ms": m.get("duration_ms"),
                "segments": len(segs),
                "meeting_type": m.get("meeting_type"),
                "topics": len(mins.get("topics", [])) if mins else 0,
                "mindmap_version": mm.get("version") if mm else None,
            },
        }

    def ingest(self, meeting_id: int, publish: bool | None = None, force: bool = False) -> dict[str, Any]:
        r = self.render(meeting_id)
        return capture(
            kind="meeting",
            external_id=str(meeting_id),
            title=r["title"],
            text=r["text"],
            uri=f"{self.base}/api/v1/meetings/{meeting_id}",
            occurred_at=r["occurred_at"],
            meta=r["meta"],
            publish=publish,
            force=force,
        )

    def push_action(self, meeting_id: int, text: str, owner: str = "") -> dict[str, Any]:
        """把提炼出的行动项回写到 iMeeting（级联的「反向」一公里）。"""
        body: dict[str, Any] = {"text": text}
        if owner:
            body["owner"] = owner
        try:
            return self._post(f"/meetings/{meeting_id}/actions", body)
        except httpx.HTTPError as exc:
            return {"error": str(exc)}

    def status(self) -> Status:
        if not self.enabled:
            return Status(self.name, self.kind, False, False, "未启用")
        try:
            h = self.health()
            meetings = self.list_meetings(limit=5)
            running = [m for m in meetings if m.get("status") == "recording"]
            return Status(
                self.name, self.kind, True, True,
                f"在线 · 模型已加载 · 最近会议 {len(meetings)} 场"
                + (f" · 正在录制 {len(running)} 场" if running else ""),
                {"base_url": self.base, "active_sessions": h.get("active_sessions"),
                 "recent": [{"id": m.get("id"), "title": m.get("title"), "status": m.get("status")}
                            for m in meetings[:5]]},
            )
        except Exception as exc:  # noqa: BLE001
            return Status(self.name, self.kind, True, False, f"不可达：{exc}", {"base_url": self.base})

    def capabilities(self) -> list[str]:
        return ["list_meetings", "render_transcript", "ingest_meeting", "push_action", "mindmap"]


def _outline(node: Any, depth: int) -> list[str]:
    if not isinstance(node, dict) or depth > 4:
        return []
    out = ["  " * depth + f"- {node.get('label', '')}"]
    for ch in node.get("children") or []:
        out += _outline(ch, depth + 1)
    return out
