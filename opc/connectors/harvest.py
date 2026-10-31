"""AI 会话采集器：把 OpenCode / PI-Desktop / WorkBuddy 里「已经发生的思考」吸回中枢。

三者都只读打开，绝不回写；采集时截断到最近若干轮，避免把 token 烧在陈年对话上。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..capture import capture
from ..util import now_iso
from .base import Connector, Status


def _ro(path: str | Path) -> sqlite3.Connection:
    p = str(path).replace("\\", "/")
    if not Path(path).exists():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


class OpenCodeConnector(Connector):
    name = "opencode"
    kind = "sqlite"

    def _db(self) -> sqlite3.Connection:
        return _ro(self.conf.get("db") or "")

    def sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._db() as c:
            rows = c.execute(
                "SELECT s.id, s.title, s.directory, s.project_id,"
                " (SELECT MAX(p.time_created) FROM part p WHERE p.session_id=s.id) AS last_at,"
                " (SELECT COUNT(*) FROM part p WHERE p.session_id=s.id) AS parts"
                " FROM session s ORDER BY last_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def render(self, session_id: str, max_chars: int = 30000) -> tuple[str, str]:
        with self._db() as c:
            srow = c.execute("SELECT title, directory FROM session WHERE id=?", (session_id,)).fetchone()
            rows = c.execute(
                "SELECT p.data AS pdata, m.data AS mdata, p.time_created"
                " FROM part p JOIN message m ON m.id = p.message_id"
                " WHERE p.session_id=? ORDER BY p.time_created DESC LIMIT 400",
                (session_id,),
            ).fetchall()
        title = (srow["title"] if srow else "") or session_id
        lines: list[str] = [f"# {title}", "", f"工作目录：{srow['directory'] if srow else ''}", ""]
        for r in rows:
            try:
                pd = json.loads(r["pdata"])
                md = json.loads(r["mdata"])
            except Exception:
                continue
            if pd.get("type") != "text" or not pd.get("text"):
                continue
            role = md.get("role", "?")
            text = str(pd["text"]).strip()
            if not text:
                continue
            lines.append(f"### {role}\n{text}\n")
        body = "\n".join(lines)
        if len(body) > max_chars:
            body = body[:max_chars] + "\n…（已截断）"
        return title, body

    def harvest(self, limit: int = 3, publish: bool | None = None) -> list[dict[str, Any]]:
        out = []
        for s in self.sessions(limit):
            try:
                title, body = self.render(s["id"])
                if len(body) < 200:
                    continue
                out.append(capture(
                    kind="chat",
                    external_id=f"opencode:{s['id']}",
                    title=f"[OpenCode] {title}",
                    text=body,
                    uri=f"opencode://session/{s['id']}",
                    occurred_at=now_iso(),
                    meta={"tool": "opencode", "directory": s.get("directory"), "parts": s["parts"]},
                    publish=publish,
                ))
            except Exception as exc:  # noqa: BLE001
                out.append({"error": str(exc), "session": s["id"]})
        return out

    def status(self) -> Status:
        if not self.enabled:
            return Status(self.name, self.kind, False, False, "未启用")
        try:
            ss = self.sessions(limit=1000)
            return Status(self.name, self.kind, True, True, f"可读 · {len(ss)} 个会话",
                          {"db": self.conf.get("db"), "recent": ss[:3]})
        except Exception as exc:  # noqa: BLE001
            return Status(self.name, self.kind, True, False, f"不可读：{exc}")

    def capabilities(self) -> list[str]:
        return ["list_sessions", "harvest_session"]


class PiConnector(Connector):
    name = "pi_desktop"
    kind = "sqlite"

    def _db(self) -> sqlite3.Connection:
        return _ro(self.conf.get("db") or "")

    def sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._db() as c:
            rows = c.execute(
                "SELECT s.id, s.title, s.project_id,"
                " (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS msgs"
                " FROM sessions s WHERE s.deleted_at IS NULL ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def render(self, session_id: str, max_chars: int = 30000) -> tuple[str, str]:
        with self._db() as c:
            srow = c.execute("SELECT title FROM sessions WHERE id=?", (session_id,)).fetchone()
            rows = c.execute(
                "SELECT role, text FROM messages WHERE session_id=? AND text IS NOT NULL"
                " ORDER BY seq LIMIT 400",
                (session_id,),
            ).fetchall()
        title = (srow["title"] if srow else "") or session_id
        lines = [f"# {title}", ""]
        for r in rows:
            if r["text"] and str(r["text"]).strip():
                lines.append(f"### {r['role']}\n{str(r['text']).strip()}\n")
        body = "\n".join(lines)
        return title, body[:max_chars]

    def harvest(self, limit: int = 3, publish: bool | None = None) -> list[dict[str, Any]]:
        out = []
        for s in self.sessions(limit):
            try:
                title, body = self.render(s["id"])
                if len(body) < 200:
                    continue
                out.append(capture(
                    kind="chat", external_id=f"pi:{s['id']}", title=f"[PI] {title}", text=body,
                    uri=f"pi://session/{s['id']}", meta={"tool": "pi_desktop"}, publish=publish,
                ))
            except Exception as exc:  # noqa: BLE001
                out.append({"error": str(exc), "session": s["id"]})
        return out

    def status(self) -> Status:
        if not self.enabled:
            return Status(self.name, self.kind, False, False, "未启用")
        try:
            ss = self.sessions(limit=500)
            return Status(self.name, self.kind, True, True, f"可读 · {len(ss)} 个会话",
                          {"db": self.conf.get("db"), "recent": ss[:3]})
        except Exception as exc:  # noqa: BLE001
            return Status(self.name, self.kind, True, False, f"不可读：{exc}")

    def capabilities(self) -> list[str]:
        return ["list_sessions", "harvest_session"]


class WorkBuddyConnector(Connector):
    name = "workbuddy"
    kind = "fs"

    @property
    def home(self) -> Path:
        return Path(self.conf.get("home") or "")

    def memory_files(self) -> list[Path]:
        mem = self.home / "memory"
        return sorted(mem.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if mem.exists() else []

    def harvest(self, limit: int = 3, publish: bool | None = None) -> list[dict[str, Any]]:
        out = []
        for p in self.memory_files()[:limit]:
            text = p.read_text(encoding="utf-8", errors="ignore")
            if len(text.strip()) < 40:
                continue
            out.append(capture(
                kind="note", external_id=f"workbuddy:{p.name}",
                title=f"[WorkBuddy] {p.stem}", text=text, uri=str(p),
                meta={"tool": "workbuddy"}, publish=publish,
            ))
        return out

    def status(self) -> Status:
        if not self.enabled:
            return Status(self.name, self.kind, False, False, "未启用")
        try:
            mem = self.memory_files()
            arts = list((self.home / "artifact-index").glob("*")) if (self.home / "artifact-index").exists() else []
            return Status(self.name, self.kind, True, True,
                          f"可读 · 记忆 {len(mem)} 份 · 产物索引 {len(arts)} 项",
                          {"home": str(self.home), "memory": [p.name for p in mem[:3]]})
        except Exception as exc:  # noqa: BLE001
            return Status(self.name, self.kind, True, False, f"不可读：{exc}")

    def capabilities(self) -> list[str]:
        return ["harvest_memory"]
