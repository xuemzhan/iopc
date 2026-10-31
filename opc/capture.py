"""捕获服务：把任意来源落库为 source + document + chunks，然后交给提炼流水线。"""
from __future__ import annotations

from typing import Any

from .config import cfg
from .db import Database, db
from .pipeline import distill_source
from .util import sha, to_json
from .vault import Vault


def capture(
    *,
    kind: str,
    external_id: str,
    title: str,
    text: str,
    uri: str | None = None,
    author: str | None = None,
    occurred_at: str | None = None,
    meta: dict[str, Any] | None = None,
    publish: bool | None = None,
    force: bool = False,
    database: Database | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """幂等捕获：同内容重复捕获直接跳过（除非 force）。"""
    database = database or db()
    text = (text or "").strip()
    if not text:
        raise ValueError("正文为空")
    digest = sha(text)
    publish = bool(cfg().get("vault.auto_publish", True)) if publish is None else publish

    with database.connect() as c:
        row = c.execute(
            "SELECT s.id, s.content_hash,"
            " (SELECT COUNT(*) FROM notes n WHERE n.source_id=s.id AND n.status='published') AS pub"
            " FROM sources s WHERE s.kind=? AND s.external_id=?",
            (kind, external_id),
        ).fetchone()
    if row and row["content_hash"] == digest and row["pub"] and not force:
        return {"skipped": True, "reason": "内容未变化", "source_id": int(row["id"])}

    raw_path = None
    if cfg().get("vault.write_raw", True):
        try:
            v = Vault()
            raw_path = v.rel(
                v.write_inbox(
                    title,
                    text,
                    meta_line=f"kind={kind} external_id={external_id} uri={uri or '-'}",
                )
            )
        except Exception:
            raw_path = None

    source_id = database.upsert_source(
        kind,
        external_id,
        title,
        uri=uri,
        author=author,
        occurred_at=occurred_at,
        content_hash=digest,
        raw_path=raw_path,
        meta=meta or {},
    )
    doc_id = database.put_document(source_id, text)
    database.put_chunks(doc_id, source_id, [text])
    database.event("capture", {"kind": kind, "external_id": external_id, "source_id": source_id,
                               "chars": len(text)})

    if on_progress:
        on_progress(15, "已落库，开始提炼")
    result = distill_source(source_id, publish=publish, database=database,
                            on_progress=on_progress)
    result["skipped"] = False
    result["raw_path"] = raw_path
    return result


def capture_note(text: str, title: str | None = None, *, tags: list[str] | None = None,
                 publish: bool | None = None) -> dict[str, Any]:
    if not title:
        first = text.strip().splitlines()[0] if text.strip() else "随手记"
        title = first.strip().rstrip("：:。.，,；;")[:40] or "随手记"
    return capture(
        kind="note",
        external_id=sha(text, 16),
        title=title,
        text=text,
        meta={"tags": tags or [], "via": "cli"},
        publish=publish,
    )


def source_brief(database: Database | None = None, limit: int = 20) -> list[dict[str, Any]]:
    database = database or db()
    with database.connect() as c:
        rows = c.execute(
            "SELECT s.id, s.kind, s.title, s.captured_at, s.uri,"
            " n.path AS note_path, n.summary"
            " FROM sources s LEFT JOIN notes n ON n.source_id=s.id AND n.kind='source'"
            " ORDER BY s.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def pending_jobs(database: Database | None = None, limit: int = 50) -> list[dict[str, Any]]:
    database = database or db()
    with database.connect() as c:
        rows = c.execute(
            "SELECT id, kind, payload, status, attempts, created_at, error FROM jobs"
            " WHERE status IN ('pending','failed','running') ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def enqueue_distill(source_id: int, database: Database | None = None) -> int:
    database = database or db()
    return database.enqueue("distill", {"source_id": source_id})


def run_worker(once: bool = False, max_jobs: int = 100, database: Database | None = None) -> int:
    """处理 pending 任务，返回处理条数。"""
    import time

    database = database or db()
    done = 0
    while done < max_jobs:
        job = database.claim_job()
        if not job:
            break
        result: dict[str, Any] | None = None
        try:
            from .util import from_json

            payload = from_json(job["payload"], {}) or {}

            def progress(pct: int, stage: str) -> None:
                database.update_job(job["id"], progress=pct, stage=stage)

            if job["kind"] == "distill":
                result = distill_source(int(payload["source_id"]), database=database,
                                        on_progress=progress)
            elif job["kind"] == "capture":
                result = capture(**payload, database=database, on_progress=progress)
            elif job["kind"] == "ingest_meeting":
                from .connectors import get as get_connector

                conn = get_connector("imeeting")
                progress(10, "拉取会议转写/纪要/导图")
                result = conn.ingest(int(payload["meeting_id"]),  # type: ignore[attr-defined]
                                     force=bool(payload.get("force")))
            elif job["kind"] == "voice":
                from .voice import transcribe_and_capture

                result = transcribe_and_capture(payload["audio"], on_progress=progress)
            else:
                raise ValueError(f"未知任务类型：{job['kind']}")
            database.finish_job(job["id"], True, result=result)
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            database.finish_job(job["id"], False, err)
        done += 1
        if once:
            break
        time.sleep(0.05)
    return done
