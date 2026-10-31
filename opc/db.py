"""SQLite 存储层：来源、文档、笔记、实体、概念、关系边、任务、事件 + FTS5 全文检索。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import cfg
from .util import from_json, now_iso, to_json

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                -- meeting / note / file / web / chat / session
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  uri TEXT,
  author TEXT,
  occurred_at TEXT,
  captured_at TEXT NOT NULL,
  content_hash TEXT,
  raw_path TEXT,
  meta TEXT,
  UNIQUE(kind, external_id)
);

CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  char_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(source_id)
);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  source_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  text TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  kind TEXT NOT NULL DEFAULT 'source',   -- source / synthetic / daily
  title TEXT NOT NULL,
  slug TEXT NOT NULL,
  path TEXT,
  summary TEXT,
  body_md TEXT,
  tags TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  model TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(slug, kind)
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  title, summary, body_md, tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT 'thing',
  aliases TEXT,
  summary TEXT,
  page_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS concepts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  definition TEXT,
  aliases TEXT,
  page_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src_type TEXT NOT NULL,
  src_id INTEGER NOT NULL,
  dst_type TEXT NOT NULL,
  dst_id INTEGER NOT NULL,
  rel TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0,
  evidence TEXT,
  source_id INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE(src_type, src_id, dst_type, dst_id, rel)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_type, src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_type, dst_id);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload TEXT,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending/running/done/failed
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, id DESC);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  title TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  citations TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_msgs ON conversation_messages(conv_id, id);

CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task TEXT,
  provider TEXT,
  model TEXT,
  in_tokens INTEGER NOT NULL DEFAULT 0,
  out_tokens INTEGER NOT NULL DEFAULT 0,
  source_id INTEGER,
  sensitive INTEGER NOT NULL DEFAULT 0,
  ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_notes_source ON notes(source_id);
CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_entities_updated ON entities(updated_at DESC);
"""


FTS_VERSION = "trigram-1"

REBUILD_FTS = """
DROP TABLE IF EXISTS chunks_fts;
DROP TABLE IF EXISTS notes_fts;
CREATE VIRTUAL TABLE chunks_fts USING fts5(text, tokenize='trigram');
CREATE VIRTUAL TABLE notes_fts USING fts5(title, summary, body_md, tokenize='trigram');
INSERT INTO notes_fts(rowid, title, summary, body_md)
  SELECT id, title, COALESCE(summary,''), COALESCE(body_md,'') FROM notes;
INSERT INTO chunks_fts(rowid, text) SELECT id, text FROM chunks;
"""


