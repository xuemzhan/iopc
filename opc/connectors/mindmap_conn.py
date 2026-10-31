"""思维导图连接器：把「导图」当成一种可读写的产物，而不是某个软件的私有格式。

落点：Obsidian Canvas（默认，跨工具）· iMeeting 自动导图 · XMind（若安装）· Markmap。
"""
from __future__ import annotations

from pathlib import Path

from ..config import cfg
from ..vault import Vault
from .base import Connector, Status

_XMIND_HINTS = [
    "C:/Program Files/XMind/XMind.exe",
    "C:/Program Files (x86)/XMind/XMind.exe",
    "D:/Program Files/XMind/XMind.exe",
]


class MindmapConnector(Connector):
    name = "mindmap"
    kind = "files"

    def xmind_path(self) -> str:
        for p in _XMIND_HINTS:
            if Path(p).exists():
                return p
        return ""

    def canvas_count(self) -> int:
        try:
            return len(list(Vault().wiki.rglob("*.canvas")))
        except Exception:
            return 0

    def status(self) -> Status:
        if not self.enabled:
            return Status(self.name, self.kind, False, False, "未启用")
        n = self.canvas_count()
        xmind = self.xmind_path()
        return Status(
            self.name, self.kind, True, True,
            f"Obsidian Canvas {n} 张 · iMeeting 自动导图可用"
            + (" · XMind 已安装" if xmind else " · XMind 未安装（可选）"),
            {"canvas": n, "xmind": xmind,
             "output": str(Vault().wiki / "synthetic") if cfg().get("vault.path") else ""},
        )

    def capabilities(self) -> list[str]:
        return ["export_canvas", "meeting_mindmap", "xmind"]
