"""P0-F 注记系统验收（A1–A8）。

- A1：秒级落库（无 LLM 调用），events 留痕
- A2：iMeeting 不可达/未录制 → 安静返回 none
- A3：自动锚点取最近转写段时间戳；可手动覆盖
- A4：reply → 父注记 answered + focused
- A5：focus/archive 可审计
- A6：render_for_meeting 含 timestamp 与回应；会议 ingest 注入正文
- A7：列表过滤（focused/open/answered/anchor）
- A8：本文件即单测，不碰网络/真实库
"""
from __future__ import annotations

import pytest

import opc.annotate as annotate_mod
import opc.config as config_mod
from opc.config import Config
from opc.db import Database


class StubMeetingConn:
    """模拟 iMeeting：录制中会议 + 转写段。"""

    def __init__(self, meetings=None, segments=None, fail_list=False, fail_meeting=False):
        self.meetings = meetings if meetings is not None else [
            {"id": 7, "title": "周会", "status": "recording"}]
        self.segments = segments if segments is not None else [
            {"start_ms": 600000, "end_ms": 3725000, "text": "……"}]
        self.fail_list = fail_list
        self.fail_meeting = fail_meeting

    def list_meetings(self, limit=20, timeout=30.0):
        if self.fail_list:
            raise RuntimeError("connection refused")
        return self.meetings

    def meeting(self, meeting_id, timeout=30.0):
        if self.fail_meeting:
            raise RuntimeError("timeout")
        return {"id": meeting_id, "segments": self.segments}


@pytest.fixture()
def aenv(tmp_path, monkeypatch):
    cfg = Config({
        "vault": {"path": str(tmp_path / "vault"), "inbox": "inbox", "wiki": "wiki",
                  "write_raw": False},
        "data": {"dir": str(tmp_path / "data")},
        "llm": {"max_input_chars": 24000},
    })
    (tmp_path / "vault" / "wiki").mkdir(parents=True)
    monkeypatch.setattr(config_mod, "_cfg", cfg)
    database = Database(cfg.db_path)  # 与 db() 同一文件：会议 ingest 注入走 db()
    return {"cfg": cfg, "database": database}


def _use_connector(monkeypatch, conn):
    monkeypatch.setattr(annotate_mod, "_connector", lambda: conn)


def test_add_note_none_anchor_and_event(aenv, monkeypatch):
    """A1：无锚点也能秒记；不触发提炼；events 留痕。"""
    database = aenv["database"]
    _use_connector(monkeypatch, StubMeetingConn(
        meetings=[{"id": 1, "title": "空闲", "status": "finished"}]))
    row = annotate_mod.add_note(database, "这句话的数据来源是什么？", kind="question")
    assert row["anchor_kind"] == "none" and row["status"] == "open"
    assert row["focused"] == 0 and row["kind"] == "question"
    kinds = _event_kinds(database)
    assert "annotate" in kinds


def _event_kinds(database):
    with database.connect() as c:
        return [r["kind"] for r in c.execute("SELECT kind FROM events").fetchall()]


def test_auto_anchor_recording_meeting(aenv, monkeypatch):
    """A2+A3：录制中会议自动挂锚点，timestamp 取最近段末尾；已入库时可解析 source_id。"""
    database = aenv["database"]
    _use_connector(monkeypatch, StubMeetingConn())
    sid = database.upsert_source("meeting", "7", "周会", content_hash="h")
    row = annotate_mod.add_note(database, "这里口径对吗？", kind="question")
    assert row["anchor_kind"] == "meeting" and row["anchor_ref"] == "7"
    assert row["anchor_pos"] == "01:02:05", "end_ms=3725000 → 01:02:05"
    assert row["anchor_title"] == "周会"
    assert row["anchor_source_id"] == sid


