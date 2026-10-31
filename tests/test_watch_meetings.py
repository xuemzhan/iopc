"""值守：会议「结束」状态判定 + 中断会议入库标记。

背景：iMeeting 把连接中断/超时记为 `abandoned`，但已收转写照常落库。
若值守只认 finished，整场会议（哪怕 48 分钟转写）永远进不了知识库，
会中的注记也随之丢失 —— 这里把这条路径钉住。
"""
from __future__ import annotations

import pytest

import opc.config as config_mod
import opc.scheduler as scheduler_mod
from opc.config import Config
from opc.db import Database


class StubConn:
    def __init__(self, meetings, fail=False):
        self.meetings = meetings
        self.fail = fail
        self.ingested: list[int] = []

    def list_meetings(self, limit=20):
        if self.fail:
            raise RuntimeError("connection refused")
        return self.meetings

    def ingest(self, mid):
        self.ingested.append(mid)
        return {"path": f"wiki/meeting-{mid}.md"}


@pytest.fixture()
def wenv(tmp_path, monkeypatch):
    cfg = Config({
        "vault": {"path": str(tmp_path / "vault"), "inbox": "inbox", "wiki": "wiki"},
        "data": {"dir": str(tmp_path / "data")},
        "watch": {"meetings": True},
    })
    (tmp_path / "vault" / "wiki").mkdir(parents=True)
    monkeypatch.setattr(config_mod, "_cfg", cfg)
    return {"db": Database(cfg.db_path), "cfg": cfg}


def _watcher(monkeypatch, conn, db):
    monkeypatch.setattr(scheduler_mod, "get_connector", lambda *a, **k: conn)
    monkeypatch.setattr(scheduler_mod, "sync_page", lambda *a, **k: None)
    return scheduler_mod.Watcher(database=db)


def test_abandoned_meeting_is_ingested(wenv, monkeypatch):
    """中断会议（abandoned）也要入库：内容已落库，丢掉等于白录。"""
    conn = StubConn([{"id": 7, "title": "知识图谱课程", "status": "abandoned",
                      "segment_count": 153, "finished_at": None}])
    r = _watcher(monkeypatch, conn, wenv["db"]).check_meetings()
    assert conn.ingested == [7]
    assert [x["id"] for x in r["ingested"]] == [7]


def test_recording_meeting_is_skipped(wenv, monkeypatch):
    """仍在录制的不入库（注记还在往上挂）。"""
    conn = StubConn([{"id": 8, "title": "进行中", "status": "recording",
                      "segment_count": 12}])
    r = _watcher(monkeypatch, conn, wenv["db"]).check_meetings()
    assert conn.ingested == []
    assert r["ingested"] == []


def test_unchanged_meeting_not_reingested(wenv, monkeypatch):
    """同一场（段数/结束时间未变）只入一次。"""
    conn = StubConn([{"id": 9, "title": "已结束", "status": "finished",
                      "segment_count": 40, "finished_at": "2026-10-31T12:00:00"}])
    w = _watcher(monkeypatch, conn, wenv["db"])
    w.check_meetings()
    w.check_meetings()
    assert conn.ingested == [9]


def test_connector_error_is_isolated(wenv, monkeypatch):
    """iMeeting 不可达只报错，不抛。"""
    r = _watcher(monkeypatch, StubConn([], fail=True), wenv["db"]).check_meetings()
    assert "error" in r and "connection refused" in r["error"]


def test_interrupted_render_marks_warning(monkeypatch):
    """abandoned 会议正文带「异常中断」提示，meta 记 interrupted。"""
    import opc.connectors.imeeting as im

    conn = im.IMeetingConnector({"enabled": True})
    monkeypatch.setattr(conn, "meeting", lambda mid: {
        "id": mid, "title": "课程", "status": "abandoned",
        "segments": [{"start_ms": 0, "end_ms": 1000, "text": "第一句", "speaker": "老师"}],
    })
    monkeypatch.setattr(conn, "minutes", lambda mid: {})
    monkeypatch.setattr(conn, "mindmap", lambda mid: None)
    monkeypatch.setattr(conn, "actions", lambda mid: [])
    r = conn.render(7)
    assert "异常中断" in r["text"]
    assert r["meta"]["interrupted"] is True
    assert r["meta"]["status"] == "abandoned"
