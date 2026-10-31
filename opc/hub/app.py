"""OPC 中枢 HTTP 服务：REST + SSE + Web 控制台。

所有重活（LLM、抓取、写盘）都丢线程池，事件循环只做 IO 编排。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
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
from ..llm import attribution as llm_attribution
from ..llm import llm
from ..vault import Vault

STATIC = Path(__file__).parent / "static"


def _build_id() -> str:
    """静态壳内容哈希：用于 Service Worker 更新提示与「当前版本」显示。"""
    h = hashlib.sha1()
    for name in ("index.html", "app.css", "app.js", "sw.js", "logo.svg"):
        p = STATIC / name
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


BUILD = _build_id()

# 深探测（HTTP + 全库扫描）缓存：默认 60 秒，避免 UI 每 30 秒把连接器当探针
_STATUS_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_STATUS_LOCK = threading.Lock()


def _status_payload(refresh: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached = _STATUS_CACHE.get("data")
    if not refresh and cached and now - float(_STATUS_CACHE["at"]) < 60:
        return cached
    with _STATUS_LOCK:
        cached = _STATUS_CACHE.get("data")
        now = time.monotonic()
        if not refresh and cached and now - float(_STATUS_CACHE["at"]) < 60:
            return cached
        data = {"tools": status_all(), "stats": db().stats(), "build": BUILD}
        _STATUS_CACHE["data"] = data
        _STATUS_CACHE["at"] = time.monotonic()
        return data


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "microphone=(self), camera=()",
    # 单文件/原生 JS 没有外部脚本；仍允许内联样式与 blob worker（图谱布局）
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline' blob:; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'"
    ),
}



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

    # 后台预热连接器状态：设置页首次打开不必等 iMeeting / 全库扫描
    def _warm() -> None:
        try:
            _status_payload()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_warm, name="opc-status-warm", daemon=True).start()
    try:
        yield
    finally:
        w.stop()
        worker.stop()


app = FastAPI(title="OPC 中枢", version=__version__, docs_url="/api/docs",
              openapi_url="/api/openapi.json", lifespan=lifespan)

PUBLIC_PATHS = ("/static/", "/manifest.webmanifest", "/sw.js", "/icon-192.png",
                "/icon-512.png", "/favicon.ico", "/favicon-32.png",
                "/apple-touch-icon.png")


def _host_allowed(host: str, token: str) -> bool:
    """Host 头白名单：防 DNS rebinding（攻击者域名解析到 127.0.0.1）。"""
    import ipaddress

    host = (host or "").split(":")[0].strip("[]").lower()
    if host in ("127.0.0.1", "localhost", "::1", "testserver"):
        return True
    if token:
        try:
            return ipaddress.ip_address(host).is_private
        except ValueError:
            return False
    return False


@app.middleware("http")
async def token_guard(request: Request, call_next):
    """Host 白名单 + 令牌校验 + 同源校验；本机请求免令牌，但同样校验 Host/Origin。"""
    import hmac

    def harden(resp):
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    token = os.environ.get("OPC_TOKEN") or cfg().get("security.token", "")
    host_header = request.headers.get("host", "")
    if not _host_allowed(host_header, token):
        return harden(JSONResponse({"detail": "Host 不在允许列表"}, status_code=403))

    # 成本型接口的跨站防护：同源页面不会带 Origin，或带与本机 Host 相同的 Origin。
    origin = request.headers.get("origin", "")
    if origin:
        try:
            netloc = urllib.parse.urlsplit(origin).netloc
        except Exception:
            netloc = ""
        if netloc and netloc != host_header:
            return harden(JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403))
    if request.headers.get("sec-fetch-site") == "cross-site":
        return harden(JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403))

    client = _real_client(request)
    loopback = client in ("127.0.0.1", "::1", "localhost", "testclient")
    if loopback:
        return harden(await call_next(request))

    # 非本机：没有配置令牌就直接拒绝（不再"裸奔"）
    if not token:
        return harden(JSONResponse(
            {"detail": "未配置 OPC_TOKEN：仅允许本机访问。"
                       "局域网访问请先运行 `python -m opc token`"}, status_code=403))

    path = request.url.path
    if path == "/" or path.startswith(PUBLIC_PATHS):
        return harden(await call_next(request))
    supplied = request.headers.get("x-opc-token") or request.query_params.get("token") or ""
    if not hmac.compare_digest(supplied, token):
        return harden(JSONResponse(
            {"detail": "需要访问令牌（?token=… 或 X-OPC-Token）"}, status_code=403))
    return harden(await call_next(request))


def _real_client(request: Request) -> str:
    """真实客户端地址。

    - 未声明信任代理时，任何带 `X-Forwarded-For` 的请求都不当本机（防反代绕过令牌）
    - `[security].trust_proxy = true` 时才采信 XFF 的第一个地址
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        if bool(cfg().get("security.trust_proxy", False)):
            return xff.split(",")[0].strip()
        return ""
    return request.client.host if request.client else ""