def test_auto_anchor_degrades_on_failure(aenv, monkeypatch):
    """A2：连接器抛错/查询会议失败 → 降级 none，不阻塞输入。"""
    database = aenv["database"]
    _use_connector(monkeypatch, StubMeetingConn(fail_list=True))
    assert annotate_mod.add_note(database, "记一笔")["anchor_kind"] == "none"
    _use_connector(monkeypatch, StubMeetingConn(fail_meeting=True))
    row = annotate_mod.add_note(database, "录到了但取段失败")
    assert row["anchor_kind"] == "meeting" and row["anchor_pos"] in ("", None)


def test_manual_anchor_and_source_title_lookup(aenv, monkeypatch):
    """视频/论文：手动锚点；给 source_id 时自动补标题与 ref。"""
    database = aenv["database"]
    _use_connector(monkeypatch, None)
    sid = database.upsert_source("web", "https://example.com/paper", "论文甲", content_hash="h")
    row = annotate_mod.add_note(database, "第 3 页的假设可疑", anchor_source_id=sid,
                                anchor_pos="p3")
    assert row["anchor_kind"] == "source" and row["anchor_ref"] == str(sid)
    assert "论文甲" in row["anchor_title"] and row["anchor_pos"] == "p3"

    row2 = annotate_mod.add_note(database, "视频第 12 分钟", anchor_kind="meeting",
                                 anchor_ref="9", anchor_pos="12:00", anchor_title="回看")
    assert row2["anchor_kind"] == "meeting" and row2["anchor_pos"] == "12:00"


def test_reply_focuses_parent(aenv, monkeypatch):
    """A4：回应后父注记 answered + focused，子注记继承锚点。"""
    database = aenv["database"]
    _use_connector(monkeypatch, StubMeetingConn(
        meetings=[{"id": 1, "title": "x", "status": "finished"}]))
    parent = annotate_mod.add_note(database, "预算是不是超了？", kind="question",
                                   anchor_kind="meeting", anchor_ref="3", anchor_pos="10:00")
    got = annotate_mod.reply(database, parent["id"], "老师答：没超，含税口径不同")
    assert got["status"] == "answered" and got["focused"] == 1
    child = database.list_annotations(anchor="meeting:3", scope="recent")[0]
    assert child["kind"] == "reply" and child["parent_id"] == parent["id"]
    assert child["anchor_pos"] == "10:00"


def test_focus_and_archive(aenv, monkeypatch):
    """A5：手动标重点/归档，均可审计。"""
    database = aenv["database"]
    _use_connector(monkeypatch, None)
    row = annotate_mod.add_note(database, "重要", anchor_kind="url", anchor_ref="https://x")
    assert annotate_mod.focus(database, row["id"], True)["focused"] == 1
    assert annotate_mod.focus(database, row["id"], False)["focused"] == 0
    assert annotate_mod.archive(database, row["id"])["status"] == "archived"
    kinds = _event_kinds(database)
    assert "annotate.focus" in kinds and "annotate.archive" in kinds


def test_list_filters(aenv, monkeypatch):
    """A7：focused/open/answered/anchor 过滤正确。"""
    database = aenv["database"]
    _use_connector(monkeypatch, None)
    a = annotate_mod.add_note(database, "开会记的", anchor_kind="meeting", anchor_ref="5")
    b = annotate_mod.add_note(database, "阅读记的", anchor_kind="source", anchor_ref="2")
    annotate_mod.reply(database, a["id"], "回应")
    annotate_mod.focus(database, b["id"], True)

    focused = {r["id"] for r in database.list_annotations(scope="focused")}
    assert {a["id"], b["id"]} <= focused, "回应的父注记与手动标重点的注记都在 focused 里"
    answered = [r["id"] for r in database.list_annotations(scope="answered")]
    assert a["id"] in answered
    meeting = [r["id"] for r in database.list_annotations(anchor="meeting:5")]
    assert a["id"] in meeting, "父注记在会议锚点下"
    assert all(r["anchor_kind"] == "meeting" for r in database.list_annotations(anchor="meeting:5"))
    open_ids = {r["id"] for r in database.list_annotations(scope="open")}
    assert b["id"] in open_ids and a["id"] not in open_ids


