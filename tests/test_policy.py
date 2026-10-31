"""PATCH 1 验收：敏感分级与令牌化。"""
from __future__ import annotations

import pytest

import opc.config as config_mod
import opc.policy as policy
from opc.config import Config


@pytest.fixture(autouse=True)
def policy_cfg(monkeypatch):
    cfg = Config({
        "policy": {
            "sensitive_enabled": True,
            "keywords": ["示例机构", "示例", "内部", "部门"],
            "mask_entities": True,
        },
        "vault": {"path": "/tmp/none"},
    })
    monkeypatch.setattr(config_mod, "_cfg", cfg)
    return cfg


TEXT = "示例机构张示例表示，内部工作要抓紧。由李示例负责验收，部门配合。"


def test_classify_hits_keyword():
    assert policy.classify("今天开了个会", "示例机构会议")
    assert policy.classify("部门的同志参加了", "")
    assert not policy.classify("今天天气不错，适合散步", "随笔")


def test_mask_removes_keywords_and_names():
    masked, mapping = policy.mask(TEXT)
    assert "示例" not in masked
    assert "内部" not in masked
    assert "张示例" not in masked
    assert "李示例" not in masked
    assert mapping, "必须产生映射"


def test_mask_unmask_roundtrip():
    masked, mapping = policy.mask(TEXT)
    assert policy.unmask(masked, mapping) == TEXT


def test_mask_is_stable():
    a, ma = policy.mask(TEXT)
    b, mb = policy.mask(TEXT)
    assert a == b and ma == mb


def test_mask_reuses_token_for_same_text():
    masked, mapping = policy.mask("示例机构开会，示例机构发文。")
    tokens = [t for t in mapping if t.startswith("ORG_")]
    assert len(tokens) == 1, "同一原文必须复用同一令牌"
    assert masked.count(tokens[0]) == 2


def test_unmask_ignores_unknown_tokens():
    assert policy.unmask("ORG_9 不存在", {}) == "ORG_9 不存在"


def test_disabled_policy_passes_through(monkeypatch):
    cfg = Config({"policy": {"sensitive_enabled": False, "keywords": ["示例"]}})
    monkeypatch.setattr(config_mod, "_cfg", cfg)
    assert policy.classify("示例机构") is False
    text, mapping = policy.mask("示例机构")
    assert text == "示例机构" and mapping == {}


class _Recorder:
    """记录每次 LLM 入参，并按需返回改写结果。"""

    def __init__(self, rewrite_to: str = ""):
        self.rewrite_to = rewrite_to
        self.prompts: list[str] = []

    def chat_json(self, messages, *args, **kwargs):
        self.prompts.append("\n".join(str(m.get("content", "")) for m in messages))
        return {"query": self.rewrite_to or "改写成什么"}

    def chat(self, messages, *args, **kwargs):
        self.prompts.append("\n".join(str(m.get("content", "")) for m in messages))
        return "ORG_1 的结论"


def test_ask_masks_question_before_rewrite_and_expand(env, monkeypatch):
    """I8：命中敏感词的追问，必须在送 LLM 之前脱敏；改写结果还原后再检索。"""
    from opc import pipeline as pipeline_mod

    _, database, _ = env
    cfg = Config({
        "policy": {"sensitive_enabled": True, "keywords": ["示例机构", "内部"],
                   "mask_entities": True},
        "ask": {"rewrite_followups": True},
        "vault": {"path": "/tmp/none"},
    })
    monkeypatch.setattr(config_mod, "_cfg", cfg)
    rec = _Recorder(rewrite_to="ORG_1 的结论")
    monkeypatch.setattr(pipeline_mod, "llm", lambda *a, **k: rec)

    prepared = pipeline_mod.prepare_ask(
        "示例机构的内部要求是什么",
        database=database,
        history=[{"role": "user", "content": "示例机构最近的动作"}],
    )
    sent = "\n".join(rec.prompts)
    assert "示例机构" not in sent and "内部" not in sent, "明文不得进入改写/扩展 prompt"
    assert "ORG_1" in sent
    assert prepared["search_query"] == "示例机构 的结论", "检索必须用还原后的原问"
    assert prepared["masked"] is True and prepared["sensitive"] is True


def test_ask_masks_context_with_same_tokens(env, monkeypatch):
    """上下文与问题共用同一映射：同一原文在 prompt 里只有一个令牌。"""
    from opc import pipeline as pipeline_mod
    from opc import retrieval as retrieval_mod

    _, database, _ = env
    cfg = Config({
        "policy": {"sensitive_enabled": True, "keywords": ["示例机构", "内部"],
                   "mask_entities": True},
        "ask": {"rewrite_followups": False},
        "vault": {"path": "/tmp/none"},
    })
    monkeypatch.setattr(config_mod, "_cfg", cfg)
    rec = _Recorder()
    monkeypatch.setattr(pipeline_mod, "llm", lambda *a, **k: rec)
    monkeypatch.setattr(retrieval_mod, "search",
                        lambda *a, **k: [{"type": "note", "id": 999, "title": "T",
                                          "score": 0.0}])
    monkeypatch.setattr(
        pipeline_mod, "build_context",
        lambda hits: ("示例机构的内部材料", [{"n": 1, "type": "note", "note_id": 999,
                                            "path": "wiki/sources/x.md", "title": "T"}]),
    )

    prepared = pipeline_mod.prepare_ask("部门在做什么", database=database)
    user_msg = prepared["messages"][-1]["content"]
    assert "示例机构" not in user_msg and "内部" not in user_msg
    assert "ORG_1" in user_msg
    assert prepared["mask_map"]["ORG_1"] == "示例机构"
    assert prepared["masked"] is True

