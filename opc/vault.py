"""Obsidian 写入器：严格遵循库内 CLAUDE.md 的 LLM Wiki 契约。

raw/      只读原文，永不修改
inbox/    OPC 新增的原始捕获（只增）
wiki/     提炼产物：sources/ entities/ concepts/ synthetic/
_index.md 目录，追加式更新
_log.md   追加式操作日志
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from .config import cfg
from .util import atomic_write, ensure_unique, slugify, today

# PATCH 5：只写库里已经存在的二级标题，绝不新增；synthetic 不再进索引
INDEX_SECTION = {
    "source": ("## 来源", "## Sources"),
    "entity": ("## 实体", "## Entities"),
    "concept": ("## 概念", "## Concepts"),
}


class Vault:
    def __init__(self, root: Path | str | None = None):
        c = cfg()
        self.root = Path(root) if root else c.vault_path
        self.wiki = self.root / c.get("vault.wiki", "wiki")
        self.inbox = self.root / c.get("vault.inbox", "inbox")
        self.protect = bool(c.get("vault.protect_existing", True))
        for sub in ("sources", "entities", "concepts", "synthetic"):
            (self.wiki / sub).mkdir(parents=True, exist_ok=True)

    # ---------- 基础 ----------
    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def taken_slugs(self, kind: str) -> set[str]:
        folder = {"source": "sources", "entity": "entities", "concept": "concepts", "synthetic": "synthetic"}[kind]
        d = self.wiki / folder
        if not d.exists():
            return set()
        return {p.stem for p in d.glob("*.md")}

    def unique_slug(self, kind: str, name: str) -> str:
        base = slugify(name)
        return ensure_unique(base, self.taken_slugs(kind))

    def page_path(self, kind: str, slug: str) -> Path:
        folder = {"source": "sources", "entity": "entities", "concept": "concepts", "synthetic": "synthetic"}[kind]
        return self.wiki / folder / f"{slug}.md"

    # ---------- 原始捕获 ----------
    def write_inbox(self, title: str, text: str, meta_line: str = "") -> Path:
        if not cfg().get("vault.write_raw", True):
            raise RuntimeError("vault.write_raw=false")
        day = self.inbox / today()
        slug = ensure_unique(slugify(title, 50), {p.stem for p in day.glob("*.md")} if day.exists() else set())
        path = day / f"{slug}.md"
        body = f"# {title}\n\n> {meta_line}\n\n{text}\n"
        atomic_write(path, body)
        return path

    # ---------- 页面 ----------
    def write_source_page(
        self,
        *,
        slug: str,
        title: str,
        summary: str,
        body: str,
        tags: list[str],
        source_refs: list[str],
        meta: dict[str, Any],
    ) -> Path:
        path = self.page_path("source", slug)
        fm = _frontmatter(
            title=title,
            type_="source",
            tags=tags,
            sources=source_refs,
            extra={"kind": meta.get("kind", ""), "opc_source_id": meta.get("source_id", ""),
                   "content_hash": meta.get("content_hash", "")},
        )
        atomic_write(path, f"{fm}\n# {title}\n\n{summary}\n\n{body}\n")
        return path

    def safe_path(self, rel: str) -> Path | None:
        """把 DB 里的相对路径解析到库内；越界返回 None。"""
        if not rel:
            return None
        root = self.root.resolve()
        try:
            p = (self.root / rel).resolve()
        except OSError:
            return None
        return p if p.is_relative_to(root) else None

    def page_exists(self, kind: str, slug: str) -> bool:
        return self.page_path(kind, slug).exists()

    def ensure_entity_page(self, *, slug: str, name: str, type_: str, description: str,
                           aliases: list[str], source_link: str) -> Path:
        path = self.page_path("entity", slug)
        if path.exists():
            if self.protect:
                _append_link_once(path, source_link, section="## 关联来源")
                return path
        fm = _frontmatter(
            title=name, type_="entity", tags=[type_], sources=[source_link],
            extra={"entity_type": type_, "aliases": aliases},
        )
        body = (
            f"{fm}\n# {name}\n\n## Overview\n{description or '（待补充）'}\n\n"
            f"## Key Facts\n- \n\n## Related\n- {source_link}\n"
        )
        atomic_write(path, body)
        return path

    def ensure_concept_page(self, *, slug: str, name: str, definition: str,
                            aliases: list[str], source_link: str) -> Path:
        path = self.page_path("concept", slug)
        if path.exists():
            if self.protect:
                _append_link_once(path, source_link, section="## 关联来源")
                return path
        fm = _frontmatter(
            title=name, type_="concept", tags=["concept"], sources=[source_link],
            extra={"aliases": aliases},
        )
        body = (
            f"{fm}\n# {name}\n\n## Definition\n{definition or '（待补充）'}\n\n"
            f"## Key Principles\n1. \n\n## Applications\n- \n\n## Related\n- {source_link}\n"
        )
        atomic_write(path, body)
        return path

    # ---------- 索引 / 日志 ----------
    def index_add(self, kind: str, slug: str, title: str, aliases: Iterable[str], extra: str = "") -> None:
        if not cfg().get("ingest.index_enabled", True):
            return
        path = self.wiki / "_index.md"
        text = path.read_text(encoding="utf-8") if path.exists() else "# Wiki Index\n"
        link = f"[[{_folder(kind)}/{slug}|{title}]]"
        if link in text:
            return
        alias_txt = ", ".join([a for a in aliases if a])
        line = f"- {link}" + (f" `aliases: {alias_txt}`" if alias_txt else "")
        line += f" {extra}".rstrip() + "\n"
        headings = INDEX_SECTION.get(kind)
        if not headings:
            return  # synthetic 等不进索引
        heading = next((h for h in headings if h in text), None)
        if heading is None:
            return  # 标题不存在就跳过，不创建新节
        idx = text.index(heading)
        nxt = text.find("\n## ", idx + len(heading))
        if nxt == -1:
            head, tail = text.rstrip("\n"), "\n"
        else:
            head, tail = text[:nxt].rstrip("\n"), text[nxt:]
        text = head + "\n" + line + tail
        atomic_write(path, text)

    def log(self, lines: list[str]) -> None:
        path = self.wiki / "_log.md"
        text = path.read_text(encoding="utf-8") if path.exists() else "# Wiki Operation Log\n\n"
        text = text.rstrip()
        header = f"## {today()}"
        last = text.rfind("\n## ")
        same_day = last != -1 and text[last + 1:].splitlines()[0].strip() == header
        if not same_day:
            text += f"\n\n{header}"
        text += "\n" + "\n".join(f"- {ln}" for ln in lines) + "\n"
        atomic_write(path, text)


def _folder(kind: str) -> str:
    return {"source": "sources", "entity": "entities", "concept": "concepts", "synthetic": "synthetic"}[kind]


def _frontmatter(*, title: str, type_: str, tags: list[str], sources: list[str],
                 extra: dict[str, Any] | None = None) -> str:
    def q(s: str) -> str:
        return '"' + str(s).replace('"', "'") + '"'

    lines = ["---", f"title: {q(title)}", f"type: {type_}"]
    lines.append("tags: [" + ", ".join(t for t in tags if t) + "]")
    lines.append(f"created: {today()}")
    lines.append(f"updated: {today()}")
    if sources:
        lines.append("sources:")
        for s in sources:
            lines.append(f"  - {q(s)}")
    for k, v in (extra or {}).items():
        if isinstance(v, list):
            lines.append(f"{k}: [" + ", ".join(q(x) for x in v) + "]")
        else:
            lines.append(f"{k}: {q(v)}")
    lines.append("---")
    return "\n".join(lines)


def _append_link_once(path: Path, link: str, section: str) -> None:
    text = path.read_text(encoding="utf-8")
    if link in text:
        return
    if section in text:
        text = text.rstrip() + f"\n- {link}\n"
    else:
        text = text.rstrip() + f"\n\n{section}\n- {link}\n"
    atomic_write(path, text)


def wikilink(kind: str, slug: str, label: str | None = None) -> str:
    folder = _folder(kind)
    return f"[[{folder}/{slug}|{label}]]" if label else f"[[{folder}/{slug}]]"


_SENT_SPLIT = re.compile(r"(?<=[。！？.!?])\s*")


def bulletize(items: Iterable[str], limit: int = 20) -> str:
    out = []
    for it in list(items)[:limit]:
        it = (it or "").strip()
        if it:
            out.append(f"- {it}")
    return "\n".join(out)