def test_render_for_meeting_contains_ts_and_replies(aenv, monkeypatch):
    """A6：注记节含 timestamp、⭐、↳ 回应；空注记返回空串。"""
    database = aenv["database"]
    _use_connector(monkeypatch, None)
    assert annotate_mod.render_for_meeting(database, 42) == ""
    a = annotate_mod.add_note(database, "这里矛盾吗？", kind="question",
                              anchor_kind="meeting", anchor_ref="42", anchor_pos="08:15")
    annotate_mod.reply(database, a["id"], "会后确认：不矛盾")
    block = annotate_mod.render_for_meeting(database, 42)
    assert "## 我的注记" in block
    assert "[08:15]" in block and "❓" in block and "⭐" in block
    assert "↳ 会后确认：不矛盾" in block


def test_meeting_ingest_injects_annotations(aenv, monkeypatch):
    """A6：会议 ingest 时注记并入正文（monkeypatch capture 捕获入参）。"""
    database = aenv["database"]
    _use_connector(monkeypatch, None)
    annotate_mod.add_note(database, "纪要里这句有歧义", kind="question",
                          anchor_kind="meeting", anchor_ref="11", anchor_pos="03:20")
    captured: dict = {}

    import opc.connectors.imeeting as imeeting_mod

    def fake_capture(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(imeeting_mod, "capture", fake_capture)
    monkeypatch.setattr(imeeting_mod.IMeetingConnector, "render",
                        lambda self, mid: {"title": "周会", "text": "# 周会\n\n转写正文",
                                           "occurred_at": None, "meta": {}})
    conn = imeeting_mod.IMeetingConnector({"enabled": True})
    conn.ingest(11, publish=False)
    assert "## 我的注记" in captured["text"]
    assert "[03:20]" in captured["text"]
    assert captured["meta"].get("annotations") is True


def test_context_recorded_and_rendered(aenv, monkeypatch):
    """A9：「当时的其他工作」随注记落库，并进会议注记节。"""
    database = aenv["database"]
    _use_connector(monkeypatch, None)
    row = annotate_mod.add_note(
        database, "这段口径和上一版不一致？", kind="question",
        anchor_kind="meeting", anchor_ref="77", anchor_pos="12:34",
        context="Code — annotate.py")
    assert row["meta"]["ctx"] == "Code — annotate.py"
    block = annotate_mod.render_for_meeting(database, 77)
    assert "（同时：Code — annotate.py）" in block
    assert "[12:34]" in block


def test_context_blank_is_ignored(aenv, monkeypatch):
    """A9：没有上下文时不写空 ctx（注记列表里不留噪声）。"""
    database = aenv["database"]
    _use_connector(monkeypatch, None)
    row = annotate_mod.add_note(database, "随手一记", context="   ")
    assert "ctx" not in row["meta"]


def test_context_truncated_and_meta_preserved(aenv, monkeypatch):
    """A9：context 最长 200 字；与调用方自带 meta 共存不互相覆盖。"""
    database = aenv["database"]
    _use_connector(monkeypatch, None)
    row = annotate_mod.add_note(database, "很长", context="x" * 400, meta={"src": "ui"})
    assert row["meta"]["src"] == "ui"
    assert len(row["meta"]["ctx"]) == 200


def test_anchor_skips_disabled_connector(aenv, monkeypatch):
    """连接器没启用时不去探测 iMeeting（避免保存注记被 30s 超时拖住）。"""

    class Disabled:
        enabled = False

        def list_meetings(self, limit=20, timeout=30.0):  # pragma: no cover - 不应被调用
            raise AssertionError("不应探测未启用的连接器")

    monkeypatch.setattr(annotate_mod, "_connector", lambda: Disabled())
    assert annotate_mod.current_anchor(aenv["database"]) == {"anchor_kind": "none"}