class Database:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or cfg().db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            row = conn.execute("SELECT value FROM meta WHERE key='fts_version'").fetchone()
            if not row or row[0] != FTS_VERSION:
                conn.executescript(REBUILD_FTS)
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('fts_version', ?)",
                    (FTS_VERSION,),
                )
                conn.commit()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """轻量迁移：只做加列，不动已有数据。"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        for name, ddl in (
            ("progress", "ALTER TABLE jobs ADD COLUMN progress INTEGER NOT NULL DEFAULT 0"),
            ("stage", "ALTER TABLE jobs ADD COLUMN stage TEXT"),
            ("result", "ALTER TABLE jobs ADD COLUMN result TEXT"),
        ):
            if name not in cols:
                conn.execute(ddl)

        # PATCH 5：单来源不建页 —— 实体记录来源数，够 2 个来源才算 confirmed
        ecols = {r[1] for r in conn.execute("PRAGMA table_info(entities)")}
        if "src_count" not in ecols:
            conn.execute("ALTER TABLE entities ADD COLUMN src_count INTEGER NOT NULL DEFAULT 0")
        if "status" not in ecols:
            conn.execute("ALTER TABLE entities ADD COLUMN status TEXT NOT NULL DEFAULT 'proposed'")
        conn.execute(
            "UPDATE entities SET src_count=("
            "  SELECT COUNT(DISTINCT source_id) FROM edges"
            "  WHERE dst_type='entity' AND dst_id=entities.id AND rel='mentions'"
            ")"
        )
        conn.execute(
            "UPDATE entities SET status='confirmed' WHERE src_count>=2 AND status!='confirmed'"
        )
        conn.commit()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 通用 ----------
    def event(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events(ts, kind, payload) VALUES(?,?,?)",
                (now_iso(), kind, to_json(payload or {})),
            )

    def merge_source_meta(self, source_id: int, patch: dict[str, Any]) -> None:
        with self.tx() as c:
            row = c.execute("SELECT meta FROM sources WHERE id=?", (source_id,)).fetchone()
            if not row:
                return
            meta = from_json(row["meta"], {}) or {}
            meta.update(patch)
            c.execute("UPDATE sources SET meta=? WHERE id=?", (to_json(meta), source_id))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO meta(key, value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ---------- 来源 / 文档 ----------
    def upsert_source(
        self,
        kind: str,
        external_id: str,
        title: str,
        *,
        uri: str | None = None,
        author: str | None = None,
        occurred_at: str | None = None,
        content_hash: str | None = None,
        raw_path: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        with self.tx() as c:
            row = c.execute(
                "SELECT id FROM sources WHERE kind=? AND external_id=?", (kind, external_id)
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE sources SET title=?, uri=?, author=?, occurred_at=?, captured_at=?,"
                    " content_hash=?, raw_path=?, meta=? WHERE id=?",
                    (
                        title,
                        uri,
                        author,
                        occurred_at,
                        now_iso(),
                        content_hash,
                        raw_path,
                        to_json(meta or {}),
                        row["id"],
                    ),
                )
                return int(row["id"])
            try:
                cur = c.execute(
                    "INSERT INTO sources(kind, external_id, title, uri, author, occurred_at,"
                    " captured_at, content_hash, raw_path, meta) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (kind, external_id, title, uri, author, occurred_at, now_iso(),
                     content_hash, raw_path, to_json(meta or {})),
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                # 并发插入撞唯一键：回到更新分支
                row = c.execute(
                    "SELECT id FROM sources WHERE kind=? AND external_id=?",
                    (kind, external_id),
                ).fetchone()
                if not row:
                    raise
                c.execute(
                    "UPDATE sources SET title=?, uri=?, author=?, occurred_at=?,"
                    " captured_at=?, content_hash=?, raw_path=?, meta=? WHERE id=?",
                    (title, uri, author, occurred_at, now_iso(), content_hash, raw_path,
                     to_json(meta or {}), row["id"]),
                )
                return int(row["id"])

    def put_document(self, source_id: int, text: str) -> int:
        with self.tx() as c:
            row = c.execute("SELECT id FROM documents WHERE source_id=?", (source_id,)).fetchone()
            if row:
                c.execute(
                    "UPDATE documents SET text=?, char_count=?, created_at=? WHERE id=?",
                    (text, len(text), now_iso(), row["id"]),
                )
                doc_id = int(row["id"])
                # 先清 FTS 再清 chunks（rowid 对齐；顺序反了会留孤儿行）
                c.execute(
                    "DELETE FROM chunks_fts WHERE rowid IN"
                    " (SELECT id FROM chunks WHERE document_id=?)",
                    (doc_id,),
                )
                c.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
            else:
                cur = c.execute(
                    "INSERT INTO documents(source_id, text, char_count, created_at) VALUES(?,?,?,?)",
                    (source_id, text, len(text), now_iso()),
                )
                doc_id = int(cur.lastrowid)
            return doc_id

    def put_chunks(self, doc_id: int, source_id: int, chunks: list[str]) -> None:
        from .util import chunk_text  # 局部导入避免循环

        with self.tx() as c:
            # FTS rowid 对齐 chunks.id：必须先删 FTS，再删 chunks，否则子查询已空
            c.execute(
                "DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE document_id=?)",
                (doc_id,),
            )
            c.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
            for i, ch in enumerate(chunks):
                cur = c.execute(
                    "INSERT INTO chunks(document_id, source_id, idx, text) VALUES(?,?,?,?)",
                    (doc_id, source_id, i, ch),
                )
                c.execute(
                    "INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", (cur.lastrowid, ch)
                )

    # ---------- 笔记 ----------
    def put_note(
        self,
        *,
        source_id: int | None,
        kind: str,
        title: str,
        slug: str,
        path: str | None,
        summary: str,
        body_md: str,
        tags: list[str] | None = None,
        status: str = "draft",
        model: str | None = None,
    ) -> int:
        with self.tx() as c:
            row = c.execute(
                "SELECT id FROM notes WHERE slug=? AND kind=?", (slug, kind)
            ).fetchone()
            if row:
                note_id = int(row["id"])
                c.execute(
                    "UPDATE notes SET title=?, path=?, summary=?, body_md=?, tags=?, status=?,"
                    " model=?, updated_at=? WHERE id=?",
                    (
                        title,
                        path,
                        summary,
                        body_md,
                        to_json(tags or []),
                        status,
                        model,
                        now_iso(),
                        note_id,
                    ),
                )
                c.execute("DELETE FROM notes_fts WHERE rowid=?", (note_id,))
            else:
                cur = c.execute(
                    "INSERT INTO notes(source_id, kind, title, slug, path, summary, body_md,"
                    " tags, status, model, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source_id,
                        kind,
                        title,
                        slug,
                        path,
                        summary,
                        body_md,
                        to_json(tags or []),
                        status,
                        model,
                        now_iso(),
                        now_iso(),
                    ),
                )
                note_id = int(cur.lastrowid)
            c.execute(
                "INSERT INTO notes_fts(rowid, title, summary, body_md) VALUES(?,?,?,?)",
                (note_id, title, summary, body_md),
            )
            return note_id

    # ---------- 实体 / 概念 ----------
    def upsert_entity(
        self,
        key: str,
        name: str,
        type_: str,
        *,
        aliases: list[str] | None = None,
        summary: str = "",
        page_path: str | None = None,
    ) -> int:
        with self.tx() as c:
            row = c.execute("SELECT id, page_path FROM entities WHERE key=?", (key,)).fetchone()
            if row:
                c.execute(
                    "UPDATE entities SET name=?, aliases=?, summary=COALESCE(NULLIF(?,''),summary),"
                    " page_path=COALESCE(?,page_path), updated_at=? WHERE id=?",
                    (name, to_json(aliases or []), summary, page_path, now_iso(), row["id"]),
                )
                return int(row["id"])
            cur = c.execute(
                "INSERT INTO entities(key, name, type, aliases, summary, page_path, created_at,"
                " updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (key, name, type_, to_json(aliases or []), summary, page_path, now_iso(), now_iso()),
            )
            return int(cur.lastrowid)

    def refresh_entity_sources(self, entity_id: int, min_sources: int = 2) -> int:
        """按 mentions 边重算来源数；够门槛就转 confirmed（不会降级）。"""
        with self.tx() as c:
            n = c.execute(
                "SELECT COUNT(DISTINCT source_id) FROM edges"
                " WHERE src_type='source' AND dst_type='entity' AND dst_id=?"
                " AND rel='mentions' AND source_id IS NOT NULL",
                (entity_id,),
            ).fetchone()[0]
            c.execute(
                "UPDATE entities SET src_count=?,"
                " status=CASE WHEN ? >= ? OR status='confirmed' THEN 'confirmed' ELSE status END"
                " WHERE id=?",
                (int(n), int(n), int(min_sources), entity_id),
            )
        return int(n)

    def upsert_concept(
        self,
        key: str,
        name: str,
        definition: str = "",
        *,
        aliases: list[str] | None = None,
        page_path: str | None = None,
    ) -> int:
        with self.tx() as c:
            row = c.execute("SELECT id FROM concepts WHERE key=?", (key,)).fetchone()
            if row:
                c.execute(
                    "UPDATE concepts SET name=?, definition=COALESCE(NULLIF(?,''),definition),"
                    " aliases=?, page_path=COALESCE(?,page_path), updated_at=? WHERE id=?",
                    (name, definition, to_json(aliases or []), page_path, now_iso(), row["id"]),
                )
                return int(row["id"])
            cur = c.execute(
                "INSERT INTO concepts(key, name, definition, aliases, page_path, created_at,"
                " updated_at) VALUES(?,?,?,?,?,?,?)",
                (key, name, definition, to_json(aliases or []), page_path, now_iso(), now_iso()),
            )
            return int(cur.lastrowid)

    def add_edge(
        self,
        src_type: str,
        src_id: int,
        dst_type: str,
        dst_id: int,
        rel: str,
        *,
        weight: float = 1.0,
        evidence: str = "",
        source_id: int | None = None,
    ) -> None:
        with self.tx() as c:
            row = c.execute(
                "SELECT id, weight, evidence, source_id FROM edges"
                " WHERE src_type=? AND src_id=? AND dst_type=? AND dst_id=? AND rel=?",
                (src_type, src_id, dst_type, dst_id, rel),
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO edges(src_type, src_id, dst_type, dst_id, rel, weight, evidence,"
                    " source_id, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (src_type, src_id, dst_type, dst_id, rel, weight, evidence, source_id,
                     now_iso()),
                )
                return
            # 同一来源重跑，或本就不按来源计数的边（source_id=None）：幂等
            if source_id is None or row["source_id"] == source_id:
                return
            longer = evidence if len(evidence or "") > len(row["evidence"] or "") else row["evidence"]
            c.execute(
                "UPDATE edges SET weight=?, evidence=?, source_id=? WHERE id=?",
                (float(row["weight"]) + 1.0, longer, source_id or row["source_id"], row["id"]),
            )

    # ---------- 检索 ----------
    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """统一入口：默认走 RRF 混合检索；`search.rrf=false` 时退回串行版本。"""
        if cfg().get("search.rrf", True):
            from .retrieval import search as _search_v2

            return _search_v2(self, query, limit=limit)
        from .retrieval import search_legacy

        return search_legacy(self, query, limit=limit)

    def _alias_hits(self, q: str, limit: int) -> list[dict[str, Any]]:
        """问题里出现了实体/概念的别名 → 直接抬高相关笔记。"""
        ql = q.lower()
        with self.connect() as c:
            ents = c.execute("SELECT id, name, aliases FROM entities").fetchall()
            cons = c.execute("SELECT id, name, aliases FROM concepts").fetchall()
        matched: list[tuple[str, int]] = []
        for kind, rows in (("entity", ents), ("concept", cons)):
            for r in rows:
                names = [r["name"]] + [str(a) for a in (from_json(r["aliases"], []) or [])]
                if any(len(n) >= 2 and n.lower() in ql for n in names if n):
                    matched.append((kind, int(r["id"])))
                    if len(matched) >= 6:
                        break
            if len(matched) >= 6:
                break
        if not matched:
            return []
        out: list[dict[str, Any]] = []
        with self.connect() as c:
            for kind, oid in matched:
                rows = c.execute(
                    "SELECT DISTINCT n.id, n.title, n.slug, n.path, n.kind, n.summary"
                    " FROM edges g JOIN notes n ON n.id=g.src_id AND n.kind IN ('source','synthetic')"
                    " WHERE g.src_type='note' AND g.dst_type=? AND g.dst_id=?"
                    " ORDER BY n.updated_at DESC LIMIT ?",
                    (kind, oid, limit),
                ).fetchall()
                for r in rows:
                    out.append({
                        "type": "note", "id": r["id"], "title": r["title"], "slug": r["slug"],
                        "path": r["path"], "kind": r["kind"],
                        "snippet": (r["summary"] or "")[:240], "score": -100.0,
                    })
                if len(out) >= limit:
                    break
        return out[:limit]

    def _fts(self, fts_q: str, limit: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self.connect() as c:
            try:
                rows = c.execute(
                    "SELECT n.id, n.title, n.slug, n.path, n.kind,"
                    " snippet(notes_fts, 2, '…', '…', '…', 24) AS snip, bm25(notes_fts) AS score"
                    " FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid"
                    " WHERE notes_fts MATCH ? ORDER BY score LIMIT ?",
                    (fts_q, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for r in rows:
                out.append({
                    "type": "note", "id": r["id"], "title": r["title"], "slug": r["slug"],
                    "path": r["path"], "kind": r["kind"], "snippet": r["snip"],
                    "score": float(r["score"]),
                })
            if len(out) < limit:
                try:
                    rows = c.execute(
                        "SELECT c.source_id, c.text, bm25(chunks_fts) AS score, s.title"
                        " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
                        " JOIN sources s ON s.id = c.source_id"
                        " WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
                        (fts_q, limit - len(out)),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                for r in rows:
                    out.append({
                        "type": "chunk", "id": r["source_id"], "title": r["title"],
                        "snippet": (r["text"] or "")[:400], "score": float(r["score"]),
                    })
        return out

    def _like(self, terms: list[str], limit: int) -> list[dict[str, Any]]:
        """多关键词 OR 兜底：短词（<3 字）与 trigram 无法命中时走这里。"""
        out: list[dict[str, Any]] = []
        note_where = " OR ".join(
            ["title LIKE ?", "summary LIKE ?", "body_md LIKE ?"] * len(terms)
        )
        note_params: list[Any] = []
        for t in terms:
            note_params += [f"%{t}%"] * 3
        with self.connect() as c:
            rows = c.execute(
                f"SELECT id, title, slug, path, kind, summary, body_md FROM notes"
                f" WHERE {note_where} ORDER BY updated_at DESC LIMIT ?",
                (*note_params, limit),
            ).fetchall()
            for r in rows:
                body = r["body_md"] or r["summary"] or ""
                i = min([body.find(t) for t in terms if body.find(t) >= 0] or [-1])
                snip = body[max(0, i - 60): i + 240] if i >= 0 else body[:240]
                out.append({
                    "type": "note", "id": r["id"], "title": r["title"], "slug": r["slug"],
                    "path": r["path"], "kind": r["kind"], "snippet": snip, "score": 0.0,
                })
            if len(out) < limit and terms:
                chunk_where = " OR ".join(["c.text LIKE ?"] * len(terms))
                rows = c.execute(
                    f"SELECT c.source_id, c.text, s.title FROM chunks c"
                    f" JOIN sources s ON s.id=c.source_id WHERE {chunk_where} LIMIT ?",
                    (*[f"%{t}%" for t in terms], limit - len(out)),
                ).fetchall()
                for r in rows:
                    text = r["text"] or ""
                    idx = min([text.find(t) for t in terms if text.find(t) >= 0] or [-1])
                    out.append({
                        "type": "chunk", "id": r["source_id"], "title": r["title"],
                        "snippet": text[max(0, idx - 60): idx + 300] if idx >= 0 else text[:300],
                        "score": 0.0,
                    })
        return out

    def graph(self, limit: int = 300) -> dict[str, Any]:
        with self.connect() as c:
            nodes: list[dict[str, Any]] = []
            min_src = int(cfg().get("vault.min_sources_for_page", 2))
            ents = c.execute(
                "SELECT id, name, type, summary FROM entities"
                " WHERE src_count >= ? OR status='confirmed'"
                " ORDER BY updated_at DESC LIMIT ?",
                (min_src, limit),
            ).fetchall()
            cons = c.execute(
                "SELECT id, name, definition FROM concepts ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            srcs = c.execute(
                "SELECT id, title, kind FROM sources ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            for r in ents:
                nodes.append({"id": f"entity:{r['id']}", "label": r["name"], "group": "entity"})
            for r in cons:
                nodes.append({"id": f"concept:{r['id']}", "label": r["name"], "group": "concept"})
            for r in srcs:
                nodes.append({"id": f"source:{r['id']}", "label": r["title"][:40], "group": r["kind"]})
            ids = {n["id"] for n in nodes}
            links = []
            for r in c.execute(
                "SELECT src_type, src_id, dst_type, dst_id, rel, weight FROM edges LIMIT 4000"
            ).fetchall():
                a = f"{r['src_type']}:{r['src_id']}"
                b = f"{r['dst_type']}:{r['dst_id']}"
                if a in ids and b in ids:
                    links.append({"source": a, "target": b, "rel": r["rel"], "weight": r["weight"]})
            return {"nodes": nodes, "links": links}

    # ---------- 任务队列 ----------
    def enqueue(self, kind: str, payload: dict[str, Any]) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO jobs(kind, payload, status, created_at) VALUES(?,?, 'pending', ?)",
                (kind, to_json(payload), now_iso()),
            )
            return int(cur.lastrowid)

    def claim_job(self) -> dict[str, Any] | None:
        """单条 UPDATE 原子认领：多 worker 并发时同一 job 只会被领走一次。"""
        with self.tx() as c:
            row = c.execute(
                "UPDATE jobs SET status='running', started_at=?, attempts=attempts+1"
                " WHERE id = (SELECT id FROM jobs WHERE status='pending'"
                "             ORDER BY id LIMIT 1)"
                " RETURNING *",
                (now_iso(),),
            ).fetchone()
        return dict(row) if row else None

    def requeue_stale(self, minutes: int = 30) -> int:
        """running 但超过 N 分钟没动静的任务，放回 pending（崩溃恢复）。"""
        from datetime import datetime, timedelta

        cutoff = (datetime.now().astimezone() - timedelta(minutes=minutes)).strftime(
            "%Y-%m-%dT%H:%M:%S%z")
        with self.tx() as c:
            cur = c.execute(
                "UPDATE jobs SET status='pending', error='requeued: stale running'"
                " WHERE status='running' AND COALESCE(started_at, created_at) < ?",
                (cutoff,),
            )
            return int(cur.rowcount or 0)

    def finish_job(self, job_id: int, ok: bool, error: str | None = None,
                   result: dict[str, Any] | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE jobs SET status=?, finished_at=?, error=?, progress=?, stage=?, result=?"
                " WHERE id=?",
                ("done" if ok else "failed", now_iso(), error,
                 100 if ok else 0, "完成" if ok else "失败",
                 to_json(result) if result is not None else None, job_id),
            )

    def update_job(self, job_id: int, *, progress: int | None = None,
                   stage: str | None = None) -> None:
        sets, params = [], []
        if progress is not None:
            sets.append("progress=?")
            params.append(max(0, min(100, int(progress))))
        if stage is not None:
            sets.append("stage=?")
            params.append(stage)
        if not sets:
            return
        with self.tx() as c:
            c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*params, job_id))

    def job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["payload"] = from_json(out.get("payload"), {})
        out["result"] = from_json(out.get("result"), None)
        return out

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT id, kind, status, progress, stage, error, created_at, finished_at"
                " FROM jobs ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 对话 ----------
    def create_conversation(self, title: str = "新对话") -> str:
        import uuid

        cid = uuid.uuid4().hex[:16]
        with self.tx() as c:
            c.execute(
                "INSERT INTO conversations(id, title, created_at, updated_at) VALUES(?,?,?,?)",
                (cid, title, now_iso(), now_iso()),
            )
        return cid

    def touch_conversation(self, conv_id: str, title: str | None = None) -> None:
        with self.tx() as c:
            if title:
                c.execute(
                    "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                    (title[:80], now_iso(), conv_id),
                )
            else:
                c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_iso(), conv_id))

    def add_message(self, conv_id: str, role: str, content: str,
                    citations: list[dict[str, Any]] | None = None) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO conversation_messages(conv_id, role, content, citations, created_at)"
                " VALUES(?,?,?,?,?)",
                (conv_id, role, content, to_json(citations) if citations else None, now_iso()),
            )
            c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_iso(), conv_id))
            return int(cur.lastrowid)

    def conversation(self, conv_id: str) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if not row:
                return None
            msgs = c.execute(
                "SELECT id, role, content, citations, created_at FROM conversation_messages"
                " WHERE conv_id=? ORDER BY id", (conv_id,),
            ).fetchall()
        return {
            "id": row["id"], "title": row["title"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "messages": [
                {"id": m["id"], "role": m["role"], "content": m["content"],
                 "citations": from_json(m["citations"], []), "created_at": m["created_at"]}
                for m in msgs
            ],
        }

    def list_conversations(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT c.id, c.title, c.created_at, c.updated_at,"
                " (SELECT COUNT(*) FROM conversation_messages m WHERE m.conv_id=c.id) AS n,"
                " (SELECT content FROM conversation_messages m WHERE m.conv_id=c.id"
                "  ORDER BY m.id DESC LIMIT 1) AS last"
                " FROM conversations c ORDER BY c.updated_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_conversation(self, conv_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM conversation_messages WHERE conv_id=?", (conv_id,))
            c.execute("DELETE FROM conversations WHERE id=?", (conv_id,))

    def stats(self) -> dict[str, int]:
        with self.connect() as c:
            def n(sql: str) -> int:
                return int(c.execute(sql).fetchone()[0])

            return {
                "sources": n("SELECT COUNT(*) FROM sources"),
                "documents": n("SELECT COUNT(*) FROM documents"),
                "notes": n("SELECT COUNT(*) FROM notes"),
                "entities": n("SELECT COUNT(*) FROM entities"),
                "concepts": n("SELECT COUNT(*) FROM concepts"),
                "edges": n("SELECT COUNT(*) FROM edges"),
                "jobs_pending": n("SELECT COUNT(*) FROM jobs WHERE status='pending'"),
                "jobs_failed": n("SELECT COUNT(*) FROM jobs WHERE status='failed'"),
                "sensitive_sources": n(
                    "SELECT COUNT(*) FROM sources WHERE json_extract(meta, '$.sensitive') = 1"
                ),
            }


def _terms(q: str) -> list[str]:
    """把自然语言查询切成检索词。

    中文没有空格：长句拆成 3 字滑窗（trigram 索引天然对齐），兼顾召回与精度；
    英文按空白/标点切。
    """
    import re

    raw = [t for t in re.split(r"[\s,，。、;；:：!！?？()（）\"'“”‘’]+", q) if t]
    out: list[str] = []
    for t in raw:
        if _has_cjk(t) and len(t) > 4:
            grams = [t[i:i + 3] for i in range(len(t) - 2)]
            out.extend(grams[:14])
        else:
            out.append(t)
    seen: set[str] = set()
    dedup: list[str] = []
    for t in out:
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            dedup.append(t)
    return dedup


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


_db: Database | None = None


def db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db
