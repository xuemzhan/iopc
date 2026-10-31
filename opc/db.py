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

-- 边 × 来源：weight = 覆盖该关系的不同来源数（同一来源只计一次）
CREATE TABLE IF NOT EXISTS edge_sources (
  edge_id INTEGER NOT NULL,
  source_id INTEGER NOT NULL,
  PRIMARY KEY(edge_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_edge_sources_src ON edge_sources(source_id);

-- 受控类型（M1）：typefix 只允许落到这里登记的枚举
CREATE TABLE IF NOT EXISTS ontology_types (
  kind TEXT NOT NULL,               -- entity | concept
  name TEXT NOT NULL,
  description TEXT,
  builtin INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(kind, name)
);

-- 归并候选（M1）：只提议，永不自动合并
CREATE TABLE IF NOT EXISTS merge_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL DEFAULT 'entity',
  a_id INTEGER NOT NULL,
  b_id INTEGER NOT NULL,
  score REAL NOT NULL DEFAULT 0,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'open',   -- open | merged | rejected
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(kind, a_id, b_id)
);
CREATE INDEX IF NOT EXISTS idx_merge_candidates_status ON merge_candidates(status, score DESC);

-- 语义检索（WP-07）：向量按 (target_type,target_id,model) 唯一；float32 小端 blob
CREATE TABLE IF NOT EXISTS embeddings (
  target_type TEXT NOT NULL,
  target_id INTEGER NOT NULL,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL DEFAULT 0,
  vec BLOB NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(target_type, target_id, model)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);

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

-- UI 操作观察：只存事件名与匿名属性，不存正文/查询词，用于后续交互优化
CREATE TABLE IF NOT EXISTS ui_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  session_id TEXT,
  event TEXT NOT NULL,
  props TEXT,
  client TEXT
);
CREATE INDEX IF NOT EXISTS idx_ui_events_ts ON ui_events(ts);
CREATE INDEX IF NOT EXISTS idx_ui_events_event ON ui_events(event, id DESC);

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