# ------------------------------------------------------------------ 模型
class CaptureIn(BaseModel):
    text: str = Field(..., min_length=1)
    title: Optional[str] = None
    tags: list[str] = []
    publish: bool = True


class AnnotationIn(BaseModel):
    text: str = Field(..., min_length=1)
    kind: str = "note"              # note | question
    auto_anchor: bool = True
    anchor_kind: Optional[str] = None
    anchor_ref: Optional[str] = None
    anchor_pos: Optional[str] = None
    anchor_source_id: Optional[int] = None
    anchor_title: Optional[str] = None
    context: Optional[str] = None   # 当时的其他工作（前台窗口标题等）


class ReplyIn(BaseModel):
    text: str = Field(..., min_length=1)


class FocusIn(BaseModel):
    on: bool = True


class AskIn(BaseModel):
    question: str = Field(..., min_length=1)
    save: bool = False
    history: list[dict[str, Any]] = Field(default_factory=list)
    conversation_id: Optional[str] = None
    # 问答作用域（FR-408）：只问某来源 / 某类来源 / 某时间窗
    source_id: Optional[int] = None
    kind: Optional[str] = None
    since: Optional[str] = None
    until: Optional[str] = None


def _scope_of(body: AskIn) -> dict[str, Any]:
    scope = {k: getattr(body, k) for k in ("source_id", "kind", "since", "until")
             if getattr(body, k)}
    return scope


class IngestIn(BaseModel):
    target: str
    publish: bool = True
    background: bool = False


class SyncIn(BaseModel):
    limit: int = 20
    publish: bool = True


class HarvestIn(BaseModel):
    tool: str = "opencode"
    limit: int = 3


class ActionIn(BaseModel):
    text: str
    owner: str = ""


class TelemetryEventIn(BaseModel):
    event: str = Field(..., min_length=2, max_length=64)
    props: dict[str, Any] = Field(default_factory=dict)
    ts: Optional[str] = None


class TelemetryIn(BaseModel):
    session_id: str = ""
    client: str = ""
    events: list[TelemetryEventIn] = Field(default_factory=list)


class ConversationPatch(BaseModel):
    title: str = Field(..., min_length=1, max_length=80)


class FeedbackIn(BaseModel):
    target: str = "answer"
    rating: str = "up"                 # up / down / note
    props: dict[str, Any] = Field(default_factory=dict)


class AnswerSaveIn(BaseModel):
    question: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)
    citations: list[dict[str, Any]] = Field(default_factory=list)


class ActionItemsIn(BaseModel):
    items: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ 基础
@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        vault_name = cfg().vault_path.name
    except Exception:  # noqa: BLE001
        vault_name = ""
    return {
        "ok": True,
        "name": cfg().get("hub.name", "OPC"),
        "version": __version__,
        "build": BUILD,
        "llm": cfg().get("llm.model"),
        "vault": vault_name,
        "stats": db().stats(),
    }


