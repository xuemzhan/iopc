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
