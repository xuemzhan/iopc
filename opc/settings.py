"""语义检索的运行期开关：直接改 config/opc.toml 里的 [search].semantic。

只做一行替换 / 插入，保留注释与其余内容；改完重置 cfg 缓存，立即生效。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import CONFIG_PATH, cfg, reset_config

SECTION = "[search]"
LINE_RE = re.compile(r"^(\s*)semantic\s*=\s*(true|false)\s*(#.*)?$", re.I)


def _config_file() -> Path:
    if CONFIG_PATH.exists():
        return CONFIG_PATH
    example = CONFIG_PATH.with_name(CONFIG_PATH.name + ".example")
    return example if example.exists() else CONFIG_PATH


def set_semantic(on: bool) -> dict[str, Any]:
    path = _config_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    value = "true" if on else "false"

    start = next((i for i, ln in enumerate(lines) if ln.strip() == SECTION), None)
    if start is None:
        raise ValueError("配置文件里找不到 [search] 节，未修改")
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip().startswith("[") and lines[i].strip().endswith("]")),
               len(lines))

    hits = [i for i in range(start + 1, end) if LINE_RE.match(lines[i])]
    if hits:
        # 保留行尾注释
        matched = LINE_RE.match(lines[hits[0]])
        comment = (matched.group(3) or "").strip() if matched else ""
        lines[hits[0]] = f"semantic = {value}" + (f"  {comment}" if comment else "")
        for extra in reversed(hits[1:]):
            del lines[extra]
    else:
        lines.insert(start + 1, f"semantic = {value}")

    from .util import atomic_write

    atomic_write(path, "\n".join(lines) + "\n")
    reset_config()
    return status()


def status() -> dict[str, Any]:
    from .db import db
    from .llm import llm
    from .semantic import _note_rows, enabled

    database = db()
    model = llm("embed").model
    ready = True
    hint = ""
    try:
        if not llm("embed").api_key():
            ready = False
            hint = "未配置 embedding 的 API key"
    except Exception as exc:  # noqa: BLE001
        ready = False
        hint = f"{type(exc).__name__}: {exc}"

    notes = len(_note_rows(database))
    vectors = len(database.embedding_ids(model))
    return {
        "enabled": enabled(),
        "model": model,
        "provider": cfg().get("llm.routes.embed", "embed"),
        "ready": ready,
        "hint": hint,
        "notes": notes,
        "vectors": vectors,
        "pending": max(0, notes - vectors),
    }
