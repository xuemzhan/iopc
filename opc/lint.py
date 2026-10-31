"""知识库体检：孤儿页、断链、空壳、重复、矛盾。

对应 Obsidian 库 CLAUDE.md 里约定的 Lint 工作流，只是把它自动化了。
"""
from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .db import Database, db
from .llm import llm
from .util import now_iso, today
from .vault import Vault, _frontmatter, bulletize, wikilink

LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
CLAIMS_SECTION = "## 关键主张"

CONTRADICT_SYSTEM = """你是知识库的事实核查员。判断两段材料是否存在事实性冲突。
只输出 JSON：{"conflict": true/false, "reason": "≤40字说明", "severity": "high|medium|low"}
判定标准：
- 同一事实给出不同数字/时间/结论 → conflict
- 一个是总体表述、一个是细节补充 → 不算冲突
- 只是措辞不同 → 不算冲突
- 无法判断 → conflict=false, reason 说明信息不足"""


def _all_pages(vault: Vault) -> list[Path]:
    return [p for p in vault.wiki.rglob("*.md") if p.name not in ("_index.md", "_log.md")]


PLACEHOLDERS = {"xxx", "page-name", "...", "sources/...", "concepts/...", "entities/...",
                "页面名", "filename", "wikilinks", "link", "标题", "placeholder"}
UTILITY_STEMS = {"index", "log", "config", "readme", "claude.md", "agents.md"}


def _inbound_links(vault: Vault) -> tuple[dict[str, int], dict[Path, list[str]]]:
    """全库解析链接（含 raw/ 与 _index.md），并把占位链接排除掉。"""
    wiki_pages = _all_pages(vault)
    all_md = [p for p in vault.root.rglob("*.md")
              if p.name not in ("_index.md", "_log.md") and ".obsidian" not in p.parts]
    stems = {p.stem.lower(): p for p in all_md}
    inbound: dict[str, int] = defaultdict(int)
    broken: dict[Path, list[str]] = defaultdict(list)
    for p in all_md:
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for raw in LINK_RE.findall(text):
            target = raw.strip().split("/")[-1].strip().lower()
            if not target or "..." in target or target in PLACEHOLDERS:
                continue
            key = target[:-3] if target.endswith(".md") else target
            if key in stems:
                inbound[key] += 1
            elif p in wiki_pages:
                broken[p].append(raw.strip())
    return inbound, broken


def _claims(text: str, limit: int = 8) -> list[str]:
    out, in_sec = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            in_sec = line.strip() == CLAIMS_SECTION
            continue
        if in_sec and line.strip().startswith("- "):
            t = line.strip()[2:]
            t = re.split(r"（置信度", t)[0].strip()
            if t:
                out.append(t)
        if len(out) >= limit:
            break
    return out


def structural(vault: Vault) -> dict[str, Any]:
    pages = _all_pages(vault)
    inbound, broken = _inbound_links(vault)
    stems = {p.stem.lower(): p for p in pages}

    orphans = [p for p in pages
               if inbound.get(p.stem.lower(), 0) == 0 and p.stem.lower() not in UTILITY_STEMS]
    stubs = []
    for p in pages:
        if p.parent.name not in ("entities", "concepts"):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if "（待补充）" in text or re.search(r"## Key Facts\s*\n\s*-\s*\n", text):
            stubs.append(p)

    # 重复来源：标题归一后相同，或摘要高度相似
    by_title: dict[str, list[Path]] = defaultdict(list)
    for p in pages:
        if p.parent.name != "sources":
            continue
        norm = re.sub(r"[^\w\u4e00-\u9fff]", "", p.stem.lower())
        by_title[norm].append(p)
    dup_titles = {k: v for k, v in by_title.items() if len(v) > 1 and k}

    dup_summaries: list[tuple[Path, Path, float]] = []
    src_pages = [p for p in pages if p.parent.name == "sources"]
    summaries = {}
    for p in src_pages:
        text = p.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"^# .+\n\n(.+?)\n", text, re.S)
        summaries[p] = (m.group(1) if m else "")[:600]
    keys = list(summaries)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = summaries[keys[i]], summaries[keys[j]]
            if len(a) < 40 or len(b) < 40:
                continue
            r = SequenceMatcher(None, a, b).ratio()
            if r >= 0.9:
                dup_summaries.append((keys[i], keys[j], round(r, 3)))

    return {
        "pages": len(pages),
        "orphans": orphans,
        "broken": broken,
        "stubs": stubs,
        "dup_titles": dup_titles,
        "dup_summaries": dup_summaries,
        "inbound": inbound,
    }