CREATE TABLE IF NOT EXISTS graph_edits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  undone INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_notes_source ON notes(source_id);
CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_entities_updated ON entities(updated_at DESC);
"""


FTS_VERSION = "trigram-1"
# 只有这些关系按"覆盖来源数"计权；其余（related_to/distills/人工连线）是标量边
SOURCE_COUNTED = {"mentions", "covers", "co_occurs"}

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
            # 操作观察只保留 180 天，避免长期增长
            from datetime import datetime, timedelta

            cutoff = (datetime.now().astimezone() - timedelta(days=180)).strftime(
                "%Y-%m-%dT%H:%M:%S%z")
            conn.execute("DELETE FROM ui_events WHERE ts < ?", (cutoff,))
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
            ("priority", "ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"),
            ("cancel_requested", "ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"),
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

        # P0-C：locked 不可变基准（实体/概念）+ 冲突台账
        for tbl in ("entities", "concepts"):
            lcols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")}
            if "locked" not in lcols:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
            if "locked_at" not in lcols:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN locked_at TEXT")
            if "locked_by" not in lcols:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN locked_by TEXT")
        # 边权语义：weight = 覆盖该关系的不同来源数（旧数据一次性回填来源集合）
        if not conn.execute("SELECT value FROM meta WHERE key='edge_sources_v1'").fetchone():
            conn.execute(
                "INSERT OR IGNORE INTO edge_sources(edge_id, source_id)"
                " SELECT id, source_id FROM edges WHERE source_id IS NOT NULL"
            )
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('edge_sources_v1','1')")

        # M1 本体治理：归并指针 + 受控类型枚举
        ecols = {r[1] for r in conn.execute("PRAGMA table_info(entities)")}
        if "merged_into" not in ecols:
            conn.execute("ALTER TABLE entities ADD COLUMN merged_into INTEGER")
        ccols = {r[1] for r in conn.execute("PRAGMA table_info(concepts)")}
        if "type" not in ccols:
            conn.execute("ALTER TABLE concepts ADD COLUMN type TEXT")
        conn.executemany(
            "INSERT OR IGNORE INTO ontology_types(kind, name, description, builtin)"
            " VALUES('entity', ?, '受控类型（pipeline.ENTITY_TYPES）', 1)",
            [("person",), ("org",), ("system",), ("tool",), ("project",),
             ("place",), ("event",), ("other",)],
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS conflicts ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  kind TEXT NOT NULL,"
            "  ref_id INTEGER NOT NULL,"
            "  field TEXT NOT NULL,"
            "  locked_value TEXT,"
            "  incoming_value TEXT,"
            "  incoming_source INTEGER,"
            "  status TEXT NOT NULL DEFAULT 'open',"
            "  verdict TEXT,"
            "  created_at TEXT NOT NULL,"
            "  decided_at TEXT,"
            "  UNIQUE(kind, ref_id, field, incoming_source)"
            ")"
        )
        Database._ensure_annotations(conn)
        conn.commit()

    # ---------- locked 基准 / 冲突台账 ----------
    def object_row(self, kind: str, ident: int | str) -> sqlite3.Row | None:
        """按 id 或归一化 key 取实体/概念行（含锁定字段）。"""
        table = "entities" if kind == "entity" else "concepts"
        with self.connect() as c:
            if isinstance(ident, int):
                return c.execute(f"SELECT * FROM {table} WHERE id=?", (ident,)).fetchone()
            key = str(ident)
            if key.isdigit():
                row = c.execute(f"SELECT * FROM {table} WHERE id=?", (int(key),)).fetchone()
                if row:
                    return row
            from .util import normalize_key  # 局部导入避免循环

            return c.execute(f"SELECT * FROM {table} WHERE key=?", (normalize_key(key),)).fetchone()

    def set_locked(self, kind: str, oid: int, locked: bool, *, by: str = "") -> None:
        from datetime import datetime, timezone

        table = "entities" if kind == "entity" else "concepts"
        with self.tx() as c:
            c.execute(
                f"UPDATE {table} SET locked=?, locked_at=?, locked_by=? WHERE id=?",
                (1 if locked else 0, now_iso() if locked else None, by if locked else "", oid),
            )

    def add_conflict(self, kind: str, ref_id: int, field: str, locked_value: str,
                     incoming_value: str, incoming_source: int | None) -> int:
        """登记冲突（幂等：同 (kind, ref, field, source) 复用既有行）。返回冲突行 id。"""
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO conflicts(kind, ref_id, field, locked_value,"
                " incoming_value, incoming_source, created_at) VALUES(?,?,?,?,?,?,?)",
                (kind, ref_id, field, locked_value, incoming_value, incoming_source, now_iso()),
            )
            if cur.rowcount > 0:
                return int(cur.lastrowid)
            row = c.execute(
                "SELECT id FROM conflicts WHERE kind=? AND ref_id=? AND field=?"
                " AND incoming_source=?", (kind, ref_id, field, incoming_source),
            ).fetchone()
            return int(row["id"]) if row else -1

    def open_conflicts(self, limit: int = 50) -> list[dict[str, Any]]:
        from .util import from_json

        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM conflicts WHERE status='open' ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["verdict"] = from_json(d.get("verdict") or "", {})
            except Exception:
                pass
            out.append(d)
        return out

    def decide_conflict(self, cid: int, status: str, verdict: dict[str, Any] | None = None) -> None:
        from .util import to_json

        with self.tx() as c:
            c.execute(
                "UPDATE conflicts SET status=?, verdict=?, decided_at=? WHERE id=?",
                (status, to_json(verdict or {}), now_iso(), cid),
            )

    def conflict(self, cid: int) -> dict[str, Any] | None:
        from .util import from_json

        with self.connect() as c:
            row = c.execute("SELECT * FROM conflicts WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["verdict"] = from_json(d.get("verdict") or "", {}) or {}
        return d

    # ---------- 注记（P0-F：随手要点/疑问，带上下文锚点） ----------
    @staticmethod
    def _ensure_annotations(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS annotations ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  kind TEXT NOT NULL DEFAULT 'note',"
            "  parent_id INTEGER,"
            "  anchor_kind TEXT NOT NULL DEFAULT 'none',"
            "  anchor_ref TEXT,"
            "  anchor_pos TEXT,"
            "  anchor_source_id INTEGER,"
            "  anchor_title TEXT,"
            "  text TEXT NOT NULL,"
            "  status TEXT NOT NULL DEFAULT 'open',"
            "  focused INTEGER NOT NULL DEFAULT 0,"
            "  meta TEXT,"
            "  created_at TEXT NOT NULL,"
            "  updated_at TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_annotations_anchor"
            " ON annotations(anchor_kind, anchor_ref)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_annotations_status ON annotations(status, focused)"
        )

    def add_annotation(
        self, text: str, *, kind: str = "note", parent_id: int | None = None,
        anchor_kind: str = "none", anchor_ref: str | None = None,
        anchor_pos: str | None = None, anchor_source_id: int | None = None,
        anchor_title: str | None = None, meta: dict[str, Any] | None = None,
    ) -> int:
        if not (text or "").strip():
            raise ValueError("注记内容为空")
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO annotations(kind, parent_id, anchor_kind, anchor_ref,"
                " anchor_pos, anchor_source_id, anchor_title, text, meta, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (kind, parent_id, anchor_kind or "none", anchor_ref, anchor_pos,
                 anchor_source_id, anchor_title, text.strip(),
                 to_json(meta or {}), now_iso(), now_iso()),
            )
            return int(cur.lastrowid)

    def annotation(self, aid: int) -> dict[str, Any] | None:
        from .util import from_json

        with self.connect() as c:
            row = c.execute("SELECT * FROM annotations WHERE id=?", (aid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["meta"] = from_json(d.get("meta") or "", {}) or {}
        return d

    def list_annotations(
        self, *, scope: str = "recent", anchor: str | None = None, limit: int = 60,
    ) -> list[dict[str, Any]]:
        """scope: recent / focused / open / answered | anchor: 'meeting:12'。"""
        from .util import from_json

        where, args = [], []
        if scope == "focused":
            where.append("focused=1")
        elif scope == "open":
            where.append("status='open'")
        elif scope == "answered":
            where.append("status='answered'")
        elif scope == "archived":
            where.append("status='archived'")
        if anchor:
            akind, _, aref = anchor.partition(":")
            where.append("anchor_kind=?")
            args.append(akind)
            if aref:
                where.append("anchor_ref=?")
                args.append(aref)
        with self.connect() as c:
            sql = "SELECT * FROM annotations"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id DESC LIMIT ?"
            rows = c.execute(sql, (*args, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = from_json(d.get("meta") or "", {}) or {}
            out.append(d)
        return out

    def set_annotation_focus(self, aid: int, on: bool) -> None:
        if not self.annotation(aid):
            raise ValueError(f"注记 {aid} 不存在")
        with self.tx() as c:
            c.execute("UPDATE annotations SET focused=?, updated_at=? WHERE id=?",
                      (1 if on else 0, now_iso(), aid))

    def archive_annotation(self, aid: int) -> None:
        if not self.annotation(aid):
            raise ValueError(f"注记 {aid} 不存在")
        with self.tx() as c:
            c.execute("UPDATE annotations SET status='archived', updated_at=? WHERE id=?",
                      (now_iso(), aid))

    def reply_annotation(self, aid: int, text: str) -> tuple[int, dict[str, Any]]:
        """回应一条注记：子注记回复 + 父注记自动 answered+focused（A4）。"""
        parent = self.annotation(aid)
        if not parent:
            raise ValueError(f"注记 {aid} 不存在")
        if parent["parent_id"]:
            raise ValueError("只可对父注记回应")
        rid = self.add_annotation(
            text, kind="reply", parent_id=aid,
            anchor_kind=parent["anchor_kind"], anchor_ref=parent["anchor_ref"],
            anchor_pos=parent["anchor_pos"], anchor_source_id=parent["anchor_source_id"],
            anchor_title=parent["anchor_title"], meta={"for": aid},
        )
        with self.tx() as c:
            c.execute("UPDATE annotations SET status='answered', focused=1, updated_at=?"
                      " WHERE id=?", (now_iso(), aid))
        return rid, self.annotation(aid)

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

    # ---------- 语义检索向量（WP-07） ----------
    @staticmethod
    def _pack_vec(vec: list[float]) -> bytes:
        import struct

        return struct.pack(f"<{len(vec)}f", *[float(x) for x in vec])

    @staticmethod
    def _unpack_vec(blob: bytes) -> list[float]:
        import struct

        n = len(blob) // 4
        return list(struct.unpack(f"<{n}f", blob))

    def put_embedding(self, target_type: str, target_id: int, model: str,
                      vec: list[float]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO embeddings(target_type, target_id, model, dim, vec, updated_at)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(target_type, target_id, model) DO UPDATE SET"
                " dim=excluded.dim, vec=excluded.vec, updated_at=excluded.updated_at",
                (target_type, int(target_id), model, len(vec), self._pack_vec(vec), now_iso()),
            )

    def embeddings(self, model: str, target_type: str = "note") -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT target_id, dim, vec FROM embeddings"
                " WHERE model=? AND target_type=?", (model, target_type)
            ).fetchall()
        return [{"target_id": int(r["target_id"]), "dim": int(r["dim"]),
                 "vec": self._unpack_vec(r["vec"])} for r in rows]

    def embedding_ids(self, model: str, target_type: str = "note") -> set[int]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT target_id FROM embeddings WHERE model=? AND target_type=?",
                (model, target_type)).fetchall()
        return {int(r["target_id"]) for r in rows}

    def embedding_stats(self, model: str = "") -> dict[str, Any]:
        with self.connect() as c:
            if model:
                n = c.execute("SELECT COUNT(*) FROM embeddings WHERE model=?",
                              (model,)).fetchone()[0]
            else:
                n = c.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            models = [r[0] for r in c.execute(
                "SELECT DISTINCT model FROM embeddings").fetchall()]
        return {"count": int(n), "models": models}

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
        # 只有这几类关系按"覆盖来源数"计权；related_to/distills/人工连线是标量边
        if rel not in SOURCE_COUNTED or source_id is None:
            self._add_edge_scalar(src_type, src_id, dst_type, dst_id, rel,
                                  weight=weight, evidence=evidence, source_id=source_id)
            return
        with self.tx() as c:
            row = c.execute(
                "SELECT id, evidence FROM edges"
                " WHERE src_type=? AND src_id=? AND dst_type=? AND dst_id=? AND rel=?",
                (src_type, src_id, dst_type, dst_id, rel),
            ).fetchone()
            if row is None:
                cur = c.execute(
                    "INSERT INTO edges(src_type, src_id, dst_type, dst_id, rel, weight, evidence,"
                    " source_id, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (src_type, src_id, dst_type, dst_id, rel, weight, evidence, source_id,
                     now_iso()),
                )
                edge_id = int(cur.lastrowid)
            else:
                edge_id = int(row["id"])
                longer = (evidence if len(evidence or "") > len(row["evidence"] or "")
                          else row["evidence"])
                evidence = longer
            # 同一来源只计一次；weight = 覆盖该关系的不同来源数
            c.execute(
                "INSERT OR IGNORE INTO edge_sources(edge_id, source_id) VALUES(?,?)",
                (edge_id, source_id),
            )
            count = c.execute(
                "SELECT COUNT(*) FROM edge_sources WHERE edge_id=?", (edge_id,)
            ).fetchone()[0]
            c.execute(
                "UPDATE edges SET weight=?, evidence=?, source_id=? WHERE id=?",
                (float(count), evidence, source_id, edge_id),
            )

    def _add_edge_scalar(self, src_type: str, src_id: int, dst_type: str, dst_id: int,
                         rel: str, *, weight: float, evidence: str,
                         source_id: int | None = None) -> None:
        """不按来源计数的边（distills / related_to / 人工连线）：只写一次，重跑幂等。"""
        with self.tx() as c:
            row = c.execute(
                "SELECT id, evidence, source_id FROM edges"
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
            longer = evidence if len(evidence or "") > len(row["evidence"] or "") else row["evidence"]
            keep_source = row["source_id"] if row["source_id"] is not None else source_id
            if longer != row["evidence"] or keep_source != row["source_id"]:
                c.execute("UPDATE edges SET evidence=?, source_id=? WHERE id=?",
                          (longer, keep_source, row["id"]))

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
            ents = c.execute(
                "SELECT id, name, aliases FROM entities"
                " WHERE COALESCE(status,'') NOT IN ('merged','rejected')"
            ).fetchall()
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
                        "SELECT c.source_id, c.text, bm25(chunks_fts) AS score, s.title,"
                        " (SELECT n.path FROM notes n WHERE n.source_id=c.source_id"
                        "   AND n.kind='source' ORDER BY n.id DESC LIMIT 1) AS path,"
                        " (SELECT n.slug FROM notes n WHERE n.source_id=c.source_id"
                        "   AND n.kind='source' ORDER BY n.id DESC LIMIT 1) AS slug"
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
                        "path": r["path"] or "", "slug": r["slug"] or "", "kind": "source",
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
                    f"SELECT c.source_id, c.text, s.title,"
                    f" (SELECT n.path FROM notes n WHERE n.source_id=c.source_id"
                    f"   AND n.kind='source' ORDER BY n.id DESC LIMIT 1) AS path,"
                    f" (SELECT n.slug FROM notes n WHERE n.source_id=c.source_id"
                    f"   AND n.kind='source' ORDER BY n.id DESC LIMIT 1) AS slug"
                    f" FROM chunks c"
                    f" JOIN sources s ON s.id=c.source_id WHERE {chunk_where} LIMIT ?",
                    (*[f"%{t}%" for t in terms], limit - len(out)),
                ).fetchall()
                for r in rows:
                    text = r["text"] or ""
                    idx = min([text.find(t) for t in terms if text.find(t) >= 0] or [-1])
                    out.append({
                        "type": "chunk", "id": r["source_id"], "title": r["title"],
                        "path": r["path"] or "", "slug": r["slug"] or "", "kind": "source",
                        "snippet": text[max(0, idx - 60): idx + 300] if idx >= 0 else text[:300],
                        "score": 0.0,
                    })
        return out

    def graph(self, limit: int = 300, *, min_sources: int | None = None) -> dict[str, Any]:
        """图谱数据。

        准入规则与建页规则一致：实体/概念至少被 min_sources 个来源覆盖才进图
        （locked 条目不收起）。单个 limit 是「每个类别」的上限，不是总数。
        """
        with self.connect() as c:
            min_src = int(min_sources if min_sources is not None
                          else cfg().get("vault.min_sources_for_page", 2))
            nodes: list[dict[str, Any]] = []
            ents = c.execute(
                "SELECT id, name, type, summary, page_path FROM entities"
                " WHERE (src_count >= ? OR status='confirmed' OR locked=1)"
                " AND COALESCE(status,'') NOT IN ('merged','rejected')"
                " ORDER BY updated_at DESC LIMIT ?",
                (min_src, limit),
            ).fetchall()
            cons = c.execute(
                "SELECT id, name, definition, aliases, page_path FROM concepts c"
                " WHERE locked=1 OR ("
                "   SELECT COUNT(DISTINCT e.source_id) FROM edges e"
                "   WHERE e.src_type='source' AND e.dst_type='concept'"
                "     AND e.dst_id=c.id AND e.rel='covers' AND e.source_id IS NOT NULL"
                " ) >= ?"
                " ORDER BY updated_at DESC LIMIT ?",
                (min_src, limit),
            ).fetchall()
            srcs = c.execute(
                "SELECT s.id, s.title, s.kind, s.uri, s.captured_at,"
                "  (SELECT n.path FROM notes n WHERE n.source_id=s.id AND n.kind='source'"
                "    ORDER BY n.id DESC LIMIT 1) AS note_path,"
                "  (SELECT n.slug FROM notes n WHERE n.source_id=s.id AND n.kind='source'"
                "    ORDER BY n.id DESC LIMIT 1) AS note_slug"
                " FROM sources s ORDER BY s.id DESC LIMIT ?", (limit,)
            ).fetchall()
            for r in ents:
                nodes.append({"id": f"entity:{r['id']}", "ref": int(r["id"]),
                              "label": r["name"], "group": "entity", "type": r["type"],
                              "summary": r["summary"] or "", "path": r["page_path"] or ""})
            for r in cons:
                nodes.append({"id": f"concept:{r['id']}", "ref": int(r["id"]),
                              "label": r["name"], "group": "concept", "type": "concept",
                              "summary": r["definition"] or "", "path": r["page_path"] or ""})
            for r in srcs:
                nodes.append({"id": f"source:{r['id']}", "ref": int(r["id"]),
                              "label": r["title"][:60], "group": r["kind"], "type": r["kind"],
                              "summary": "", "path": r["note_path"] or "",
                              "uri": r["uri"] or "", "captured_at": r["captured_at"] or ""})
            ids = {n["id"] for n in nodes}
            links = []
            for r in c.execute(
                "SELECT id, src_type, src_id, dst_type, dst_id, rel, weight, evidence, source_id"
                " FROM edges ORDER BY weight DESC, id DESC LIMIT 8000"
            ).fetchall():
                a = f"{r['src_type']}:{r['src_id']}"
                b = f"{r['dst_type']}:{r['dst_id']}"
                if a in ids and b in ids:
                    links.append({"id": int(r["id"]), "source": a, "target": b,
                                  "rel": r["rel"], "weight": r["weight"],
                                  "evidence": (r["evidence"] or "")[:240],
                                  "source_id": r["source_id"]})
            total_concepts = int(c.execute("SELECT COUNT(*) FROM concepts").fetchone()[0])
            total_entities = int(c.execute("SELECT COUNT(*) FROM entities").fetchone()[0])
            return {
                "nodes": nodes, "links": links,
                "meta": {
                    "min_sources": min_src,
                    "shown": {"entities": len(ents), "concepts": len(cons), "sources": len(srcs)},
                    "total": {"entities": total_entities, "concepts": total_concepts,
                              "sources": int(c.execute("SELECT COUNT(*) FROM sources")
                                             .fetchone()[0])},
                    "gated_concepts": max(0, total_concepts - len(cons)),
                },
            }

    # ---------- 任务队列 ----------
    def enqueue(self, kind: str, payload: dict[str, Any], *, priority: int = 0) -> int:
        """入队。priority 越大越先被认领（交互任务 10 / 提炼 8 / 批处理 5）。"""
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO jobs(kind, payload, status, priority, created_at)"
                " VALUES(?,?, 'pending', ?, ?)",
                (kind, to_json(payload), int(priority), now_iso()),
            )
            return int(cur.lastrowid)

    def claim_job(self) -> dict[str, Any] | None:
        """单条 UPDATE 原子认领：多 worker 并发时同一 job 只会被领走一次。"""
        with self.tx() as c:
            row = c.execute(
                "UPDATE jobs SET status='running', started_at=?, attempts=attempts+1"
                " WHERE id = (SELECT id FROM jobs WHERE status='pending'"
                "             ORDER BY priority DESC, id LIMIT 1)"
                " RETURNING *",
                (now_iso(),),
            ).fetchone()
        return dict(row) if row else None

    def request_cancel(self, job_id: int) -> dict[str, Any]:
        """取消任务：pending 直接置 cancelled；running 置取消标记，由 worker 在检查点收尾。"""
        with self.tx() as c:
            row = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return {"ok": False, "reason": "not_found"}
            status = row["status"]
            if status in ("done", "failed", "cancelled"):
                return {"ok": False, "reason": status}
            if status == "pending":
                c.execute(
                    "UPDATE jobs SET status='cancelled', cancel_requested=1, finished_at=?,"
                    " stage='已取消', error='用户取消' WHERE id=?",
                    (now_iso(), job_id),
                )
            else:
                c.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
        return {"ok": True, "status": "cancelled" if status == "pending" else "cancelling"}

    def cancel_requested(self, job_id: int) -> bool:
        with self.connect() as c:
            row = c.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def retry_job(self, job_id: int) -> dict[str, Any]:
        """把 failed/cancelled 任务重新放回 pending；保持原 payload 与优先级。"""
        with self.tx() as c:
            row = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return {"ok": False, "reason": "not_found"}
            if row["status"] not in ("failed", "cancelled"):
                return {"ok": False, "reason": row["status"]}
            c.execute(
                "UPDATE jobs SET status='pending', progress=0, stage=NULL, error=NULL,"
                " result=NULL, cancel_requested=0, started_at=NULL, finished_at=NULL"
                " WHERE id=?", (job_id,),
            )
        return {"ok": True, "status": "pending"}

    def requeue_stale(self, minutes: int = 30) -> int:
        """running 但超过 N 分钟没动静的任务，放回 pending（崩溃恢复）。"""
        from datetime import datetime, timedelta

        cutoff = (datetime.now().astimezone() - timedelta(minutes=minutes)).strftime(
            "%Y-%m-%dT%H:%M:%S%z")
        with self.tx() as c:
            cancelled = c.execute(
                "UPDATE jobs SET status='cancelled', finished_at=?, stage='已取消',"
                " error='用户取消（超时回收）'"
                " WHERE status='running' AND cancel_requested=1"
                " AND COALESCE(started_at, created_at) < ?",
                (now_iso(), cutoff),
            ).rowcount
            cur = c.execute(
                "UPDATE jobs SET status='pending', error='requeued: stale running'"
                " WHERE status='running' AND cancel_requested=0"
                " AND COALESCE(started_at, created_at) < ?",
                (cutoff,),
            )
            return int((cur.rowcount or 0) + (cancelled or 0))

    def finish_job(self, job_id: int, ok: bool, error: str | None = None,
                   result: dict[str, Any] | None = None, *, status: str | None = None) -> None:
        final = status or ("done" if ok else "failed")
        with self.tx() as c:
            c.execute(
                "UPDATE jobs SET status=?, finished_at=?, error=?, progress=?, stage=?, result=?"
                " WHERE id=?",
                (final, now_iso(), error,
                 100 if final == "done" else 0,
                 {"done": "完成", "cancelled": "已取消", "failed": "失败"}.get(final, final),
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
                "SELECT id, kind, status, progress, stage, error, priority,"
                " cancel_requested, created_at, finished_at"
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

    def update_message(self, mid: int, content: str,
                       citations: list[dict[str, Any]] | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE conversation_messages SET content=?, citations=? WHERE id=?",
                (content, to_json(citations) if citations else None, mid),
            )

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

    def rename_conversation(self, conv_id: str, title: str) -> None:
        title = (title or "").strip()[:80] or "未命名"
        with self.tx() as c:
            c.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                      (title, now_iso(), conv_id))

    # ---------- 使用观察（UI telemetry） ----------
    def add_ui_event(self, session_id: str, event: str, props: dict[str, Any] | None = None,
                     client: str = "") -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO ui_events(ts, session_id, event, props, client)"
                " VALUES(?,?,?,?,?)",
                (now_iso(), (session_id or "")[:64], event[:64],
                 to_json(props or {})[:4000], (client or "")[:300]),
            )
            return int(cur.lastrowid)

    def ui_event_summary(self, days: int = 7, limit: int = 40) -> dict[str, Any]:
        """只返回计数，不返回任何正文；用于「使用观察」面板。"""
        from datetime import datetime, timedelta

        cutoff = (datetime.now().astimezone() - timedelta(days=max(1, min(days, 90)))).strftime(
            "%Y-%m-%dT%H:%M:%S%z")
        with self.connect() as c:
            rows = c.execute(
                "SELECT event, COUNT(*) n FROM ui_events WHERE ts >= ?"
                " GROUP BY event ORDER BY n DESC LIMIT ?", (cutoff, limit),
            ).fetchall()
            total = c.execute(
                "SELECT COUNT(*) FROM ui_events WHERE ts >= ?", (cutoff,)
            ).fetchone()[0]
            sessions = c.execute(
                "SELECT COUNT(DISTINCT session_id) FROM ui_events WHERE ts >= ? AND session_id != ''",
                (cutoff,),
            ).fetchone()[0]
            daily = c.execute(
                "SELECT substr(ts, 1, 10) d, COUNT(*) n FROM ui_events WHERE ts >= ?"
                " GROUP BY d ORDER BY d DESC LIMIT 14", (cutoff,),
            ).fetchall()
            fb = c.execute(
                "SELECT COALESCE(json_extract(props, '$.rating'), '?') r, COUNT(*) n"
                " FROM ui_events WHERE event='feedback' AND ts >= ?"
                " GROUP BY r", (cutoff,),
            ).fetchall()
        return {
            "days": days, "total": int(total), "sessions": int(sessions),
            "events": [{"event": r["event"], "n": int(r["n"])} for r in rows],
            "daily": [{"date": r["d"], "n": int(r["n"])} for r in daily],
            "feedback": {str(r["r"]): int(r["n"]) for r in fb},
        }

    # ---------- 注意力中心 ----------
    def attention(self, limit: int = 8) -> dict[str, Any]:
        """聚合需要人处理的项：失败任务 / 本体冲突 / 草稿页 / 未回应注记 / 排队任务。"""
        with self.connect() as c:
            def n(sql: str, args: tuple[Any, ...] = ()) -> int:
                return int(c.execute(sql, args).fetchone()[0])

            counts = {
                "jobs_failed": n("SELECT COUNT(*) FROM jobs WHERE status='failed'"),
                "jobs_pending": n("SELECT COUNT(*) FROM jobs WHERE status='pending'"),
                "conflicts": n("SELECT COUNT(*) FROM conflicts WHERE status='open'"),
                "drafts": n("SELECT COUNT(*) FROM notes WHERE status='draft'"),
                "annotations": n("SELECT COUNT(*) FROM annotations WHERE status='open'"
                                 " AND parent_id IS NULL"),
            }
            failed = [dict(r) for r in c.execute(
                "SELECT id, kind, error, created_at FROM jobs WHERE status='failed'"
                " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
            conflicts = [dict(r) for r in c.execute(
                "SELECT id, kind, ref_id, field, locked_value, incoming_value, created_at"
                " FROM conflicts WHERE status='open' ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]
            drafts = [dict(r) for r in c.execute(
                "SELECT n.id, n.title, n.path, n.source_id, s.kind AS source_kind"
                " FROM notes n LEFT JOIN sources s ON s.id=n.source_id"
                " WHERE n.status='draft' ORDER BY n.id DESC LIMIT ?", (limit,)
            ).fetchall()]
            notes = [dict(r) for r in c.execute(
                "SELECT id, text, anchor_kind, anchor_ref, anchor_pos, anchor_title,"
                " created_at FROM annotations WHERE status='open' AND parent_id IS NULL"
                " ORDER BY focused DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()]
        total = sum(counts.values())
        return {"counts": counts, "total": total, "failed": failed,
                "conflicts": conflicts, "drafts": drafts, "annotations": notes}

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
