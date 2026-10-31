"""WP-08：值守时序（_due / maybe_digest / maybe_lint 的日期与时间判断）。"""
from __future__ import annotations

from datetime import datetime

import opc.config as config_mod
import opc.scheduler as scheduler_mod
from opc.config import Config


class FakeDT:
    """固定“现在”为 2026-01-07（周三）10:00。"""

    @classmethod
    def now(cls):
        return datetime(2026, 1, 7, 10, 0, 0)


def _today() -> str:
    return "2026-01-07"


def test_due_respects_time_of_day_and_last_run(env, monkeypatch):
    _, database, _ = env
    monkeypatch.setattr(scheduler_mod, "datetime", FakeDT)
    w = scheduler_mod.Watcher(database=database)

    monkeypatch.setattr(config_mod, "_cfg", Config({
        "watch": {"digest_at": "09:00"}, "vault": {"path": "/tmp/none"}}))
    assert w._due("digest_at", "watch:last_digest") is True
    database.set_meta("watch:last_digest", _today())
    assert w._due("digest_at", "watch:last_digest") is False, "同一天不重复"

    database.set_meta("watch:last_digest", "2026-01-06")
    monkeypatch.setattr(config_mod, "_cfg", Config({
        "watch": {"digest_at": "11:00"}, "vault": {"path": "/tmp/none"}}))
    assert w._due("digest_at", "watch:last_digest") is False, "未到点不跑"


def test_maybe_digest_disabled_and_not_due(env, monkeypatch):
    _, database, _ = env
    monkeypatch.setattr(scheduler_mod, "datetime", FakeDT)
    w = scheduler_mod.Watcher(database=database)

    monkeypatch.setattr(config_mod, "_cfg", Config({
        "digest": {"enabled": False}, "vault": {"path": "/tmp/none"}}))
    assert w.maybe_digest() == {"skipped": "disabled"}

    monkeypatch.setattr(config_mod, "_cfg", Config({
        "watch": {"digest_at": "11:00"}, "vault": {"path": "/tmp/none"}}))
    assert w.maybe_digest().get("skipped") == "not_due"


def test_maybe_lint_only_on_configured_weekday(env, monkeypatch):
    _, database, _ = env
    monkeypatch.setattr(scheduler_mod, "datetime", FakeDT)
    w = scheduler_mod.Watcher(database=database)
    # 2026-01-07 是周三（isoweekday=3）
    monkeypatch.setattr(config_mod, "_cfg", Config({
        "watch": {"lint_weekday": 7, "lint_at": "09:00"},
        "vault": {"path": "/tmp/none"}}))
    assert w.maybe_lint().get("skipped") == "not_today"
