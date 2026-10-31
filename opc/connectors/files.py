"""本地文件连接器：把 inbox / 任意目录里的文档吸进中枢。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..capture import capture
from ..util import read_text_any, sha
from .base import Connector, Status


class FilesConnector(Connector):
    name = "files"
    kind = "fs"

    @property
    def watch_dirs(self) -> list[Path]:
        raw = self.conf.get("watch_dirs") or []
        if isinstance(raw, str):
            raw = [raw]
        return [Path(p) for p in raw if p]

    @property
    def extensions(self) -> set[str]:
        return {e.lower() for e in (self.conf.get("extensions") or [".md", ".txt", ".pdf"])}

    def candidates(self, root: str | Path | None = None) -> list[Path]:
        roots = [Path(root)] if root else self.watch_dirs
        out: list[Path] = []
        for r in roots:
            if r.is_file():
                out.append(r)
                continue
            if not r.exists():
                continue
            for p in r.rglob("*"):
                if p.is_file() and p.suffix.lower() in self.extensions and not _skip(p):
                    out.append(p)
        return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)

    def ingest(self, path: str | Path, publish: bool | None = None) -> dict[str, Any]:
        p = Path(path).resolve()
        text = read_text_any(p)
        if not text.strip() or text.startswith(("[读取失败", "[PDF 读取失败")):
            raise ValueError(f"无法提取文本：{p.name}")
        st = p.stat()
        return capture(
            kind="file",
            external_id=f"{p}|{int(st.st_mtime)}|{st.st_size}",
            title=p.stem,
            text=text,
            uri=str(p),
            occurred_at=None,
            meta={"path": str(p), "size": st.st_size, "suffix": p.suffix},
            publish=publish,
        )

    def scan(self, root: str | Path | None = None, limit: int = 30,
             publish: bool | None = None) -> list[dict[str, Any]]:
        results = []
        for p in self.candidates(root)[:limit]:
            try:
                results.append({"path": str(p), **self.ingest(p, publish=publish)})
            except Exception as exc:  # noqa: BLE001
                results.append({"path": str(p), "error": str(exc)})
        return results

    def status(self) -> Status:
        if not self.enabled:
            return Status(self.name, self.kind, False, False, "未启用")
        files = self.candidates()
        return Status(
            self.name, self.kind, True, True,
            f"{len(self.watch_dirs)} 个监听目录 · {len(files)} 个待处理文档",
            {"dirs": [str(d) for d in self.watch_dirs], "count": len(files)},
        )

    def capabilities(self) -> list[str]:
        return ["scan_dir", "ingest_file"]


def _skip(p: Path) -> bool:
    parts = {x.lower() for x in p.parts}
    return bool(parts & {".git", "node_modules", ".obsidian", "__pycache__", ".venv"}) or p.name.startswith("~$")
