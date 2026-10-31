"""提炼流水线：捕获 → 归一 → 抽取（实体/概念/主张）→ 建链 → 写入 Obsidian。

一个来源只花一次（长文分块）LLM 调用，结果全部落 SQLite，
再以幂等方式同步为 Obsidian 页面，保证可重跑、可审计。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import policy
from .config import cfg
from .db import Database, db
from .llm import attribution as llm_attribution
from .llm import llm
from .util import atomic_write, chunk_text, normalize_key, now_iso, to_json
from .vault import Vault, bulletize, wikilink

ENTITY_TYPES = {"person", "org", "system", "tool", "project", "place", "event", "other"}

_JUNK_NAME = re.compile(
    r"(^[/\\]|[/\\]|^[a-zA-Z]:|^https?://|"
    r"\.(?:md|markdown|py|json|jsonc|toml|ya?ml|txt|js|ts|tsx|jsx|exe|db|html?|csv|xlsx|docx|pptx|pdf|zip|log)$)",
    re.I,
)
_GENERIC_NAMES = {"index", "config", "readme", "untitled", "例", "示例", "xxx"}


def valid_name(name: str) -> bool:
    """过滤文件名、路径、URL、纯符号等伪实体。"""
    n = (name or "").strip()
    if len(n) < 2 or len(n) > 60:
        return False
    if n.lower() in _GENERIC_NAMES or n.lower() in _GENERIC_NAMES:
        return False
    if _JUNK_NAME.search(n):
        return False
    if not re.search(r"[\w\u4e00-\u9fff]", n):
        return False
    return True

EXTRACT_SYSTEM = """你是「OPC 知识中枢」的提炼引擎。你的唯一任务：把原始素材转成结构化 JSON，\
用于构建个人知识图谱。要求：
1. 只输出 JSON，不要 Markdown 代码围栏，不要解释。
2. 忠于原文，禁止编造原文没有的人名、数字、结论。
3. 实体 = 具体存在物（人/组织/系统/工具/项目/地点/事件）；概念 = 可复用的抽象方法、框架、术语、指标。
4. 实体名用原文最常用写法，保留英文缩写；aliases 只放原文出现的别名。
5. 摘要用中文，3-5 句，说明"这段材料在讲什么、结论是什么"。
6. 抽取信息密度优先：宁可少而准，不要多而虚。
JSON 结构：
{
 "title": "≤30字标题",
 "summary": "3-5句中文摘要",
 "key_points": ["要点，每条≤60字", "..."],
 "claims": [{"text": "可被验证的主张/结论", "confidence": "high|medium|low"}],
 "entities": [{"name": "", "type": "person|org|system|tool|project|place|event|other",
               "description": "一句话说明它在本材料中的角色", "aliases": []}],
 "concepts": [{"name": "", "definition": "一句话定义", "aliases": [], "related_entities": []}],
 "actions": [{"text": "待办/决定", "owner": ""}],
 "tags": ["3-6个中文标签"],
 "open_questions": ["值得继续追的问题"]
}"""


def _user_prompt(title: str, text: str, part: int, total: int) -> str:
    hint = f"（第 {part}/{total} 段，注意本段可能不完整，摘要只概括本段）" if total > 1 else ""
    return f"标题：{title}{hint}\n\n--- 素材开始 ---\n{text}\n--- 素材结束 ---"


def extract_once(title: str, text: str, part: int = 1, total: int = 1) -> dict[str, Any]:
    return llm("extract").chat_json(
        [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": _user_prompt(title, text, part, total)},
        ],
        max_tokens=4096,
    )


def _merge(parts: list[dict[str, Any]], title: str) -> dict[str, Any]:
    if len(parts) == 1:
        data = parts[0]
    else:
        ents: dict[str, dict[str, Any]] = {}
        cons: dict[str, dict[str, Any]] = {}
        points: list[str] = []
        claims: list[dict[str, str]] = []
        actions: list[dict[str, str]] = []
        tags: list[str] = []
        questions: list[str] = []
        summaries: list[str] = []
        for p in parts:
            if p.get("summary"):
                summaries.append(str(p["summary"]))
            for pt in p.get("key_points") or []:
                if pt and str(pt) not in points:
                    points.append(str(pt))
            for c in p.get("claims") or []:
                if isinstance(c, dict) and c.get("text"):
                    claims.append({"text": str(c["text"]), "confidence": str(c.get("confidence", "medium"))})
            for a in p.get("actions") or []:
                if isinstance(a, dict) and a.get("text"):
                    actions.append({"text": str(a["text"]), "owner": str(a.get("owner", ""))})
            for t in p.get("tags") or []:
                if t and str(t) not in tags:
                    tags.append(str(t))
            for q in p.get("open_questions") or []:
                if q and str(q) not in questions:
                    questions.append(str(q))
            for e in p.get("entities") or []:
                if not isinstance(e, dict) or not e.get("name"):
                    continue
                k = normalize_key(str(e["name"]))
                cur = ents.setdefault(
                    k,
                    {
                        "name": str(e["name"]).strip(),
                        "type": _etype(e.get("type")),
                        "description": str(e.get("description", "")),
                        "aliases": [],
                    },
                )
                _add_aliases(cur["aliases"], e.get("aliases"))
                if len(str(e.get("description", ""))) > len(cur["description"]):
                    cur["description"] = str(e.get("description", ""))
            for c in p.get("concepts") or []:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                k = normalize_key(str(c["name"]))
                cur = cons.setdefault(
                    k,
                    {
                        "name": str(c["name"]).strip(),
                        "definition": str(c.get("definition", "")),
                        "aliases": [],
                        "related_entities": [],
                    },
                )
                _add_aliases(cur["aliases"], c.get("aliases"))
                _add_aliases(cur["related_entities"], c.get("related_entities"))
                if len(str(c.get("definition", ""))) > len(cur["definition"]):
                    cur["definition"] = str(c.get("definition", ""))
        data = {
            "title": title,
            "summary": " ".join(summaries)[:600],
            "key_points": points[:40],
            "claims": claims[:30],
            "entities": list(ents.values()),
            "concepts": list(cons.values()),
            "actions": actions[:20],
            "tags": tags[:8],
            "open_questions": questions[:12],
        }

    # 统一清洗与截断
    data["entities"] = [
        {
            "name": str(e.get("name", "")).strip(),
            "type": _etype(e.get("type")),
            "description": str(e.get("description", "")).strip(),
            "aliases": [str(a).strip() for a in (e.get("aliases") or []) if str(a).strip()][:5],
        }
        for e in (data.get("entities") or [])
        if isinstance(e, dict) and str(e.get("name", "")).strip()
        and valid_name(str(e.get("name", "")))
    ][: int(cfg().get("ingest.max_entities_per_doc", 24))]
    data["concepts"] = [
        {
            "name": str(c.get("name", "")).strip(),
            "definition": str(c.get("definition", "")).strip(),
            "aliases": [str(a).strip() for a in (c.get("aliases") or []) if str(a).strip()][:5],
            "related_entities": [str(a).strip() for a in (c.get("related_entities") or []) if str(a).strip()][:8],
        }
        for c in (data.get("concepts") or [])
        if isinstance(c, dict) and str(c.get("name", "")).strip()
        and valid_name(str(c.get("name", "")))
    ][: int(cfg().get("ingest.max_concepts_per_doc", 16))]
    data.setdefault("summary", "")
    data.setdefault("title", title)
    for key in ("key_points", "claims", "actions", "tags", "open_questions"):
        if not isinstance(data.get(key), list):
            data[key] = []
    return data


def _etype(v: Any) -> str:
    s = str(v or "other").strip().lower()
    return s if s in ENTITY_TYPES else "other"


def _add_aliases(bucket: list[str], values: Any) -> None:
    for a in values or []:
        a = str(a).strip()
        if a and a not in bucket:
            bucket.append(a)


def extract(title: str, text: str) -> dict[str, Any]:
    """分块抽取 + 合并，返回统一结构。"""
    max_chars = int(cfg().get("llm.max_input_chars", 24000))
    chunks = chunk_text(text, size=max_chars, overlap=300)
    if not chunks:
        raise ValueError("素材为空，无法提炼")
    parts: list[dict[str, Any]] = []
    for i, ch in enumerate(chunks, 1):
        parts.append(extract_once(title, ch, i, len(chunks)))
    return _merge(parts, title)


# ---------------------------------------------------------------- 建链 + 写入
def resolve_and_link(database: Database, source_id: int, data: dict[str, Any],
                     vault: Vault, text: str = "") -> dict[str, Any]:
    ents: list[dict[str, Any]] = []
    cons: list[dict[str, Any]] = []
    src_link = ""

    for e in data["entities"]:
        key = normalize_key(e["name"])
        if not key:
            continue
        # 锚定优先：命中锁定条目 → 名称/类型以锁定为准，类型分歧进冲突台账
        from .ontology import anchor

        locked = anchor(database, "entity", key, e, source_id)
        if locked:
            e["name"], e["type"] = locked["name"], locked["type"] or e["type"]
            e["id"], e["slug"] = locked["id"], locked["slug"]
            ents.append(e)
            continue
        slug = vault.unique_slug("entity", e["name"])
        # 已存在同名页时复用其路径，不新建
        existing = _existing_page(vault, "entity", key, database)
        if existing:
            slug = existing.stem
        ent_id = database.upsert_entity(
            key, e["name"], e["type"], aliases=e["aliases"], summary=e["description"],
            page_path=f"wiki/entities/{slug}.md",
        )
        e["id"], e["slug"] = ent_id, slug
        ents.append(e)

    for c in data["concepts"]:
        key = normalize_key(c["name"])
        if not key:
            continue
        # 锚定优先：锁定概念的定义不可覆盖（definition 分歧走 lint 矛盾通道，本层不建冲突）
        from .ontology import anchor

        locked = anchor(database, "concept", key, c, source_id)
        if locked:
            c["name"], c["definition"] = locked["name"], locked["definition"]
            c["id"], c["slug"] = locked["id"], locked["slug"]
            cons.append(c)
            continue
        existing = _existing_page(vault, "concept", key, database)
        slug = existing.stem if existing else vault.unique_slug("concept", c["name"])
        cid = database.upsert_concept(
            key, c["name"], c["definition"], aliases=c["aliases"],
            page_path=f"wiki/concepts/{slug}.md",
        )
        c["id"], c["slug"] = cid, slug
        cons.append(c)

    # 边：来源→实体 / 来源→概念 / 实体↔实体（同源共现）
    for e in ents:
        database.add_edge("source", source_id, "entity", e["id"], "mentions", weight=1.0,
                          evidence=e["description"][:200], source_id=source_id)
    for c in cons:
        database.add_edge("source", source_id, "concept", c["id"], "covers", weight=1.0,
                          evidence=c["definition"][:200], source_id=source_id)
    # 句级共现：只有同一句话里出现过的实体对才建边，evidence 是那句原文
    if text:
        from .cooccur import rebuild_for_source

        rebuild_for_source(database, source_id, text, ents)

    name_to_ent = {normalize_key(e["name"]): e for e in ents}
    for alias, e in [(a, e) for e in ents for a in e["aliases"]]:
        name_to_ent.setdefault(normalize_key(alias), e)
    for c in cons:
        for rn in c["related_entities"]:
            e = name_to_ent.get(normalize_key(rn))
            if e:
                database.add_edge("concept", c["id"], "entity", e["id"], "related_to", weight=0.7,
                                  source_id=source_id)

    min_src = int(cfg().get("vault.min_sources_for_page", 2))
    for e in ents:
        e["src_count"] = database.refresh_entity_sources(e["id"], min_src)
    return {"entities": ents, "concepts": cons, "source_link": src_link}


def _existing_page(vault: Vault, kind: str, key: str, database: Database | None = None):
    """先查库里的 page_path，命中就免掉逐文件扫描；未命中再回退到磁盘。"""
    if database is not None:
        table = "entities" if kind == "entity" else "concepts"
        with database.connect() as c:
            row = c.execute(f"SELECT page_path FROM {table} WHERE key=?", (key,)).fetchone()
        if row and row["page_path"]:
            p = vault.safe_path(row["page_path"])
            if p is not None and p.exists():
                return p
    folder = {"entity": "entities", "concept": "concepts"}[kind]
    for p in (vault.wiki / folder).glob("*.md"):
        if normalize_key(p.stem) == key:
            return p
        try:
            head = p.read_text(encoding="utf-8")[:600]
        except Exception:
            continue
        fm = head.split("---", 2)[1] if head.startswith("---") and head.count("---") >= 2 else ""
        if not fm:
            continue
        m = re.search(r'^title:\s*"?(.*?)"?\s*$', fm, re.M)
        if m and normalize_key(m.group(1)) == key:
            return p
        m = re.search(r"^aliases:\s*\[(.*?)\]", fm, re.M | re.S)
        if m:
            for alias in m.group(1).split(","):
                if normalize_key(alias.strip().strip("\"'")) == key:
                    return p
    return None


def render_body(data: dict[str, Any], links: dict[str, Any], raw_note: str = "") -> str:
    parts: list[str] = []
    if data.get("key_points"):
        parts.append("## 要点\n" + bulletize(data["key_points"], 20))
    if data.get("claims"):
        rows = [f"- {c['text']}（置信度 {c.get('confidence', 'medium')}）" for c in data["claims"][:12]]
        parts.append("## 关键主张\n" + "\n".join(rows))
    if data.get("actions"):
        rows = [f"- [ ] {a['text']}" + (f" —— @{a['owner']}" if a.get("owner") else "")
                for a in data["actions"][:12]]
        parts.append("## 行动项\n" + "\n".join(rows))
    if links["entities"]:
        rows = [f"- {wikilink('entity', e['slug'], e['name'])}（{e['type']}）" for e in links["entities"]]
        parts.append("## 相关实体\n" + "\n".join(rows))
    if links["concepts"]:
        rows = [f"- {wikilink('concept', c['slug'], c['name'])}" for c in links["concepts"]]
        parts.append("## 相关概念\n" + "\n".join(rows))
    if data.get("open_questions"):
        parts.append("## 待追问题\n" + bulletize(data["open_questions"], 8))
    if raw_note:
        parts.append("## 原始记录\n" + raw_note)
    return "\n\n".join(parts)


def distill_source(source_id: int, *, publish: bool = True, database: Database | None = None,
                   on_progress=None) -> dict[str, Any]:
    database = database or db()
    vault = Vault()
    with database.connect() as c:
        src = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        doc = c.execute("SELECT * FROM documents WHERE source_id=?", (source_id,)).fetchone()
    if not src:
        raise ValueError(f"source {source_id} 不存在")
    if not doc or not doc["text"].strip():
        raise ValueError(f"source {source_id} 没有正文")

    title = src["title"]
    raw_text = doc["text"]
    # PATCH 1：敏感闸。命中关键词 → 送模型前令牌化，回来后还原；映射表只活在内存里。
    sensitive = policy.classify(raw_text, title)
    mask_map: dict[str, str] = {}
    text_for_llm, title_for_llm = raw_text, title
    if sensitive and cfg().get("policy.mask_entities", True):
        masked, mask_map = policy.mask_many([title, raw_text])
        title_for_llm, text_for_llm = masked
    try:
        database.merge_source_meta(source_id, {"sensitive": sensitive})
    except Exception:
        pass
    if on_progress:
        on_progress(25, "提炼中：抽取实体/概念/主张")
    with llm_attribution(llm("extract"), sensitive=sensitive, source_id=source_id):
        data = extract(title_for_llm, text_for_llm)
    if mask_map:
        data = json.loads(policy.unmask(json.dumps(data, ensure_ascii=False), mask_map))
    if on_progress:
        on_progress(65, "建链中：实体归并 + 关系边")
    final_title = (data.get("title") or title).strip() or title
    links = resolve_and_link(database, source_id, data, vault, text=raw_text)
    if on_progress:
        on_progress(80, "写入中：生成 Obsidian 页面")

    source_refs = [src["uri"]] if src["uri"] else []
    meta = {"kind": src["kind"], "source_id": source_id,
            "content_hash": src["content_hash"] or ""}
    raw_note = ""
    if src["raw_path"]:
        source_refs.append(src["raw_path"])
    source_slug = _note_slug(database, source_id, final_title)
    src_link = wikilink("source", source_slug, final_title)
    body = render_body(data, links, raw_note)

    path = None
    if publish:
        p = vault.write_source_page(
            slug=source_slug, title=final_title, summary=data.get("summary", ""),
            body=body, tags=data.get("tags", []), source_refs=source_refs, meta=meta,
        )
        min_src = int(cfg().get("vault.min_sources_for_page", 2))
        for e in links["entities"]:
            # 单来源实体只留在库里，够 2 个来源（或已有页）才写 vault
            if e.get("src_count", 0) >= min_src or vault.page_exists("entity", e["slug"]):
                vault.ensure_entity_page(
                    slug=e["slug"], name=e["name"], type_=e["type"],
                    description=e["description"], aliases=e["aliases"], source_link=src_link,
                )
        for c in links["concepts"]:
            vault.ensure_concept_page(
                slug=c["slug"], name=c["name"], definition=c["definition"],
                aliases=c["aliases"], source_link=src_link,
            )
        vault.index_add("source", source_slug, final_title,
                        [str(src["external_id"])] if src["kind"] == "meeting" else [])
        for e in links["entities"]:
            if e.get("src_count", 0) >= min_src or vault.page_exists("entity", e["slug"]):
                vault.index_add("entity", e["slug"], e["name"], e["aliases"],
                                f"- type: {e['type']}")
        for c in links["concepts"]:
            vault.index_add("concept", c["slug"], c["name"], c["aliases"])
        vault.log([
            f"ingest `{final_title}` → `{vault.rel(p)}`"
            f"（实体 {len(links['entities'])} / 概念 {len(links['concepts'])}）"
        ])
        path = vault.rel(p)

    note_id = database.put_note(
        source_id=source_id, kind="source", title=final_title, slug=source_slug, path=path,
        summary=data.get("summary", ""), body_md=body, tags=data.get("tags", []),
        status="published" if publish else "draft", model=llm("extract").model,
    )
    database.add_edge("note", note_id, "source", source_id, "distills", weight=1.0)
    for e in links["entities"]:
        database.add_edge("note", note_id, "entity", e["id"], "mentions", weight=1.0)
    for c in links["concepts"]:
        database.add_edge("note", note_id, "concept", c["id"], "covers", weight=1.0)

    if on_progress:
        on_progress(95, "收尾：更新索引与日志")
    database.event("distill.done", {"source_id": source_id, "note_id": note_id, "path": path})
    return {
        "source_id": source_id,
        "note_id": note_id,
        "title": final_title,
        "path": path,
        "summary": data.get("summary", ""),
        "entities": [{"name": e["name"], "slug": e["slug"]} for e in links["entities"]],
        "concepts": [{"name": c["name"], "slug": c["slug"]} for c in links["concepts"]],
        "key_points": data.get("key_points", [])[:8],
        "actions": data.get("actions", []),
    }


def _note_slug(database: Database, source_id: int, title: str) -> str:
    with database.connect() as c:
        row = c.execute(
            "SELECT slug FROM notes WHERE source_id=? AND kind='source'", (source_id,)
        ).fetchone()
    if row:
        return row["slug"]
    base = Vault().unique_slug("source", title)
    slug, i = base, 2
    while True:
        with database.connect() as c:
            hit = c.execute(
                "SELECT source_id FROM notes WHERE slug=? AND kind='source'", (slug,)
            ).fetchone()
        if not hit or hit["source_id"] == source_id:
            return slug
        slug = f"{base}-{i}"
        i += 1


# ---------------------------------------------------------------- 问答
ASK_SYSTEM = """你是用户的第二大脑。基于给定的知识库片段回答，规则：
1. 只使用给定片段中的信息；片段不足就明说"知识库里没有足够信息"，不要编造。
2. 用中文，先给结论，再给依据；涉及具体来源时在句末标注 [n]。
3. 如果片段之间存在冲突，明确指出冲突。
4. 回答末尾给「可继续追问」2-3 条。"""


def build_context(hits: list[dict[str, Any]], limit_chars: int = 9000) -> tuple[str, list[dict[str, Any]]]:
    blocks, cites, used = [], [], 0
    seen_titles: set[str] = set()
    for h in hits:
        text = h.get("snippet") or ""
        if h.get("type") == "note" and h.get("body_md"):
            text = h["body_md"]
        text = text.strip()[:2200]
        if not text:
            continue
        title = (h.get("title") or "").strip()
        if title and title in seen_titles:
            continue
        if used + len(text) > limit_chars:
            break
        used += len(text)
        seen_titles.add(title)
        n = len(cites) + 1
        ctype = h.get("type") or "note"
        cites.append({
            "n": n, "title": title, "type": ctype,
            "path": h.get("path", "") or "", "slug": h.get("slug", "") or "",
            "id": h.get("id"),
            "note_id": h.get("id") if ctype == "note" else None,
            "source_id": h.get("id") if ctype == "chunk" else None,
            "kind": h.get("kind", "") or "",
            "channels": h.get("channels", []) or [],
            "snippet": text[:240],
        })
        blocks.append(f"[{n}] 《{title}》\n{text}")
    return "\n\n".join(blocks), cites


def _history_messages(history: list[dict[str, Any]] | None, limit: int = 6) -> list[dict[str, str]]:
    """把前端带来的多轮对话压成 LLM 消息；只保留最近若干轮，单条截断。"""
    out: list[dict[str, str]] = []
    for h in (history or [])[-limit:]:
        role = str(h.get("role", "")).strip()
        content = str(h.get("content", "")).strip()
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content[:1800]})
    return out


REWRITE_SYSTEM = """把用户的最新追问改写成一句「可独立检索」的问题。
规则：
1. 结合上一轮对话补全指代（"那第二条""这个"→ 具体指什么）。
2. 只输出改写后的问句，不要解释，不要引号。
3. 如果最新一句本身已经能独立检索，原样输出。
只输出 JSON：{"query": ""}"""


def rewrite_followup(question: str, history: list[dict[str, Any]] | None,
                     database: Database) -> str:
    """多轮追问的检索改写：把「那第二条呢」变成能搜到东西的句子。"""
    if not history or not cfg().get("ask.rewrite_followups", True):
        return question
    try:
        data = llm("ask").chat_json(
            [
                {"role": "system", "content": REWRITE_SYSTEM},
                {"role": "user", "content": "上一轮：\n" + "\n".join(
                    f"{h.get('role')}: {str(h.get('content'))[:600]}" for h in history[-4:]
                ) + f"\n\n最新追问：{question}"},
            ],
            max_tokens=160,
            temperature=0.1,
        )
        q = str(data.get("query", "")).strip().strip('"')
        return q if 2 <= len(q) <= 120 else question
    except Exception:
        return question


def _scope_hits(database: Database, hits: list[dict[str, Any]],
                scope: dict[str, Any]) -> list[dict[str, Any]]:
    """问答作用域（FR-408）：按来源 / 来源类型 / 日期范围过滤检索命中。"""
    out: list[dict[str, Any]] = []
    with database.connect() as c:
        for h in hits:
            if h.get("type") == "note":
                row = c.execute(
                    "SELECT s.id AS sid, s.kind AS skind,"
                    " COALESCE(s.occurred_at, s.captured_at) AS ts"
                    " FROM notes n JOIN sources s ON s.id=n.source_id WHERE n.id=?",
                    (h["id"],),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT id AS sid, kind AS skind,"
                    " COALESCE(occurred_at, captured_at) AS ts FROM sources WHERE id=?",
                    (h["id"],),
                ).fetchone()
            if not row:
                continue
            if scope.get("source_id") and int(row["sid"]) != int(scope["source_id"]):
                continue
            if scope.get("kind") and row["skind"] != scope["kind"]:
                continue
            day = str(row["ts"] or "")[:10]
            if scope.get("since") and day and day < scope["since"]:
                continue
            if scope.get("until") and day and day > scope["until"]:
                continue
            out.append(h)
    return out


def prepare_ask(question: str, database: Database | None = None,
                history: list[dict[str, Any]] | None = None,
                on_stage: Any = None,
                scope: dict[str, Any] | None = None) -> dict[str, Any]:
    """检索 + 拼上下文，供一次性问答与流式问答共用（支持多轮）。

    on_stage: 可选回调，接收阶段文字，用于 SSE 进度提示。
    """
    database = database or db()

    def stage(text: str) -> None:
        if on_stage:
            try:
                on_stage(text)
            except Exception:
                pass

    stage("理解追问")
    # 敏感闸前移（I8）：先给问题与历史分级；命中则脱敏后再送 LLM（改写/扩展），
    # 检索仍用原文（知识库里存的是原名），送模型的上下文再按同一映射脱敏。
    history_msgs = _history_messages(history)
    history_text = "\n".join(m["content"] for m in history_msgs)
    mask_entities = bool(cfg().get("policy.mask_entities", True))
    sensitive = policy.classify(f"{question}\n{history_text}", "")
    state: dict[str, Any] | None = None
    q_for_llm = question
    if sensitive and mask_entities:
        state = policy.new_state()
        if history_msgs:
            masked_hist, _ = policy.mask_many([m["content"] for m in history_msgs], state=state)
            history_msgs = [{**m, "content": t} for m, t in zip(history_msgs, masked_hist)]
        masked_q, _ = policy.mask_many([question], state=state)
        q_for_llm = masked_q[0]
    search_q = rewrite_followup(q_for_llm, history_msgs, database)
    if state is not None:
        search_q = policy.unmask(search_q, state["mapping"])
    expansions: list[str] = []
    # 敏感问题不做查询扩展：扩展词会进 30 天缓存，令牌化词条出缓存后无法安全还原
    if state is None:
        stage("扩展检索词")
        try:
            from .query_expand import expand

            expansions = expand(search_q, database)
        except Exception:
            expansions = []
    hits: list[dict[str, Any]] = []
    stage("检索知识库")
    try:
        from .retrieval import search as hybrid_search

        hits = hybrid_search(database, search_q, limit=6, expansions=expansions)
    except Exception:
        hits = database.search(search_q, limit=6)
    if not hits and search_q != question:
        hits = database.search(question, limit=6)
    vec_hits = sum(1 for h in hits if "vec" in (h.get("channels") or []))
    semantic_meta = {"semantic": vec_hits > 0, "vec_hits": vec_hits}
    if scope:
        hits = _scope_hits(database, hits, scope)
    if not hits:
        return {"messages": [], "citations": [], "empty": True, "expansions": expansions,
                "search_query": search_q, "hits": 0, "sensitive": sensitive,
                "masked": state is not None, **semantic_meta,
                "mask_map": state["mapping"] if state else {}}
    with database.connect() as c:
        for h in hits:
            if h["type"] == "note":
                row = c.execute("SELECT body_md FROM notes WHERE id=?", (h["id"],)).fetchone()
                if row:
                    h["body_md"] = row["body_md"]
    stage("准备上下文")
    context, cites = build_context(hits)
    # 上下文里可能带原文：与问题共用同一映射，令牌一致
    if mask_entities and (sensitive or policy.classify(context, "")):
        sensitive = True
        if state is None:
            state = policy.new_state()
        context, _ = policy.mask_many([context], state=state)
    mask_map: dict[str, str] = state["mapping"] if state else {}
    q_prompt, sq_prompt = question, search_q
    if state is not None:
        q_prompt = policy.mask_many([question], state=state)[0][0]
        sq_prompt = policy.mask_many([search_q], state=state)[0][0]
    messages = [{"role": "system", "content": ASK_SYSTEM}]
    messages += history_msgs
    tail = f"知识库片段：\n{context}\n\n问题：{q_prompt}"
    if search_q != question:
        tail += f"\n（检索时理解为：{sq_prompt}）"
    messages.append({"role": "user", "content": tail})
    return {"messages": messages, "citations": cites, "empty": False,
            "expansions": expansions, "search_query": search_q, "hits": len(cites),
            "sensitive": sensitive, "masked": state is not None,
            "mask_map": mask_map, **semantic_meta}


def ask(question: str, *, save: bool = False, database: Database | None = None,
        history: list[dict[str, Any]] | None = None,
        scope: dict[str, Any] | None = None) -> dict[str, Any]:
    database = database or db()
    prepared = prepare_ask(question, database, history=history, scope=scope)
    if prepared["empty"]:
        return {"answer": "知识库里还没有相关内容。先用 /capture 记一条，或 /meeting 导入一场会议。",
                "citations": [], "saved": None, "expansions": prepared.get("expansions", []),
                "semantic": False, "vec_hits": 0}
    cites = prepared["citations"]
    with llm_attribution(llm("ask"), sensitive=bool(prepared.get("sensitive"))):
        answer = llm("ask").chat(prepared["messages"], max_tokens=1800)
    if prepared.get("mask_map"):
        answer = policy.unmask(answer, prepared["mask_map"])
    saved = None
    if save:
        saved = save_synthetic(question, answer, cites, database)
    database.event("ask", {"q_len": len(question),
                           "q_fp": hashlib.sha1(question.encode("utf-8")).hexdigest()[:8],
                           "hits": len(cites)})
    return {"answer": answer, "citations": cites, "saved": saved,
            "expansions": prepared.get("expansions", []),
            "semantic": prepared.get("semantic", False),
            "vec_hits": prepared.get("vec_hits", 0)}


def save_synthetic(question: str, answer: str, cites: list[dict[str, Any]],
                   database: Database | None = None) -> dict[str, Any]:
    database = database or db()
    vault = Vault()
    title = question.strip().splitlines()[0][:40] or "问答"
    slug = vault.unique_slug("source", f"qa-{title}")
    src_lines = "\n".join(f"- [{c['n']}] [[sources/{c['slug']}|{c['title']}]]" for c in cites if c.get("slug"))
    body = f"## 问题\n{question}\n\n## 回答\n{answer}\n\n## 出处\n{src_lines}\n"
    from .vault import _frontmatter  # 复用 frontmatter

    fm = _frontmatter(title=title, type_="synthetic", tags=["qa"], sources=[c["path"] for c in cites if c.get("path")])
    path = vault.wiki / "synthetic" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, f"{fm}\n# {title}\n\n{body}")
    vault.log([f"qa `{title}` → `{vault.rel(path)}`"])
    note_id = database.put_note(
        source_id=None, kind="synthetic", title=title, slug=slug, path=vault.rel(path),
        summary=answer[:200], body_md=body, tags=["qa"], status="published", model=llm("ask").model,
    )
    return {"note_id": note_id, "path": vault.rel(path)}
