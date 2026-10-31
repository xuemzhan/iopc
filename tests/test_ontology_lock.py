"""P0-C §9（M2.5）locked 不可变基准 + 冲突仲裁。

- A7：人工确认 × 模型 verdict=pass（AND）才锁定；--force 可强锁但审计留痕
- A8：distill 命中锁定条目 → 名称/类型以锁定为准，类型分歧自动生成冲突（幂等）
- A9：仲裁三裁 keep_locked/overturn/unsure —— LLM 永不直接改锁定条目
- A10：prune/enrich 永不触碰 locked 行
"""
from __future__ import annotations

import pytest

import opc.config as config_mod
import opc.ontology as ontology_mod
import opc.pipeline as pipeline_mod
from opc.config import Config
from opc.db import Database, now_iso
from opc.util import normalize_key


class ExtractStub:
    """提炼模型：固定抽出一个 type=system 的实体。"""

    model = "stub-model"

    def chat_json(self, *args, **kwargs):
        return {
            "title": "会议纪要",
            "summary": "摘要。",
            "key_points": [],
            "claims": [],
            "entities": [{"name": "某甲", "type": "system", "description": "系统", "aliases": []}],
            "concepts": [],
            "actions": [],
            "tags": [],
            "open_questions": [],
        }

    def chat(self, *args, **kwargs):
        return "结构性回答"


class VerdictStub:
    """裁决/仲裁模型：返回 fixture 中共享的可变 payload。"""

    model = "stub-verdict"

    def __init__(self, payload: dict):
        self.payload = payload

    def chat_json(self, *args, **kwargs):
        return dict(self.payload)

    def chat(self, *args, **kwargs):
        return "回答"


PASS = {"decision": "pass", "reason": "一致", "confidence": 0.95}
FAIL = {"decision": "fail", "reason": "证据可疑", "confidence": 0.8}


@pytest.fixture()
def oenv(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "wiki" / "entities").mkdir()
    (vault / "raw").mkdir()
    cfg = Config({
        "vault": {"path": str(vault), "inbox": "inbox", "wiki": "wiki",
                  "write_raw": True, "auto_publish": True, "protect_existing": True},
        "ingest": {"index_enabled": True, "max_entities_per_doc": 24},
        "llm": {"max_input_chars": 24000},
        "ontology": {"adjudicate_min_confidence": 0.7},
    })
    vp = dict(PASS)  # verdict 通道的可变输出口，测试按需改写
    monkeypatch.setattr(config_mod, "_cfg", cfg)
    monkeypatch.setattr(pipeline_mod, "llm", lambda *a, **k: ExtractStub())
    monkeypatch.setattr(ontology_mod, "llm", lambda *a, **k: VerdictStub(vp))
    return {"cfg": cfg, "database": Database(tmp_path / "o.db"), "vault": vault,
            "verdict": vp}


def _distill(database, ext_id):
    from opc.capture import capture

    return capture(kind="note", external_id=ext_id, title=f"标题{ext_id}",
                   text=f"来源{ext_id}的正文，提到某甲和相关讨论。",
                   publish=True, database=database)


def test_approve_requires_model_pass(oenv):
    """A7：人工确认 × 模型 pass —— fail 不能锁；pass 才锁。"""
    database = oenv["database"]
    vp = oenv["verdict"]
    oid = database.upsert_entity(normalize_key("某甲"), "某甲", "person")

    vp.update(FAIL)
    res_fail = ontology_mod.approve(database, "entity", oid)
    assert not res_fail["locked"]
    assert database.object_row("entity", oid)["locked"] == 0

    vp.update(PASS)
    res_pass = ontology_mod.approve(database, "entity", oid)
    assert res_pass["locked"] and res_pass["changed"]
    row = database.object_row("entity", oid)
    assert row["locked"] == 1 and "model" in (row["locked_by"] or "")


def test_approve_force_overrides_and_audits(oenv):
    """A7：--force 强锁成功且审计 override。"""
    database = oenv["database"]
    vp = oenv["verdict"]
    oid = database.upsert_entity(normalize_key("某甲"), "某甲", "person")

    vp.update(FAIL)
    res = ontology_mod.approve(database, "entity", oid, force=True)
    assert res["locked"] and res["override"]
    assert database.object_row("entity", oid)["locked"] == 1
    kinds = _event_kinds(database)
    assert "ontology.lock" in kinds


def _event_kinds(database):
    with database.connect() as c:
        return [r["kind"] for r in c.execute("SELECT kind FROM events").fetchall()]


def test_distill_anchors_locked_entity_and_raises_conflict(oenv):
    """A8：distill 命中锁定条目：type 不被覆盖 + 类型分歧自动记冲突 + 边照常挂接。"""
    database = oenv["database"]
    r = _distill(database, "src1")
    with database.connect() as c:
        eid = c.execute("SELECT id FROM entities WHERE key=?",
                        (normalize_key("某甲"),)).fetchone()["id"]
    assert database.object_row("entity", eid)["type"] == "system"
    # 模拟人工修正类型后锁定（锁定态来自 approve 的简化版：直接 set）
    database.set_locked("entity", eid, True, by="human+model")
    with database.tx() as c:
        c.execute("UPDATE entities SET type='person' WHERE id=?", (eid,))
    _distill(database, "src2")  # 提取仍是 type=system → 冲突
    row = database.object_row("entity", eid)
    assert row["type"] == "person", "锁定后 type 不可被新数据覆盖"
    confs = database.open_conflicts()
    assert len(confs) == 1 and confs[0]["field"] == "type"
    assert confs[0]["locked_value"] == "person" and confs[0]["incoming_value"] == "system"
    _distill(database, "src2")  # 同源重跑 → 幂等
    assert len(database.open_conflicts()) == 1