@app.get("/api/pulse")
def pulse() -> dict[str, Any]:
    """轻量心跳：首屏/顶栏用，不探测连接器，目标 < 50ms。"""
    attention = db().attention(limit=3)
    budget: dict[str, Any] = {}
    try:
        from ..usage import today_tokens

        used = int(today_tokens())
        cap = int(cfg().get("llm.budget.max_tokens_per_day", 0) or 0)
        budget = {"used": used, "max": cap,
                  "ratio": round(used / cap, 4) if cap else 0.0,
                  "exhausted": bool(cap and used >= cap)}
    except Exception:  # noqa: BLE001 — 预算读不到不影响心跳
        budget = {}
    return {
        "ok": True,
        "version": __version__,
        "build": BUILD,
        "llm": cfg().get("llm.model"),
        "stats": db().stats(),
        "attention": attention["counts"],
        "attention_total": attention["total"],
        "budget": budget,
        "server_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


@app.get("/api/status")
def status(refresh: bool = False) -> dict[str, Any]:
    return _status_payload(refresh=refresh)


@app.get("/api/tools")
def tools(refresh: bool = False) -> list[dict[str, Any]]:
    return _status_payload(refresh=refresh)["tools"]


@app.get("/api/sources")
def sources(limit: int = 20, q: str = "", kind: str = "") -> list[dict[str, Any]]:
    return source_brief(limit=limit, q=q, kind=kind)


@app.get("/api/sources/{source_id}")
def source_detail(source_id: int) -> dict[str, Any]:
    """来源详情：原文摘要 + 对应知识页；给来源抽屉做预览。"""
    database = db()
    with database.connect() as c:
        row = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise HTTPException(404, "来源不存在")
        note = c.execute(
            "SELECT id, title, slug, path, summary, status FROM notes"
            " WHERE source_id=? AND kind='source' ORDER BY id DESC LIMIT 1", (source_id,)
        ).fetchone()
        doc = c.execute(
            "SELECT char_count, substr(text, 1, 1200) AS excerpt FROM documents WHERE source_id=?",
            (source_id,),
        ).fetchone()
    return {"source": dict(row), "note": dict(note) if note else None,
            "document": dict(doc) if doc else None}



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
        }, priority=10)
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


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int) -> dict[str, Any]:
    result = db().request_cancel(job_id)
    if not result.get("ok"):
        raise HTTPException(404 if result.get("reason") == "not_found" else 409,
                            f"无法取消：{result.get('reason')}")
    return result


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: int) -> dict[str, Any]:
    result = db().retry_job(job_id)
    if not result.get("ok"):
        raise HTTPException(404 if result.get("reason") == "not_found" else 409,
                            f"无法重试：{result.get('reason')}")
    return result


@app.post("/api/answer/save")
async def save_answer(body: AnswerSaveIn) -> dict[str, Any]:
    try:
        saved = await run_in_threadpool(
            lambda: save_synthetic(body.question, body.answer, body.citations))
        return {"saved": saved}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/actions/append")
async def append_actions(body: ActionItemsIn) -> dict[str, Any]:
    from ..actions import append_items

    try:
        return await run_in_threadpool(lambda: append_items(body.items))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc


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
    job_id = db().enqueue("voice", {"audio": str(path)}, priority=5)
    return {"job_id": job_id, "status": "pending", "bytes": len(data)}


@app.post("/api/ingest")
async def api_ingest(body: IngestIn) -> dict[str, Any]:
    target = (body.target or "").strip()
    if not target:
        raise HTTPException(400, "缺少 target")
    is_web = target.startswith(("http://", "https://"))
    if body.background:
        job_id = db().enqueue(
            "ingest_web" if is_web else "ingest_file",
            {"url": target, "publish": body.publish} if is_web
            else {"path": target, "publish": body.publish},
            priority=8,
        )
        return {"job_id": job_id, "status": "pending"}

    def work() -> dict[str, Any]:
        if is_web:
            return get_connector("web").ingest(target, publish=body.publish)  # type: ignore[attr-defined]
        return get_connector("files").ingest(target, publish=body.publish)  # type: ignore[attr-defined]

    try:
        return await run_in_threadpool(work)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


UPLOAD_MAX_BYTES = 8 * 1024 * 1024
# 只允许可读成文本的类型；二进制/图片/音视频走别的入口（FR-204/809）
UPLOAD_EXTS = {".md", ".markdown", ".txt", ".pdf", ".docx", ".pptx", ".xlsx",
               ".html", ".htm", ".json", ".csv", ".log"}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), ingest: bool = True) -> dict[str, Any]:
    """把浏览器拖进来的文件落到 data/uploads，并排队入库。"""
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > UPLOAD_MAX_BYTES:
        raise HTTPException(413, "文件过大（>8MB）")
    raw_name = Path(file.filename or "upload.bin").name
    suffix = Path(raw_name).suffix.lower()
    if suffix not in UPLOAD_EXTS:
        raise HTTPException(
            415, f"不支持的类型 {suffix or '(无后缀)'}；允许：{', '.join(sorted(UPLOAD_EXTS))}")
    folder = cfg().data_dir / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch for ch in raw_name if ch not in '\\/:*?"<>|').strip() or "upload.bin"
    path = folder / f"{int(time.time() * 1000)}-{safe_name}"
    path.write_bytes(data)
    job_id = None
    if ingest:
        job_id = db().enqueue("ingest_file", {"path": str(path), "publish": True}, priority=8)
    return {"path": str(path), "name": safe_name, "bytes": len(data),
            "job_id": job_id, "status": "pending" if job_id else "stored"}


