"""PATCH 2 验收：按任务路由、惰性密钥、预算护栏。"""
from __future__ import annotations

import pytest

import opc.config as config_mod
from opc.config import Config
from opc.llm import LLM, LLMError

CFG = Config({
    "llm": {
        "models": {"flash": {"provider": "deepseek", "model": "deepseek-chat"},
                   "pro": {"provider": "deepseek", "model": "deepseek-reasoner"}},
        "providers": {"deepseek": {"base_url": "https://example.invalid/v1",
                                   "api_key_env": "TEST_ROUTE_KEY"}},
        "routes": {"default": "flash", "extract": "flash", "ask": "flash",
                   "verdict": "pro", "enrich": "pro"},
        "budget": {"max_tokens_per_day": 1000, "warn_at_ratio": 0.8},
    },
    "policy": {},
})


@pytest.fixture(autouse=True)
def route_cfg(monkeypatch):
    monkeypatch.setattr(config_mod, "_cfg", CFG)
    return CFG


def test_for_task_routes_to_configured_model():
    assert LLM.for_task("extract").model == "deepseek-chat"
    assert LLM.for_task("verdict").model == "deepseek-reasoner"
    assert LLM.for_task("unknown-task").model == "deepseek-chat"  # 回退 default


def test_llm_shim_accepts_task():
    from opc.llm import llm

    assert llm("ask").task == "ask"
    assert llm().task == "default"


def test_missing_key_is_lazy(monkeypatch):
    monkeypatch.setenv("OPC_BUDGET_FORCE", "1")
    monkeypatch.delenv("TEST_ROUTE_KEY", raising=False)
    inst = LLM.for_task("extract")  # 构造不报错
    with pytest.raises(LLMError) as ei:
        inst.chat([{"role": "user", "content": "hi"}])
    assert "缺少密钥" in str(ei.value)


def test_budget_blocks_batch_but_not_manual(monkeypatch):
    import opc.usage as usage

    monkeypatch.setattr(usage, "today_tokens", lambda: 2000)
    with pytest.raises(LLMError) as ei:
        LLM.for_task("extract")._budget_guard()
    assert "预算" in str(ei.value)
    # 人工单条不拦
    LLM.for_task("ask")._budget_guard()


def test_budget_force_env(monkeypatch):
    import opc.usage as usage

    monkeypatch.setattr(usage, "today_tokens", lambda: 2000)
    monkeypatch.setenv("OPC_BUDGET_FORCE", "1")
    LLM.for_task("extract")._budget_guard()


def test_usage_table_records_task(monkeypatch, tmp_path):
    from opc.db import Database
    from opc.usage import record_llm, today_tokens

    db = Database(tmp_path / "u.db")
    monkeypatch.setattr("opc.db._db", db)
    record_llm(task="extract", provider="deepseek", model="deepseek-chat",
               prompt=100, completion=50, sensitive=True, source_id=7)
    assert today_tokens() == 150
    with db.connect() as c:
        row = c.execute("SELECT task, provider, model, sensitive FROM llm_usage").fetchone()
    assert row["task"] == "extract" and row["provider"] == "deepseek"
    assert row["sensitive"] == 1
