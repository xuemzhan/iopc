"""共享测试夹具：临时库 + 临时 vault + stub LLM。"""
from __future__ import annotations

import pytest

import opc.config as config_mod
import opc.pipeline as pipeline_mod
from opc.config import Config
from opc.db import Database
from opc.vault import Vault

PAYLOAD = {
    "title": "测试标题",
    "summary": "这是一段用于测试的摘要。",
    "key_points": ["要点一"],
    "claims": [],
    "entities": [{"name": "测试系统", "type": "system", "description": "被测试的系统", "aliases": []}],
    "concepts": [{"name": "测试概念", "definition": "用于验证的概念", "aliases": [], "related_entities": []}],
    "actions": [],
    "tags": ["测试"],
    "open_questions": [],
}


class StubLLM:
    model = "stub-model"

    def __init__(self, payload=None):
        self.payload = payload or PAYLOAD

    def chat_json(self, *args, **kwargs):
        return self.payload

    def chat(self, *args, **kwargs):
        return "结构性回答"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "raw").mkdir()
    (vault / "wiki/_index.md").write_text(
        "# Wiki Index\n\n## 实体\n\n## 概念\n\n## 来源\n", encoding="utf-8"
    )
    cfg = Config({
        "vault": {"path": str(vault), "inbox": "inbox", "wiki": "wiki",
                  "write_raw": True, "auto_publish": True, "protect_existing": True,
                  "min_sources_for_page": 2},
        "ingest": {"index_enabled": True, "max_entities_per_doc": 24,
                   "max_concepts_per_doc": 16},
        "search": {"alias_boost": True, "expand_queries": False},
        "ask": {"rewrite_followups": False},
        "llm": {"max_input_chars": 24000},
        "policy": {"sensitive_enabled": False, "keywords": [], "mask_entities": True},
    })
    monkeypatch.setattr(config_mod, "_cfg", cfg)
    monkeypatch.setattr(pipeline_mod, "llm", lambda *a, **k: StubLLM())
    database = Database(tmp_path / "t.db")
    return cfg, database, vault