@app.post("/api/sync")
async def api_sync(body: SyncIn) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        out: dict[str, Any] = {
            "results": get_connector("files").scan(  # type: ignore[attr-defined]
                limit=body.limit, publish=body.publish)
        }
        obs = get_connector("obsidian")
        if getattr(obs, "enabled", True):
            try:
                out["raw_results"] = obs.ingest_raw(  # type: ignore[attr-defined]
                    limit=body.limit, publish=body.publish)
            except Exception as exc:  # noqa: BLE001 — raw/ 失败不影响文件扫描
                out["raw_error"] = f"{type(exc).__name__}: {exc}"
        return out

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
        job_id = db().enqueue("ingest_meeting", {"meeting_id": meeting_id, "force": force},
                              priority=5)
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
            lambda: ask_pipeline(body.question, save=body.save, history=history,
                                 scope=_scope_of(body)))
    except Exception as exc:  # noqa: BLE001
        database.add_message(conv_id, "assistant", f"出错了：{exc}")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    database.add_message(conv_id, "assistant", result.get("answer", ""), result.get("citations"))
    return {**result, "conversation_id": conv_id}


async def _stream_ask(question: str, save: bool, history: list[dict[str, Any]],
                      conversation_id: str | None = None,
                      scope: dict[str, Any] | None = None) -> StreamingResponse:
    database = db()
    conv_id = _ensure_conversation(conversation_id, question)
    if not history:
        history = _conv_history(conv_id)
    database.add_message(conv_id, "user", question)
    started = time.perf_counter()

    async def gen():
        def sse(obj: dict[str, Any]) -> str:
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

        yield sse({"type": "conversation", "id": conv_id})

        # 检索准备放到生成器里：先返回响应头，再把阶段推进实时告诉前端。
        stage_q: queue.Queue[str] = queue.Queue()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(
            None, lambda: prepare_ask(question, history=history, on_stage=stage_q.put,
                                      scope=scope))
        last_ping = time.monotonic()
        prepared: dict[str, Any] | None = None
        try:
            while not fut.done():
                while True:
                    try:
                        text = stage_q.get_nowait()
                    except queue.Empty:
                        break
                    yield sse({"type": "status", "text": text})
                if time.monotonic() - last_ping > 12:
                    yield ": ping\n\n"
                    last_ping = time.monotonic()
                await asyncio.sleep(0.05)
            prepared = await fut
            while True:
                try:
                    text = stage_q.get_nowait()
                except queue.Empty:
                    break
                yield sse({"type": "status", "text": text})
        except Exception as exc:  # noqa: BLE001
            msg = f"检索失败：{type(exc).__name__}: {exc}"
            database.add_message(conv_id, "assistant", msg)
            yield sse({"type": "error", "text": msg})
            yield sse({"type": "done", "citations": [], "error": True})
            return

        if prepared["empty"]:
            text = "知识库里还没有相关内容。先记一条，或导入一场会议。"
            yield sse({"type": "status", "text": "没有命中"})
            yield sse({"type": "delta", "text": text})
            database.add_message(conv_id, "assistant", text)
            yield sse({"type": "done", "citations": [], "empty": True,
                       "latency_ms": round((time.perf_counter() - started) * 1000)})
            return

        cites = prepared["citations"]
        masked = bool(prepared.get("mask_map"))
        yield sse({"type": "citations", "citations": cites,
                   "expansions": prepared.get("expansions", []),
                   "search_query": prepared.get("search_query", question),
                   "hits": prepared.get("hits", len(cites)),
                   "sensitive": prepared.get("sensitive", False),
                   "semantic": prepared.get("semantic", False),
                   "vec_hits": prepared.get("vec_hits", 0),
                   "masked": masked})
        buf: list[str] = []
        first_token_ms: int | None = None
        persisted = False
        msg_id: int | None = None
        last_flush = 0.0
        llm_obj = llm("ask")
        if masked:
            yield sse({"type": "status", "text": "敏感来源：脱敏生成中…"})

        def _stream():
            # 生成器在线程池里执行：归因必须在线程内部设置
            with llm_attribution(llm_obj, sensitive=bool(prepared.get("sensitive"))):
                yield from llm_obj.stream(prepared["messages"], max_tokens=1800)

        try:
            async for delta in iterate_in_threadpool(_stream()):
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - started) * 1000)
                buf.append(delta)
                if not masked:
                    # 先把增量落到会话里：客户端中途断开也能看到已生成部分。
                    try:
                        if msg_id is None:
                            msg_id = database.add_message(conv_id, "assistant", "")
                        now = time.monotonic()
                        if now - last_flush > 0.8:
                            database.update_message(msg_id, "".join(buf) + " …", cites)
                            last_flush = now
                    except Exception:  # noqa: BLE001
                        pass
                    yield sse({"type": "delta", "text": delta})
            answer = "".join(buf)
            if masked and answer:
                from .. import policy

                answer = policy.unmask(answer, prepared["mask_map"])
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - started) * 1000)
                yield sse({"type": "delta", "text": answer})
            saved = None
            if save and answer:
                saved = await run_in_threadpool(
                    lambda: save_synthetic(question, answer, cites))
            if msg_id is None:
                database.add_message(conv_id, "assistant", answer, cites)
            else:
                database.update_message(msg_id, answer, cites)
            persisted = True
            usage = dict(getattr(llm_obj, "last_usage", {}) or {})
            yield sse({
                "type": "done", "citations": cites, "saved": saved,
                "semantic": prepared.get("semantic", False),
                "vec_hits": prepared.get("vec_hits", 0),
                "model": getattr(llm_obj, "model", ""),
                "usage": usage,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "first_token_ms": first_token_ms,
                "chars": len(answer),
            })
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            yield sse({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
            yield sse({"type": "done", "citations": cites, "error": True,
                       "latency_ms": round((time.perf_counter() - started) * 1000)})
        finally:
            if not persisted:
                partial = "".join(buf)
                if masked and partial:
                    from .. import policy

                    partial = policy.unmask(partial, prepared.get("mask_map") or {})
                try:
                    if msg_id is not None:
                        database.update_message(
                            msg_id, (partial + "\n\n（生成中断）").strip(), cites)
                    elif partial.strip():
                        database.add_message(conv_id, "assistant",
                                             partial + "\n\n（生成中断）", cites)
                except Exception:  # noqa: BLE001
                    pass

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"})


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


@app.patch("/api/conversations/{conv_id}")
def patch_conversation(conv_id: str, body: ConversationPatch) -> dict[str, Any]:
    if not db().conversation(conv_id):
        raise HTTPException(404, "会话不存在")
    db().rename_conversation(conv_id, body.title)
    return {"id": conv_id, "title": body.title.strip()[:80]}


# 成本型问答只保留 POST（带自定义头），不再提供 GET 版本，避免 <img> 式跨站触发。
@app.post("/api/ask/stream")
async def api_ask_stream_post(body: AskIn) -> StreamingResponse:
    """POST 版本：带多轮 history + 会话 ID（前端用 fetch + ReadableStream 读 SSE）。"""
    return await _stream_ask(body.question, body.save, body.history, body.conversation_id,
                             _scope_of(body))


# ------------------------------------------------------------------ 使用观察 / 反馈
# 只允许枚举型短字符串进库；其余字符串一律降级为长度（不落正文/查询词/文件名/标题）
TELEMETRY_TEXT_KEYS = {
    "action", "kind", "scope", "type", "mode", "group", "intent", "pane",
    "cmd", "status", "edge", "node", "tool", "source", "target", "rating", "top",
}


@app.post("/api/telemetry")
def api_telemetry(body: TelemetryIn) -> dict[str, Any]:
    """批量写入匿名操作事件：只接受事件名与小型属性，单批最多 100 条。"""
    if not bool(cfg().get("policy.telemetry_enabled", True)):
        return {"accepted": 0, "disabled": True}
    events = body.events[:100]
    accepted = 0
    for ev in events:
        name = (ev.event or "").strip()
        if not name or len(name) > 64:
            continue
        props = ev.props if isinstance(ev.props, dict) else {}
        # 属性只保留一层；字符串进白名单才留原文，否则只留长度
        clean: dict[str, Any] = {}
        for k, v in list(props.items())[:12]:
            key = str(k)[:32]
            if isinstance(v, (int, float, bool)) or v is None:
                clean[key] = v
            elif isinstance(v, str):
                if key in TELEMETRY_TEXT_KEYS and len(v) <= 32:
                    clean[key] = v
                else:
                    clean[f"{key}_len"] = len(v)
        db().add_ui_event(body.session_id, name, clean, body.client)
        accepted += 1
    return {"accepted": accepted, "received": len(events)}


@app.post("/api/feedback")
def api_feedback(body: FeedbackIn) -> dict[str, Any]:
    db().add_ui_event("", "feedback",
                      {"target": body.target[:40], "rating": body.rating[:16],
                       **{str(k)[:24]: str(v)[:80] for k, v in list(body.props.items())[:10]}},
                      "feedback")
    return {"ok": True}


@app.get("/api/telemetry/summary")
def telemetry_summary(days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
    return db().ui_event_summary(days=days)


@app.get("/api/attention")
def attention(limit: int = Query(8, ge=1, le=30)) -> dict[str, Any]:
    return db().attention(limit=limit)


# ------------------------------------------------------------------ 注记（P0-F：会议/视频/论文中随手记，带锚点）
@app.get("/api/anchor")
def api_anchor() -> dict[str, Any]:
    """探测当前锚点：录制中会议 + 最近转写段时间戳；失败返回 none。"""
    from ..annotate import current_anchor

    return current_anchor(db())


@app.get("/api/annotations")
def api_annotations(
    scope: str = Query("recent", pattern="^(recent|focused|open|answered|archived)$"),
    anchor: Optional[str] = None,
    limit: int = Query(60, ge=1, le=500),
) -> dict[str, Any]:
    return {"items": db().list_annotations(scope=scope, anchor=anchor, limit=limit)}


@app.post("/api/annotations")
def api_annotation_add(body: AnnotationIn) -> dict[str, Any]:
    from ..annotate import add_note

    try:
        return add_note(
            db(), body.text, kind=body.kind, auto_anchor=body.auto_anchor,
            anchor_kind=body.anchor_kind, anchor_ref=body.anchor_ref,
            anchor_pos=body.anchor_pos, anchor_source_id=body.anchor_source_id,
            anchor_title=body.anchor_title, context=body.context,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/annotations/{aid}/reply")
def api_annotation_reply(aid: int, body: ReplyIn) -> dict[str, Any]:
    from ..annotate import reply

    try:
        return reply(db(), aid, body.text)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/annotations/{aid}/focus")
def api_annotation_focus(aid: int, body: FocusIn) -> dict[str, Any]:
    from ..annotate import focus

    try:
        return focus(db(), aid, body.on)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/annotations/{aid}/archive")
def api_annotation_archive(aid: int) -> dict[str, Any]:
    from ..annotate import archive

    try:
        return archive(db(), aid)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/meetings/{meeting_id}/annotations")
def api_meeting_annotations(meeting_id: int) -> dict[str, Any]:
    """某会议的注记 + 聚合（给 UI/日报用）。"""
    from ..annotate import meeting_stats

    return {
        "items": db().list_annotations(anchor=f"meeting:{meeting_id}", scope="recent", limit=200),
        "stats": meeting_stats(db(), meeting_id),
    }


# ------------------------------------------------------------------ 图谱 / 笔记
# ------------------------------------------------------------------ 数据本体（locked / 冲突）
@app.get("/api/ontology/locked")
def ontology_locked() -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    with db().connect() as c:
        for tbl, kind in (("entities", "entity"), ("concepts", "concept")):
            for r in c.execute(f"SELECT * FROM {tbl} WHERE locked=1").fetchall():
                out.append({"kind": kind, **dict(r)})
    return {"items": out}


@app.get("/api/ontology/conflicts")
def ontology_conflicts(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    return {"items": db().open_conflicts(limit=limit)}


@app.post("/api/ontology/{kind}/{oid}/lock")
async def ontology_lock_route(kind: str, oid: int, force: bool = False) -> dict[str, Any]:
    from .. import ontology

    try:
        return await run_in_threadpool(lambda: ontology.approve(db(), kind, oid, force=force))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/ontology/{kind}/{oid}/unlock")
async def ontology_unlock_route(kind: str, oid: int) -> dict[str, Any]:
    from .. import ontology

    try:
        return await run_in_threadpool(lambda: ontology.unlock(db(), kind, oid))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/ontology/conflicts/{cid}/adjudicate")
async def ontology_adjudicate_route(cid: int) -> dict[str, Any]:
    from .. import ontology

    try:
        return await run_in_threadpool(lambda: ontology.adjudicate(db(), cid))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/ontology/conflicts/{cid}/dismiss")
async def ontology_dismiss_route(cid: int) -> dict[str, Any]:
    from .. import ontology

    try:
        return await run_in_threadpool(lambda: ontology.dismiss_conflict(db(), cid))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc


# ------------------------------------------------------------------ 本体 M2（治理入口）
class OntologyMergeIn(BaseModel):
    a: str
    b: str
    kind: str = "entity"
    reason: str = ""


class OntologyReviewIn(BaseModel):
    kind: str = "entity"
    ident: str
    action: str = "confirm"      # confirm | reject | lock


class TypefixIn(BaseModel):
    apply: bool = False
    use_llm: bool = True
    limit: int | None = None


@app.get("/api/ontology/types")
def ontology_types_route(kind: str = Query("entity", pattern="^(entity|concept)$")) -> dict[str, Any]:
    from .. import ontology

    return ontology.types(db(), kind)


@app.get("/api/ontology/candidates")
def ontology_candidates_route(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    from .. import ontology

    return ontology.candidates(db(), limit=limit)


@app.get("/api/ontology/queue")
def ontology_queue_route(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    """待审队列：proposed 实体（按来源数降序）。"""
    with db().connect() as c:
        items = [dict(r) for r in c.execute(
            "SELECT id, name, type, src_count, page_path FROM entities"
            " WHERE status='proposed' AND merged_into IS NULL"
            " ORDER BY src_count DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()]
    return {"items": items, "total": len(items)}


@app.post("/api/ontology/typefix")
async def ontology_typefix_route(body: TypefixIn) -> dict[str, Any]:
    from .. import ontology

    try:
        return await run_in_threadpool(
            lambda: ontology.typefix(db(), apply=body.apply, use_llm=body.use_llm,
                                     limit=body.limit))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/ontology/merge")
async def ontology_merge_route(body: OntologyMergeIn) -> dict[str, Any]:
    from .. import ontology

    try:
        return await run_in_threadpool(
            lambda: ontology.merge(db(), body.a, body.b, kind=body.kind, reason=body.reason))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/ontology/approve")
async def ontology_approve_route(body: OntologyReviewIn) -> dict[str, Any]:
    from .. import ontology

    try:
        if body.action in ("confirm", "reject"):
            return await run_in_threadpool(
                lambda: ontology.review(db(), body.kind, body.ident, action=body.action))
        return await run_in_threadpool(
            lambda: ontology.approve(db(), body.kind, body.ident))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/ontology/candidates/{cid}/reject")
def ontology_candidate_reject(cid: int) -> dict[str, Any]:
    from ..util import now_iso

    with db().tx() as c:
        cur = c.execute(
            "UPDATE merge_candidates SET status='rejected', updated_at=?"
            " WHERE id=? AND status='open'", (now_iso(), cid))
        changed = cur.rowcount
    if changed == 0:
        raise HTTPException(404, "候选不存在或已处理")
    return {"id": cid, "status": "rejected"}


@app.post("/api/ontology/relock")
async def ontology_relock_route(apply: bool = False) -> dict[str, Any]:
    from .. import ontology

    return await run_in_threadpool(lambda: ontology.relock(db(), apply=apply))


# ------------------------------------------------------------------ 能力注册表（WP-06）
@app.get("/api/capabilities")
def capabilities_route() -> dict[str, Any]:
    from .. import capabilities

    return capabilities.summary()


@app.get("/api/graph")
def graph(limit: int = Query(200, ge=10, le=2000),
          min_sources: Optional[int] = Query(None, ge=1, le=20)) -> dict[str, Any]:
    return db().graph(limit, min_sources=min_sources)


@app.get("/api/related")
def related_notes(note_id: int, limit: int = Query(6, ge=1, le=20)) -> dict[str, Any]:
    """「找相关」：与给定笔记共享实体/概念的其它笔记（按共享数排序）。"""
    with db().connect() as c:
        rows = c.execute(
            "SELECT n.id, n.title, n.path, COUNT(*) AS overlap"
            " FROM edges e1"
            " JOIN edges e2 ON e2.dst_type=e1.dst_type AND e2.dst_id=e1.dst_id"
            "   AND e2.src_type='note' AND e2.src_id<>e1.src_id"
            " JOIN notes n ON n.id=e2.src_id"
            " WHERE e1.src_type='note' AND e1.src_id=?"
            " GROUP BY n.id ORDER BY overlap DESC, n.id DESC LIMIT ?",
            (note_id, limit),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


class NodePatch(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None


class EdgeIn(BaseModel):
    src_type: str
    src_id: int
    dst_type: str
    dst_id: int
    rel: str = "related_to"


@app.patch("/api/graph/nodes/{kind}/{node_id}")
async def patch_node(kind: str, node_id: int, body: NodePatch) -> dict[str, Any]:
    from ..graph_edit import rename_node

    try:
        return await run_in_threadpool(
            lambda: rename_node(db(), kind, node_id, name=body.name, type_=body.type))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/graph/edges/{edge_id}")
async def delete_graph_edge(edge_id: int) -> dict[str, Any]:
    from ..graph_edit import remove_edge

    try:
        return await run_in_threadpool(lambda: remove_edge(db(), edge_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class SemanticToggleIn(BaseModel):
    enabled: bool


class SemanticIndexIn(BaseModel):
    limit: int = 0
    rebuild: bool = False


@app.get("/api/semantic/status")
async def semantic_status() -> dict[str, Any]:
    from ..settings import status as sem_status

    return await run_in_threadpool(sem_status)


@app.post("/api/semantic/toggle")
async def semantic_toggle(body: SemanticToggleIn) -> dict[str, Any]:
    from ..settings import set_semantic

    try:
        return await run_in_threadpool(lambda: set_semantic(body.enabled))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/semantic/index")
def semantic_index(body: SemanticIndexIn) -> dict[str, Any]:
    if not body.limit and not body.rebuild:
        body.limit = 0
    job_id = db().enqueue("embed", {"limit": body.limit, "rebuild": body.rebuild},
                          priority=5)
    return {"job_id": job_id}


@app.get("/api/graph/edits")
def graph_edits(limit: int = 20) -> dict[str, Any]:
    from ..graph_edit import list_edits

    return {"items": list_edits(limit=limit)}


@app.post("/api/graph/undo")
async def graph_undo() -> dict[str, Any]:
    from ..graph_edit import undo_last

    try:
        return await run_in_threadpool(lambda: undo_last(db()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/graph/edges")
async def add_graph_edge(body: EdgeIn) -> dict[str, Any]:
    from ..graph_edit import create_edge

    try:
        return await run_in_threadpool(lambda: create_edge(
            db(), src_type=body.src_type, src_id=body.src_id,
            dst_type=body.dst_type, dst_id=body.dst_id, rel=body.rel))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        v = Vault()
        if path:
            safe = v.safe_path(path)
            if not safe or not safe.exists():
                raise HTTPException(404, "页面不存在或不在库内")
            rel = path.replace("\\", "/").lstrip("/")
        else:
            rel = ""
        uri = (f"obsidian://open?vault={urllib.parse.quote(v.root.name)}"
               f"&file={urllib.parse.quote(rel, safe='/')}")
        return {"uri": uri}
    except HTTPException:
        raise
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


@app.get("/apple-touch-icon.png")
def apple_touch() -> FileResponse:
    return FileResponse(STATIC / "apple-touch-icon.png", media_type="image/png")


@app.get("/favicon-32.png")
def favicon32() -> FileResponse:
    return FileResponse(STATIC / "favicon-32.png", media_type="image/png")


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
