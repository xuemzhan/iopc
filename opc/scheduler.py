"""值守进程：让「捕获 → 提炼 → 建链 → 综述」自己转起来，不依赖人记得点按钮。

三件事：
1. 会议结束自动入库（录制中的不动）
2. inbox 目录自动扫描
3. 到点生成日报 / 周复盘，每周做一次体检
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any, Callable

from .actions import sync_page
from .config import cfg
from .connectors import get as get_connector
from .db import Database, db
from .digest import daily, weekly
from .lint import run as lint_run
from .util import now_iso

FINISHED_STATES = {"finished", "completed", "done", "ended", "stopped", "finalized"}


def _hhmm(value: str) -> tuple[int, int]:
    try:
        h, m = str(value).split(":")[:2]
        return int(h), int(m)
    except Exception:
        return 22, 0


class Watcher:
    def __init__(self, *, database: Database | None = None,
                 log: Callable[[str], None] | None = None):
        self.db = database or db()
        self.log = log or (lambda msg: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict[str, Any] = {"tick_at": None, "results": {}, "error": None}
        self.runs = 0

    # ---------- 配置 ----------
    def _enabled(self, key: str, default: bool = True) -> bool:
        return bool(cfg().get(f"watch.{key}", default))

    @property
    def interval(self) -> int:
        return max(30, int(cfg().get("watch.interval_s", 90)))

    # ---------- 任务 ----------
    def check_meetings(self) -> dict[str, Any]:
        if not self._enabled("meetings"):
            return {"skipped": "disabled"}
        conn = get_connector("imeeting")
        ingested, skipped, errors = [], [], []
        try:
            meetings = conn.list_meetings(limit=20)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

        try:
            state: dict[str, str] = json.loads(self.db.get_meta("watch:meeting_state", "{}") or "{}")
        except Exception:
            state = {}

        for m in meetings:
            status = str(m.get("status", "")).lower()
            if status not in FINISHED_STATES:
                continue
            key = str(m.get("id"))
            with self.db.connect() as c:
                is_voice = c.execute(
                    "SELECT 1 FROM sources WHERE kind='voice' AND external_id=?",
                    (f"voice:{m.get('id')}",),
                ).fetchone()
            if is_voice:
                skipped.append({"id": m.get("id"), "reason": "语音速记已入库"})
                continue
            sig = f"{m.get('segment_count')}|{m.get('finished_at')}"
            if state.get(key) == sig:
                skipped.append({"id": m.get("id"), "reason": "未变化"})
                continue
            try:
                r = conn.ingest(int(m["id"]))  # type: ignore[attr-defined]
                if r.get("skipped"):
                    skipped.append({"id": m["id"], "reason": r.get("reason")})
                else:
                    ingested.append({"id": m["id"], "title": m.get("title"), "path": r.get("path")})
                state[key] = sig
            except Exception as exc:  # noqa: BLE001
                errors.append({"id": m.get("id"), "error": f"{type(exc).__name__}: {exc}"})
        self.db.set_meta("watch:meeting_state", json.dumps(state, ensure_ascii=False))

        if ingested:
            self.log(f"值守：入库 {len(ingested)} 场会议")
            try:
                sync_page(self.db)
            except Exception:
                pass
        return {"ingested": ingested, "skipped": len(skipped), "errors": errors}

    def check_inbox(self) -> dict[str, Any]:
        if not self._enabled("inbox"):
            return {"skipped": "disabled"}
        try:
            results = get_connector("files").scan(limit=8)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        new = [r for r in results if not r.get("skipped") and not r.get("error")]
        if new:
            self.log(f"值守：inbox 新增 {len(new)} 个文档")
            try:
                sync_page(self.db)
            except Exception:
                pass
        return {"scanned": len(results), "new": len(new), "paths": [r.get("path") for r in new][:5]}

    def _due(self, hhmm_key: str, state_key: str) -> bool:
        h, m = _hhmm(cfg().get(f"watch.{hhmm_key}", "22:00"))
        now = datetime.now()
        if (now.hour, now.minute) < (h, m):
            return False
        return self.db.get_meta(state_key) != now.strftime("%Y-%m-%d")

    def maybe_digest(self) -> dict[str, Any]:
        if not cfg().get("digest.enabled", True):
            return {"skipped": "disabled"}
        if not self._due("digest_at", "watch:last_digest"):
            return {"skipped": "not_due"}
        out: dict[str, Any] = {}
        try:
            out["daily"] = daily(database=self.db)
            if datetime.now().isoweekday() == 7 and cfg().get("digest.weekly", True):
                out["weekly"] = weekly(database=self.db)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        if not out.get("daily", {}).get("skipped"):
            self.db.set_meta("watch:last_digest", datetime.now().strftime("%Y-%m-%d"))
            self.log("值守：已生成日报")
        return out

    def maybe_lint(self) -> dict[str, Any]:
        if not cfg().get("lint.enabled", True):
            return {"skipped": "disabled"}
        weekday = int(cfg().get("watch.lint_weekday", 7))
        if datetime.now().isoweekday() != weekday:
            return {"skipped": "not_today"}
        if not self._due("lint_at", "watch:last_lint"):
            return {"skipped": "not_due"}
        try:
            res = lint_run(max_checks=int(cfg().get("lint.max_contradiction_checks", 6)),
                           database=self.db)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        self.db.set_meta("watch:last_lint", datetime.now().strftime("%Y-%m-%d"))
        self.log(f"值守：体检完成（孤儿 {res['orphans']} / 矛盾 {len(res['conflicts'])}）")
        return res

    # ---------- 循环 ----------
    def tick(self) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for name, fn in (("meetings", self.check_meetings), ("inbox", self.check_inbox),
                         ("digest", self.maybe_digest), ("lint", self.maybe_lint)):
            try:
                results[name] = fn()
            except Exception as exc:  # noqa: BLE001
                results[name] = {"error": f"{type(exc).__name__}: {exc}"}
        self.runs += 1
        self.last = {"tick_at": now_iso(), "results": results,
                     "error": next((v.get("error") for v in results.values()
                                    if isinstance(v, dict) and v.get("error")), None)}
        return self.last

    def run_forever(self) -> None:
        self.log("值守已启动")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.last = {"tick_at": now_iso(), "results": {}, "error": str(exc)}
            self._stop.wait(self.interval)
        self.log("值守已停止")

    def start(self) -> "Watcher":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self.run_forever, name="opc-watcher", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(cfg().get("watch.enabled", True)),
            "running": bool(self._thread and self._thread.is_alive()),
            "interval_s": self.interval,
            "runs": self.runs,
            "last": self.last,
            "digest_at": cfg().get("watch.digest_at", "22:00"),
            "lint_weekday": cfg().get("watch.lint_weekday", 7),
            "last_digest": self.db.get_meta("watch:last_digest"),
            "last_lint": self.db.get_meta("watch:last_lint"),
        }


_watcher: Watcher | None = None


def watcher() -> Watcher:
    global _watcher
    if _watcher is None:
        _watcher = Watcher()
    return _watcher


class JobWorker:
    """后台任务执行器：让 capture / ingest 这类 30-60 秒的活能异步跑。"""

    def __init__(self, *, database: Database | None = None,
                 log: Callable[[str], None] | None = None):
        self.db = database or db()
        self.log = log or (lambda msg: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict[str, Any] = {}
        self.runs = 0

    def run_forever(self) -> None:
        from .capture import run_worker

        self.log("任务执行器已启动")
        while not self._stop.is_set():
            try:
                self.db.requeue_stale(30)
                n = run_worker(max_jobs=20, database=self.db)
                self.runs += 1
                self.last = {"at": now_iso(), "processed": n}
            except Exception as exc:  # noqa: BLE001
                self.last = {"at": now_iso(), "error": str(exc)}
                n = 0
            self._stop.wait(0.6 if n else 2.0)
        self.log("任务执行器已停止")

    def start(self) -> "JobWorker":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self.run_forever, name="opc-jobs", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "runs": self.runs,
            "last": self.last,
            **{k: v for k, v in self.db.stats().items() if k.startswith("jobs")},
        }


_worker: JobWorker | None = None


def job_worker() -> JobWorker:
    global _worker
    if _worker is None:
        _worker = JobWorker()
    return _worker
