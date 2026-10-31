"""提炼流水线：捕获 → 归一 → 抽取（实体/概念/主张）→ 建链 → 写入 Obsidian。

一个来源只花一次（长文分块）LLM 调用，结果全部落 SQLite，
再以幂等方式同步为 Obsidian 页面，保证可重跑、可审计。
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import policy
from .config import cfg
from .db import Database, db
from .llm import llm
from .util import chunk_text, normalize_key, now_iso, to_json
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
        slug = vault.unique_slug("entity", e["name"])
        # 已存在同名页时复用其路径，不新建
        existing = _existing_page(vault, "entity", key)
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
        existing = _existing_page(vault, "concept", key)
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


def _existing_page(vault: Vault, kind: str, key: str):
    """按文件名 / frontmatter title / aliases 精确匹配同义页（绝不做正文模糊匹配）。"""
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
    meta = {"kind": src["kind"], "source_id": source_id}
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
        cites.append({"n": n, "title": title, "path": h.get("path", ""), "slug": h.get("slug", ""),
                      "id": h.get("id"), "kind": h.get("kind", "")})
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


def prepare_ask(question: str, database: Database | None = None,
                history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """检索 + 拼上下文，供一次性问答与流式问答共用（支持多轮）。"""
    database = database or db()
    search_q = rewrite_followup(question, history, database)
    expansions: list[str] = []
    try:
        from .query_expand import expand

        expansions = expand(search_q, database)
    except Exception:
        expansions = []
    hits: list[dict[str, Any]] = []
    try:
        from .retrieval import search as hybrid_search

        hits = hybrid_search(database, search_q, limit=6, expansions=expansions)
    except Exception:
        hits = database.search(search_q, limit=6)
    if not hits and search_q != question:
        hits = database.search(question, limit=6)
    if not hits:
        return {"messages": [], "citations": [], "empty": True, "expansions": expansions,
                "search_query": search_q}
    with database.connect() as c:
        for h in hits:
            if h["type"] == "note":
                row = c.execute("SELECT body_md FROM notes WHERE id=?", (h["id"],)).fetchone()
                if row:
                    h["body_md"] = row["body_md"]
    context, cites = build_context(hits)
    # PATCH 1：问答同样过敏感闸（上下文里可能带原文）
    sensitive = policy.classify(context, question)
    mask_map: dict[str, str] = {}
    q_prompt, sq_prompt = question, search_q
    if sensitive and cfg().get("policy.mask_entities", True):
        masked, mask_map = policy.mask_many([context, question, search_q])
        context, q_prompt, sq_prompt = masked
    messages = [{"role": "system", "content": ASK_SYSTEM}]
    messages += _history_messages(history)
    tail = f"知识库片段：\n{context}\n\n问题：{q_prompt}"
    if search_q != question:
        tail += f"\n（检索时理解为：{sq_prompt}）"
    messages.append({"role": "user", "content": tail})
    return {"messages": messages, "citations": cites, "empty": False,
            "expansions": expansions, "search_query": search_q,
            "sensitive": sensitive, "mask_map": mask_map}


def ask(question: str, *, save: bool = False, database: Database | None = None,
        history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    database = database or db()
    prepared = prepare_ask(question, database, history=history)
    if prepared["empty"]:
        return {"answer": "知识库里还没有相关内容。先用 /capture 记一条，或 /meeting 导入一场会议。",
                "citations": [], "saved": None, "expansions": prepared.get("expansions", [])}
    cites = prepared["citations"]
    answer = llm("ask").chat(prepared["messages"], max_tokens=1800)
    if prepared.get("mask_map"):
        answer = policy.unmask(answer, prepared["mask_map"])
    saved = None
    if save:
        saved = save_synthetic(question, answer, cites, database)
    database.event("ask", {"question": question, "hits": len(cites)})
    return {"answer": answer, "citations": cites, "saved": saved,
            "expansions": prepared.get("expansions", [])}


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
    path.write_text(f"{fm}\n# {title}\n\n{body}", encoding="utf-8", newline="\n")
    vault.log([f"qa `{title}` → `{vault.rel(path)}`"])
    note_id = database.put_note(
        source_id=None, kind="synthetic", title=title, slug=slug, path=vault.rel(path),
        summary=answer[:200], body_md=body, tags=["qa"], status="published", model=llm("ask").model,
    )
    return {"note_id": note_id, "path": vault.rel(path)}