def contradictions(vault: Vault, database: Database, max_checks: int = 6) -> list[dict[str, Any]]:
    """最近 7 天的来源页 vs 共享实体的旧页面，抽查主张冲突。"""
    with database.connect() as c:
        rows = c.execute(
            "SELECT n.id, n.path, n.title, n.source_id FROM notes n"
            " WHERE n.kind='source' AND n.path IS NOT NULL"
            " ORDER BY n.updated_at DESC LIMIT 12"
        ).fetchall()
    found: list[dict[str, Any]] = []
    checked = 0
    for r in rows:
        if checked >= max_checks:
            break
        cur_path = vault.root / r["path"]
        if not cur_path.exists():
            continue
        cur_text = cur_path.read_text(encoding="utf-8", errors="ignore")
        cur_claims = _claims(cur_text)
        if len(cur_claims) < 1:
            continue
        # 找共享实体的其它来源页
        with database.connect() as c:
            peers = c.execute(
                "SELECT DISTINCT n2.path, n2.title FROM edges e1"
                " JOIN edges e2 ON e1.dst_id=e2.dst_id AND e1.dst_type='entity'"
                " AND e2.dst_type='entity' AND e2.src_type='source'"
                " JOIN notes n2 ON n2.source_id=e2.src_id AND n2.kind='source'"
                " WHERE e1.src_type='source' AND e1.src_id=? AND e2.src_id<>? AND n2.path IS NOT NULL"
                " LIMIT 3",
                (r["source_id"], r["source_id"]),
            ).fetchall()
        for peer in peers:
            p_path = vault.root / peer["path"]
            if not p_path.exists() or checked >= max_checks:
                continue
            peer_claims = _claims(p_path.read_text(encoding="utf-8", errors="ignore"))
            if not peer_claims:
                continue
            checked += 1
            try:
                res = llm("verdict").chat_json([
                    {"role": "system", "content": CONTRADICT_SYSTEM},
                    {"role": "user", "content":
                        f"A《{r['title']}》主张：\n" + "\n".join(f"- {x}" for x in cur_claims[:6]) +
                        f"\n\nB《{peer['title']}》主张：\n" + "\n".join(f"- {x}" for x in peer_claims[:6])},
                ], max_tokens=400)
            except Exception:
                continue
            if res.get("conflict"):
                found.append({
                    "a": r["path"], "a_title": r["title"],
                    "b": peer["path"], "b_title": peer["title"],
                    "reason": str(res.get("reason", "")), "severity": str(res.get("severity", "medium")),
                })
            if found:
                break
    return found


def run(*, apply_warnings: bool = False, max_checks: int | None = None,
        database: Database | None = None) -> dict[str, Any]:
    database = database or db()
    vault = Vault()
    s = structural(vault)
    try:
        conflicts = contradictions(vault, database, max_checks=max_checks or 6)
    except Exception:
        conflicts = []
    mark = now_iso()

    lines = [f"# 知识库体检 {mark}", "", "## 规模", f"- 页面 {s['pages']}"]
    lines += ["", "## 孤儿页（没有任何入链）"]
    orphan_rows = [f"- {wikilink(*_kind_of(p), p.stem)}" for p in s["orphans"][:40]]
    lines += orphan_rows or ["- 无"]
    lines += ["", "## 断链（指向不存在的页面）"]
    broken_rows = []
    for p, targets in list(s["broken"].items())[:20]:
        broken_rows.append(f"- `{p.name}` → " + "、".join(f"[[{t}]]" for t in targets[:5]))
    lines += broken_rows or ["- 无"]
    lines += ["", "## 空壳页（实体/概念未补内容）"]
    lines += [f"- {wikilink(*_kind_of(p), p.stem)}" for p in s["stubs"][:30]] or ["- 无"]
    lines += ["", "## 疑似重复来源"]
    dup_rows = [f"- " + "、".join(wikilink("source", q.stem) for q in ps)
                for ps in s["dup_titles"].values()]
    dup_rows += [f"- {wikilink('source', a.stem)} ≈ {wikilink('source', b.stem)}（相似度 {r}）"
                 for a, b, r in s["dup_summaries"][:10]]
    lines += dup_rows or ["- 无"]
    lines += ["", "## 矛盾（LLM 抽查）"]
    if conflicts:
        for c in conflicts:
            lines.append(f"- **{c['a_title']}** ↔ **{c['b_title']}**：{c['reason']}（{c['severity']}）")
    else:
        lines.append("- 未发现")
    lines += ["", "## 建议动作",
              "- 补全空壳页：用 `opc enrich` 让 LLM 依据关联来源补 Key Facts",
              "- 合并重复来源：确认后手动删除较早的一页，保留链接更全的那页",
              "- 处理断链：改名或补建目标页，避免 Obsidian 里出现「幽灵链接」"]

    path = vault.wiki / "synthetic" / f"lint-{today()}.md"
    fm = _frontmatter(title=f"知识库体检 {today()}", type_="synthetic", tags=["lint", "health"],
                      sources=[], extra={"generated_at": mark})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fm + "\n" + "\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    if apply_warnings and conflicts:
        for c in conflicts:
            for target, other in ((c["a"], c["b_title"]), (c["b"], c["a_title"])):
                p = vault.safe_path(target)
                if p is None or not p.exists():
                    continue
                blob = p.read_text(encoding="utf-8")
                warn = (f"> [!warning] 与《{other}》存在潜在冲突\n"
                        f"> {c['reason']}（{c['severity']}）\n")
                if warn not in blob:
                    p.write_text(blob.rstrip() + "\n\n" + warn, encoding="utf-8", newline="\n")

    vault.log([f"lint 页面 {s['pages']} · 孤儿 {len(s['orphans'])} · 断链 {len(s['broken'])}"
               f" · 空壳 {len(s['stubs'])} · 矛盾 {len(conflicts)} → `{vault.rel(path)}`"])
    return {
        "path": vault.rel(path),
        "pages": s["pages"],
        "orphans": len(s["orphans"]),
        "broken": len(s["broken"]),
        "stubs": len(s["stubs"]),
        "duplicates": len(s["dup_titles"]) + len(s["dup_summaries"]),
        "conflicts": conflicts,
    }


def _kind_of(p: Path) -> tuple[str, str]:
    m = {"sources": "source", "entities": "entity", "concepts": "concept", "synthetic": "synthetic"}
    return m.get(p.parent.name, "source"), p.stem