def test_anchor_conflict_idempotent_per_source(oenv):
    database = oenv["database"]
    oid = database.upsert_entity(normalize_key("某甲"), "某甲", "person")
    database.set_locked("entity", oid, True, by="human")
    for _ in range(3):
        database.add_conflict("entity", oid, "type", "person", "system", 7)
    assert len(database.open_conflicts()) == 1


def test_adjudicate_keep_locked(oenv):
    """A9：keep_locked（高置信）→ llm_rejected，锁定条目原样保留。"""
    database = oenv["database"]
    vp = oenv["verdict"]
    oid = database.upsert_entity(normalize_key("某甲"), "某甲", "person")
    database.set_locked("entity", oid, True, by="human")
    cid = database.add_conflict("entity", oid, "type", "person", "system", 1)
    before = database.object_row("entity", oid)["type"]

    vp.update({"decision": "keep_locked", "reason": "抽取噪声", "confidence": 0.9})
    out = ontology_mod.adjudicate(database, cid)
    assert out["status"] == "llm_rejected"
    assert database.object_row("entity", oid)["type"] == before, "仲裁不得改锁定条目"
    assert len(database.open_conflicts()) == 0


def test_adjudicate_overturn_and_unsure_stay_human(oenv):
    """A9：overturn/unsure 都只转人工，锁定条目不被程序改动。"""
    database = oenv["database"]
    vp = oenv["verdict"]
    oid = database.upsert_entity(normalize_key("某甲"), "某甲", "person")
    database.set_locked("entity", oid, True, by="human")

    c1 = database.add_conflict("entity", oid, "type", "person", "system", 1)
    vp.update({"decision": "overturn", "reason": "锁定条目确实错了", "confidence": 0.95})
    assert ontology_mod.adjudicate(database, c1)["status"] == "llm_overturn"
    assert database.object_row("entity", oid)["type"] == "person", "LLM 无权直接改锁定条目"

    c2 = database.add_conflict("entity", oid, "type", "person", "org", 2)
    vp.update({"decision": "unsure", "reason": "证据不够", "confidence": 0.5})
    assert ontology_mod.adjudicate(database, c2)["status"] == "needs_human"
    # 低置信的 overturn（阈值 0.7）也转 needs_human
    c3 = database.add_conflict("entity", oid, "type", "person", "project", 3)
    vp.update({"decision": "overturn", "reason": "不太确定", "confidence": 0.4})
    assert ontology_mod.adjudicate(database, c3)["status"] == "needs_human"
    assert database.object_row("entity", oid)["locked"] == 1
    # 人工收尾：dismiss 清账
    assert ontology_mod.dismiss_conflict(database, c3)["status"] == "dismissed"


def test_unlock(oenv):
    database = oenv["database"]
    oid = database.upsert_entity(normalize_key("某甲"), "某甲", "person")
    ontology_mod.approve(database, "entity", oid)
    assert ontology_mod.unlock(database, "entity", oid)["locked"] is False
    row = database.object_row("entity", oid)
    assert row["locked"] == 0 and not row["locked_at"]


def test_prune_scan_skips_locked(oenv):
    """A10：锁定条目不进伪实体清理。"""
    database = oenv["database"]
    cap = database.upsert_entity("httpsx.com", "https://x.无效域名", "other")
    database.set_locked("entity", cap, True, by="human")
    assert database.object_row("entity", cap)["locked"] == 1
    from opc.prune import scan

    assert scan(database) == [], "locked 行即使名字可疑也不可被 prune 清理"


def test_concept_anchor_locks_definition(oenv):
    """概念通道：锁定概念 definition 不可被新数据覆盖（不建冲突，走 lint）。"""
    database = oenv["database"]
    kid = database.upsert_concept(normalize_key("另乙概念"), "另乙概念", "权威定义")
    database.set_locked("concept", kid, True, by="human")
    from opc.ontology import anchor

    a = anchor(database, "concept", "另乙概念", {"name": "另乙概念", "definition": "新定义"}, 9)
    assert a["definition"] == "权威定义"
    assert a["type"] == ""
    assert database.open_conflicts() == [], "definition 分歧不建冲突（走 lint 矛盾通道）"


def test_conflict_schema_unique(oenv):
    database = oenv["database"]
    oid = database.upsert_entity(normalize_key("某甲"), "某甲", "person")
    c1 = database.add_conflict("entity", oid, "type", "person", "system", 1)
    c1b = database.add_conflict("entity", oid, "type", "person", "system", 1)
    c2 = database.add_conflict("entity", oid, "type", "person", "org", 2)
    assert c1b == c1 and c2 != c1, "同 (kind, ref, field, source) 幂等复用行；不同 source 各记一条"
    assert len(database.open_conflicts()) == 2


def test_now_iso_helper_untouched():
    """守门：now_iso 由 db 模块导出（供本文件/import 使用），不验证值。"""
    assert callable(now_iso)


def test_enrich_one_refuses_locked(oenv, monkeypatch):
    """A10：即使绕过队列直接调用 enrich_one，也不能改锁定行。"""
    from opc import enrich as enrich_mod

    database = oenv["database"]
    eid = database.upsert_entity(normalize_key("锁定实体"), "锁定实体", "system")
    database.set_locked("entity", eid, True, by="human")

    def boom(*a, **k):  # pragma: no cover - 不应被调用
        raise AssertionError("锁定行不应进入补全")

    monkeypatch.setattr(enrich_mod, "llm", boom)
    res = enrich_mod.enrich_one(database, {"kind": "entity", "id": eid,
                                           "name": "锁定实体", "path": ""})
    assert res["skipped"] == "已锁定"
