"""Obsidian 连接器：既是知识落点，也是可反查的既有知识源。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..capture import capture
from ..config import cfg
from ..util import read_text_any
from ..vault import Vault
from .base import Connector, Status


class ObsidianConnector(Connector):
    name = "obsidian"
    kind = "vault"

    def vault(self) -> Vault:
        p = self.conf.get("path") or cfg().get("vault.path")
        return Vault(Path(p))

    def status(self) -> Status:
        if not self.enabled:
            return Status(self.name, self.kind, False, False, "未启用")
        try:
            v = self.vault()
            pages = list(v.wiki.rglob("*.md"))
            inbox = list(v.inbox.rglob("*.md")) if v.inbox.exists() else []
            canvases = list(v.root.rglob("*.canvas"))
            plugins = [p.name for p in (v.root / ".obsidian" / "plugins").glob("*") if p.is_dir()]
            raw = list((v.root / "raw").rglob("*")) if (v.root / "raw").exists() else []
            return Status(
                self.name, self.kind, True, True,
                f"库里 {len(pages)} 个 wiki 页 · {len(canvases)} 张画布 · {len(plugins)} 个插件",
                {
                    "path": str(v.root),
                    "wiki_pages": len(pages),
                    "inbox_captures": len(inbox),
                    "raw_files": len(raw),
                    "canvases": len(canvases),
                    "has_smart_connections": "smart-connections" in plugins,
                    "has_karpathywiki": "karpathywiki" in plugins,
                    "has_markmind": "obsidian-markmind" in plugins,
                    "has_local_rest_api": "obsidian-local-rest-api" in plugins,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return Status(self.name, self.kind, True, False, f"不可用：{exc}")

    def open_uri(self, rel_path: str) -> str:
        v = self.vault()
        return f"obsidian://open?vault={v.root.name}&file={rel_path}"

    def raw_candidates(self, limit: int = 50) -> list[Path]:
        v = self.vault()
        raw = v.root / "raw"
        if not raw.exists():
            return []
        exts = {".md", ".txt", ".pdf", ".docx", ".pptx", ".html"}
        files = [p for p in raw.rglob("*") if p.is_file() and p.suffix.lower() in exts]
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]

    def ingest_raw(self, limit: int = 5, publish: bool | None = None) -> list[dict[str, Any]]:
        out = []
        for p in self.raw_candidates(limit):
            text = read_text_any(p)
            if not text.strip() or text.startswith(("[读取失败", "[PDF 读取失败")):
                continue
            st = p.stat()
            out.append(
                capture(
                    kind="file",
                    external_id=f"vault-raw|{p}",
                    title=p.stem,
                    text=text,
                    uri=str(p),
                    meta={"origin": "obsidian-raw", "path": str(p)},
                    publish=publish,
                )
            )
        return out

    def capabilities(self) -> list[str]:
        return ["open_note", "ingest_raw", "canvas", "mindmap"]
