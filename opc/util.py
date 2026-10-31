"""通用工具：slug、时间、哈希、分块、原子写。"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_TS_FMT = "%Y-%m-%dT%H:%M:%S%z"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime(_TS_FMT)


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def sha(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:n]


def normalize_key(name: str) -> str:
    """实体/概念归并用的归一化键：去空白、标点、大小写。"""
    s = unicodedata.normalize("NFKC", name or "").strip().lower()
    s = re.sub(r"[\s\-_·•/\\|,，。.、:：;；'\"“”‘’()（）\[\]【】{}]+", "", s)
    return s


_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def slugify(name: str, max_len: int = 60) -> str:
    s = unicodedata.normalize("NFKC", name or "").strip()
    s = _BAD.sub("", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    if not s:
        s = "untitled"
    return s[:max_len].rstrip("-.")


def ensure_unique(slug: str, taken: Iterable[str]) -> str:
    taken = set(taken)
    if slug not in taken:
        return slug
    i = 2
    while f"{slug}-{i}" in taken:
        i += 1
    return f"{slug}-{i}"


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def from_json(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def chunk_text(text: str, size: int = 9000, overlap: int = 400) -> list[str]:
    text = text or ""
    if len(text) <= size:
        return [text] if text.strip() else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # 尽量在段落/句号处断开
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("。"), window.rfind("\n"))
            if cut > size * 0.55:
                end = start + cut + 1
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def read_text_any(path: Path) -> str:
    """多格式取文本：md/txt/html/pdf/docx/pptx/xlsx。失败返回空串。"""
    suffix = path.suffix.lower()
    try:
        if suffix in {".md", ".txt", ".csv", ".json", ".log", ".srt", ".vtt"}:
            for enc in ("utf-8", "utf-8-sig", "gb18030", "utf-16"):
                try:
                    return path.read_text(encoding=enc)
                except UnicodeDecodeError:
                    continue
            return path.read_text(encoding="utf-8", errors="ignore")
        if suffix in {".html", ".htm"}:
            return html_to_text(path.read_text(encoding="utf-8", errors="ignore"))
        if suffix == ".pdf":
            return _pdf_text(path)
        if suffix == ".docx":
            import docx  # type: ignore

            d = docx.Document(str(path))
            parts = [p.text for p in d.paragraphs if p.text.strip()]
            for t in d.tables:
                for row in t.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
            return "\n".join(parts)
        if suffix == ".pptx":
            from pptx import Presentation  # type: ignore

            prs = Presentation(str(path))
            parts = []
            for i, slide in enumerate(prs.slides, 1):
                parts.append(f"\n## Slide {i}")
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        txt = shape.text_frame.text.strip()
                        if txt:
                            parts.append(txt)
                if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                    parts.append("[备注] " + slide.notes_slide.notes_text_frame.text.strip())
            return "\n".join(parts)
        if suffix == ".xlsx":
            import openpyxl  # type: ignore

            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets:
                parts.append(f"\n## Sheet {ws.title}")
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) for c in row if c is not None]
                    if cells:
                        parts.append(" | ".join(cells))
            return "\n".join(parts)
    except Exception as exc:  # 读失败不致命
        return f"[读取失败 {path.name}: {exc}]"
    return ""


def _pdf_text(path: Path) -> str:
    try:
        import fitz  # type: ignore

        doc = fitz.open(str(path))
        return "\n".join(page.get_text() for page in doc)
    except Exception:
        try:
            from pypdf import PdfReader  # type: ignore

            return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
        except Exception as exc:
            return f"[PDF 读取失败: {exc}]"


def html_to_text(html: str) -> str:
    """用 lxml 抽取正文：去脚本样式，保留块级换行。"""
    try:
        from lxml import html as lhtml

        tree = lhtml.fromstring(html)
        for bad in tree.xpath("//script|//style|//noscript|//svg"):
            bad.getparent().remove(bad)
        title = (tree.xpath("//title/text()") or [""])[0].strip()
        nodes = tree.xpath("//article|//main|//body")
        body = nodes[0] if nodes else tree
        text = body.text_content()
        lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in text.splitlines()]
        lines = [ln for ln in lines if ln]
        out = "\n".join(lines)
        out = re.sub(r"\n{3,}", "\n\n", out)
        return (f"# {title}\n\n{out}" if title else out)[:200_000]
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
