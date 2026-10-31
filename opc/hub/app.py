"""OPC 中枢 HTTP 服务：REST + SSE + Web 控制台。

所有重活（LLM、抓取、写盘）都丢线程池，事件循环只做 IO 编排。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from .. import __version__
from ..capture import capture, capture_note, pending_jobs, run_worker, source_brief
from ..config import cfg
from ..connectors import get as get_connector
from ..connectors import status_all
from ..db import db
from ..pipeline import ask as ask_pipeline
from ..pipeline import prepare_ask, save_synthetic
from ..llm import llm
from ..vault import Vault

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    from ..scheduler import Watcher

    from ..scheduler import JobWorker

    w = Watcher()
    app.state.watcher = w
    worker = JobWorker()
    app.state.worker = worker
    if cfg().get("watch.enabled", True):
        w.start()
    worker.start()
    try:
        yield
    finally:
        w.stop()
        worker.stop()


app = FastAPI(title="OPC 中枢", version=__version__, docs_url="/api/docs",
              openapi_url="/api/openapi.json", lifespan=lifespan)

PUBLIC_PATHS = ("/static/", "/manifest.webmanifest", "/sw.js", "/icon-192.png",
                "/icon-512.png", "/favicon.ico")


@app.middleware("http")
async def token_guard(request, call_next):
    """配了令牌时，非本机请求必须带 token；本机永远放行。"""
    import os

    token = os.environ.get("OPC_TOKEN") or cfg().get("security.token", "")
    if not token:
        return await call_next(request)
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1", "localhost"):
        return await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith(PUBLIC_PATHS):
        return await call_next(request)
    supplied = request.headers.get("x-opc-token") or request.query_params.get("token") or ""
    if supplied != token:
        return JSONResponse({"detail": "需要访问令牌（?token=… 或 X-OPC-Token）"}, status_code=401)
    return await call_next(request)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ 模型
class CaptureIn(BaseModel):
    text: str = Field(..., min_length=1)
    title: Optional[str] = None
    tags: list[str] = []
    publish: bool = True


class AskIn(BaseModel):
    question: str = Field(..., min_length=1)
    save: bool = False
    history: list[dict[str, Any]] = Field(default_factory=list)
    conversation_id: Optional[str] = None


class IngestIn(BaseModel):
    target: str
    publish: bool = True


class SyncIn(BaseModel):
    limit: int = 20
    publish: bool = True


class HarvestIn(BaseModel):
    tool: str = "opencode"
    limit: int = 3


class ActionIn(BaseModel):
    text: str
    owner: str = ""


# ------------------------------------------------------------------ 基础
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "name": cfg().get("hub.name", "OPC"),
        "version": __version__,
        "llm": cfg().get("llm.model"),
        "vault": str(cfg().get("vault.path", "")),
        "stats": db().stats(),
    }


@app.get("/api/status")
def status() -> dict[str, Any]:
    return {"tools": status_all(), "stats": db().stats()}


@app.get("/api/tools")
def tools() -> list[dict[str, Any]]:
    return status_all()


@app.get("/api/sources")
def sources(limit: int = 20) -> list[dict[str, Any]]:
    return source_brief(limit=limit)



@app.post("/api/worker")
def worker(once: bool = True, max_jobs: int = 20) -> dict[str, Any]:
    return {"processed": run_worker(once=once, max_jobs=max_jobs)}


# ------------------------------------------------------------------ 捕获
@app.post("/api/capture")
def api_capture(body: CaptureIn, background: bool = False) -> dict[str, Any]:
    if background:
        from ..util import sha

        title = body.title or body.text.strip().splitlines()[0][:40]
        job_id = db().enqueue("capture", {
            "kind": "note", "external_id": sha(body.text, 16), "title": title,
            "text": body.text, "publish": body.publish,
            "meta": {"tags": body.tags, "via": "console"},
        })
        return {"job_id": job_id, "status": "pending"}
    try:
        return capture_note(body.text, body.title, tags=body.tags, publish=body.publish)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


# ------------------------------------------------------------------ 任务队列
@app.get("/api/jobs")
def list_jobs(limit: int = 20) -> dict[str, Any]:
    return {"items": db().recent_jobs(limit=limit)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int) -> dict[str, Any]:
    job = db().job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@app.post("/api/voice")
async def api_voice(file: UploadFile = File(...)) -> dict[str, Any]:
    """浏览器录音上传 → 排队转写 → 入库（复用 iMeeting 的 FunASR）。"""
    import time

    data = await file.read()
    if not data:
        raise HTTPException(400, "空音频")
    if len(data) > 30 * 1024 * 1024:
        raise HTTPException(413, "音频过大（>30MB）")
    folder = cfg().data_dir / "voice"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "voice.webm").suffix or ".webm"
    path = folder / f"{int(time.time() * 1000)}{suffix}"
    path.write_bytes(data)
    job_id = db().enqueue("voice", {"audio": str(path)})
    return {"job_id": job_id, "status": "pending", "bytes": len(data)}


@app.post("/api/ingest")
async def api_ingest(body: IngestIn) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        if body.target.startswith(("http://", "https://")):
            return get_connector("web").ingest(body.target, publish=body.publish)  # type: ignore[attr-defined]
        return get_connector("files").ingest(body.target, publish=body.publish)  # type: ignore[attr-defined]

    try:
        return await run_in_threadpool(work)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/sync")
async def api_sync(body: SyncIn) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        return {"results": get_connector("files").scan(limit=body.limit, publish=body.publish)}  # type: ignore[attr-defined]

    return await run_in_threadpool(work)


@app.post("/api/harvest")
async def api_harvest(body: HarvestIn) -> dict[str, Any]:
    name = {"pi": "pi_desktop"}.get(body.tool, body.tool)

    def work() -> dict[str, Any]:
        conn = get_connector(name)
        return {"results": conn.harvest(limit=body.limit)}  # type: ignore[attr-defined]

    try:
        return await run_in_threadpool(work)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


# ------------------------------------------------------------------ 会议
@app.get("/api/meetings")
def meetings(limit: int = 20) -> dict[str, Any]:
    try:
        return {"meetings": get_connector("imeeting").list_meetings(limit=limit)}  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"iMeeting 不可达：{exc}") from exc


@app.post("/api/meetings/{meeting_id}/ingest")
async def ingest_meeting(meeting_id: int, publish: bool = True, force: bool = False,
                         background: bool = False) -> dict[str, Any]:
    if background:
        job_id = db().enqueue("ingest_meeting", {"meeting_id": meeting_id, "force": force})
        return {"job_id": job_id, "status": "pending"}

    def work() -> dict[str, Any]:
        return get_connector("imeeting").ingest(meeting_id, publish=publish, force=force)  # type: ignore[attr-defined]

    try:
        return await run_in_threadpool(work)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/meetings/{meeting_id}/actions")
def push_action(meeting_id: int, body: ActionIn) -> dict[str, Any]:
    return get_connector("imeeting").push_action(meeting_id, body.text, body.owner)  # type: ignore[attr-defined]


# ------------------------------------------------------------------ 检索 / 问答
@app.get("/api/search")
def search(q: str, limit: int = 8) -> dict[str, Any]:
    return {"hits": db().search(q, limit=limit)}


def _ensure_conversation(conversation_id: str | None, question: str) -> str:
    database = db()
    if conversation_id:
        conv = database.conversation(conversation_id)
        if conv:
            return conversation_id
    return database.create_conversation(question.strip()[:60] or "新对话")


def _conv_history(conversation_id: str, limit: int = 6) -> list[dict[str, Any]]:
    conv = db().conversation(conversation_id)
    if not conv:
        return []
    msgs = [m for m in conv["messages"] if m["role"] in ("user", "assistant")][-limit:]
    return [{"role": m["role"], "content": m["content"]} for m in msgs]


@app.post("/api/ask")
async def api_ask(body: AskIn) -> dict[str, Any]:
    database = db()
    conv_id = _ensure_conversation(body.conversation_id, body.question)
    history = body.history or _conv_history(conv_id)
    database.add_message(conv_id, "user", body.question)
    try:
        result = await run_in_threadpool(
            lambda: ask_pipeline(body.question, save=body.save, history=history))
    except Exception as exc:  # noqa: BLE001
        database.add_message(conv_id, "assistant", f"出错了：{exc}")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    database.add_message(conv_id, "assistant", result.get("answer", ""), result.get("citations"))
    return {**result, "conversation_id": conv_id}


async def _stream_ask(question: str, save: bool, history: list[dict[str, Any]],
                      conversation_id: str | None = None) -> StreamingResponse:
    database = db()
    conv_id = _ensure_conversation(conversation_id, question)
    if not history:
        history = _conv_history(conv_id)
    database.add_message(conv_id, "user", question)
    prepared = await run_in_threadpool(lambda: prepare_ask(question, history=history))

    async def gen():
        def sse(obj: dict[str, Any]) -> str:
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

        yield sse({"type": "conversation", "id": conv_id})
        if prepared["empty"]:
            text = "知识库里还没有相关内容。先记一条，或导入一场会议。"
            yield sse({"type": "delta", "text": text})
            database.add_message(conv_id, "assistant", text)
            yield sse({"type": "done", "citations": []})
            return
        masked = bool(prepared.get("mask_map"))
        yield sse({"type": "citations", "citations": prepared["citations"],
                   "expansions": prepared.get("expansions", []),
                   "search_query": prepared.get("search_query", question),
                   "sensitive": prepared.get("sensitive", False)})
        buf: list[str] = []
        if masked:
            yield sse({"type": "status", "text": "敏感来源：脱敏生成中…"})
        try:
            async for delta in iterate_in_threadpool(llm().stream(prepared["messages"], max_tokens=1800)):
                buf.append(delta)
                if not masked:
                    yield sse({"type": "delta", "text": delta})
        except Exception as exc:  # noqa: BLE001
            yield sse({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
        answer = "".join(buf)
        if masked and answer:
            from .. import policy

            answer = policy.unmask(answer, prepared["mask_map"])
            yield sse({"type": "delta", "text": answer})
        saved = None
        if save and answer:
            saved = await run_in_threadpool(
                lambda: save_synthetic(question, answer, prepared["citations"]))
        database.add_message(conv_id, "assistant", answer, prepared["citations"])
        yield sse({"type": "done", "citations": prepared["citations"], "saved": saved})

    return StreamingResponse(gen(), media_type="text/event-stream")


# ------------------------------------------------------------------ 会话
@app.get("/api/conversations")
def list_conversations(limit: int = 30) -> dict[str, Any]:
    return {"items": db().list_conversations(limit=limit)}


@app.post("/api/conversations")
def new_conversation(title: str = "新对话") -> dict[str, Any]:
    return {"id": db().create_conversation(title)}


@app.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: str) -> dict[str, Any]:
    conv = db().conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    return conv


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str) -> dict[str, Any]:
    db().delete_conversation(conv_id)
    return {"deleted": conv_id}


@app.get("/api/ask/stream")
async def api_ask_stream(q: str, save: bool = False) -> StreamingResponse:
    return await _stream_ask(q, save, [])


@app.post("/api/ask/stream")
async def api_ask_stream_post(body: AskIn) -> StreamingResponse:
    """POST 版本：带多轮 history + 会话 ID（前端用 fetch + ReadableStream 读 SSE）。"""
    return await _stream_ask(body.question, body.save, body.history, body.conversation_id)


# ------------------------------------------------------------------ 图谱 / 笔记
@app.get("/api/graph")
def graph(limit: int = Query(200, ge=10, le=2000)) -> dict[str, Any]:
    return db().graph(limit)


@app.get("/api/notes/{note_id}")
def note(note_id: int) -> dict[str, Any]:
    with db().connect() as c:
        row = c.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    if not row:
        raise HTTPException(404, "笔记不存在")
    return dict(row)


@app.get("/api/open")
def open_in_obsidian(path: str) -> dict[str, Any]:
    try:
        uri = Vault().root
        return {"uri": f"obsidian://open?vault={uri.name}&file={path}"}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/mindmap/{source_id}")
def make_mindmap(source_id: int) -> dict[str, Any]:
    from ..mindmap import write_canvas

    try:
        return {"path": write_canvas(source_id=source_id)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


# ------------------------------------------------------------------ 值守 / 综述 / 体检 / 行动项
@app.get("/api/watch")
def watch_status() -> dict[str, Any]:
    w = getattr(app.state, "watcher", None)
    if w is None:
        from ..scheduler import watcher as get_watcher

        w = get_watcher()
    return w.status()


@app.post("/api/watch/tick")
async def watch_tick() -> dict[str, Any]:
    w = getattr(app.state, "watcher", None)
    if w is None:
        from ..scheduler import watcher as get_watcher

        w = get_watcher()
    return await run_in_threadpool(w.tick)


@app.post("/api/digest")
async def make_digest(kind: str = "daily", date: Optional[str] = None,
                      force: bool = False) -> dict[str, Any]:
    from ..digest import daily as daily_fn
    from ..digest import weekly as weekly_fn

    fn = weekly_fn if kind.startswith("week") else daily_fn
    try:
        return await run_in_threadpool(lambda: fn(date, force=force))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/lint")
async def run_lint(apply: bool = False) -> dict[str, Any]:
    from ..lint import run as lint_run

    try:
        return await run_in_threadpool(lambda: lint_run(apply_warnings=apply))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@app.get("/api/actions")
def list_actions(limit: int = 200) -> dict[str, Any]:
    from ..actions import collect

    return {"items": collect(limit=limit)}


@app.post("/api/actions/sync")
async def sync_actions() -> dict[str, Any]:
    from ..actions import sync_page

    try:
        return await run_in_threadpool(sync_page)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/actions/push/{source_id}")
async def push_actions(source_id: int) -> dict[str, Any]:
    from ..actions import push_to_imeeting

    try:
        return await run_in_threadpool(lambda: push_to_imeeting(source_id))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/enrich")
async def enrich_pages(limit: int = 8, kind: Optional[str] = None) -> dict[str, Any]:
    from ..enrich import run as enrich_run

    try:
        return await run_in_threadpool(lambda: enrich_run(limit=limit, kind=kind))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/synthesize")
async def synthesize_topic(name: str, kind: Optional[str] = None,
                           limit: int = 8) -> dict[str, Any]:
    from ..synthesize import build as synth_build

    try:
        return await run_in_threadpool(lambda: synth_build(name, kind=kind, limit=limit))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


# ------------------------------------------------------------------ 静态入口
@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/icon-192.png")
def icon192() -> FileResponse:
    return FileResponse(STATIC / "icon-192.png", media_type="image/png")


@app.get("/icon-512.png")
def icon512() -> FileResponse:
    return FileResponse(STATIC / "icon-512.png", media_type="image/png")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
