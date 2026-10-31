"""OPC 核心逻辑测试：不碰真实网络、不碰真实知识库。"""
from __future__ import annotations

import json

import pytest

import opc.config as config_mod
import opc.pipeline as pipeline_mod
from opc.actions import collect
from opc.capture import capture
from opc.config import Config
from opc.db import Database, _terms
from opc.lint import structural
from opc.pipeline import _merge, valid_name
from opc.util import chunk_text, ensure_unique, normalize_key, slugify
from opc.vault import Vault


class StubLLM:
    model = "stub-model"

    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, *args, **kwargs):
        return self.payload

    def chat(self, *args, **kwargs):
        return "结构性回答"


PAYLOAD = {
    "title": "测试标题",
    "summary": "这是一段用于测试的摘要，说明素材讲了什么。",
    "key_points": ["要点一", "要点二"],
    "claims": [{"text": "结论一", "confidence": "high"}],
    "entities": [
        {"name": "测试系统", "type": "system", "description": "被测试的系统", "aliases": ["TS"]},
        {"name": "README.md", "type": "other", "description": "伪实体", "aliases": []},
    ],
    "concepts": [{"name": "测试概念", "definition": "用于验证的概念", "aliases": [], "related_entities": ["测试系统"]}],
    "actions": [{"text": "做一件事", "owner": "张三"}],
    "tags": ["测试"],
    "open_questions": ["还有问题吗"],
}


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
                  "write_raw": True, "auto_publish": True, "protect_existing": True},
        "ingest": {"index_enabled": True, "max_entities_per_doc": 24, "max_concepts_per_doc": 16},
        "search": {"alias_boost": True},
        "llm": {"max_input_chars": 24000},
    })
    monkeypatch.setattr(config_mod, "_cfg", cfg)
    monkeypatch.setattr(pipeline_mod, "llm", lambda *a, **k: StubLLM(PAYLOAD))
    database = Database(tmp_path / "t.db")
    return cfg, database, vault


def test_slugify_and_unique():
    assert slugify("OPC 中枢") == "OPC-中枢"
    assert slugify("a/b:c*d") == "abcd"
    assert ensure_unique("x", {"x", "x-2"}) == "x-3"
    assert slugify("") == "untitled"


def test_normalize_key():
    assert normalize_key("OPC 中枢") == normalize_key("opc-中枢")
    assert normalize_key("A/B") == "ab"


def test_chunk_text():
    text = "句子。" * 5000
    chunks = chunk_text(text, size=1000, overlap=100)
    assert len(chunks) > 1
    assert all(len(c) <= 1200 for c in chunks)
    assert chunk_text("", size=100) == []


def test_terms_chinese_trigrams():
    terms = _terms("总体能力与总部能力的关系是什么")
    assert all(len(t) >= 2 for t in terms)
    assert any(t == "总体能" for t in terms)
    # 短词保持原样
    assert "会议" in _terms("会议")


def test_valid_name_filters_files_and_paths():
    assert valid_name("示例机构")
    assert valid_name("iMeeting")
    assert not valid_name("README.md")
    assert not valid_name("E:/opc")
    assert not valid_name("C:\\Users\\x")
    assert not valid_name("x")
    assert not valid_name("...")


def test_merge_dedupes_entities():
    part1 = {"summary": "S1", "key_points": ["a"], "entities": [
        {"name": "OPC 中枢", "type": "system", "description": "d1", "aliases": ["OPC"]}],
        "concepts": [], "claims": [], "actions": [], "tags": [], "open_questions": []}
    part2 = {"summary": "S2", "key_points": ["a", "b"], "entities": [
        {"name": "opc 中枢", "type": "system", "description": "更长一点的描述", "aliases": ["中枢"]}],
        "concepts": [{"name": "x" * 80, "definition": "d"}], "claims": [], "actions": [],
        "tags": [], "open_questions": []}
    merged = _merge([part1, part2], "标题")
    assert len(merged["entities"]) == 1
    assert set(merged["entities"][0]["aliases"]) == {"OPC", "中枢"}
    assert merged["key_points"] == ["a", "b"]
    assert merged["concepts"] == []  # 超长名字被 valid_name 过滤


def test_capture_end_to_end(env, monkeypatch):
    cfg, database, vault = env
    res = capture(kind="note", external_id="t-1", title="测试来源",
                  text="这是一段测试素材，讲的是测试系统与测试概念。", database=database)
    assert res["skipped"] is False
    assert res["path"].startswith("wiki/sources/")
    page = vault / res["path"]
    assert page.exists()
    text = page.read_text(encoding="utf-8")
    assert "## 要点" in text and "测试系统" in text
    # 伪实体被过滤
    assert not (vault / "wiki/entities/README.md.md").exists()
    # 索引 / 日志
    index = (vault / "wiki/_index.md").read_text(encoding="utf-8")
    assert "sources/" in index
    # 幂等：同内容再捕获直接跳过
    again = capture(kind="note", external_id="t-1", title="测试来源",
                    text="这是一段测试素材，讲的是测试系统与测试概念。", database=database)
    assert again["skipped"] is True


def test_search_finds_note(env):
    cfg, database, _ = env
    database.put_note(source_id=None, kind="source", title="总体能力建设",
                      slug="总体能力建设", path="wiki/sources/a.md",
                      summary="总部能力与总体能力是两大抓手", body_md="## 要点\n- 总体能力是抓手\n",
                      tags=[], status="published", model="stub")
    hits = database.search("总体能力", limit=5)
    assert hits and hits[0]["title"] == "总体能力建设"
    assert database.search("完全不存在的词", limit=5) == []


def test_actions_collect(env):
    cfg, database, _ = env
    body = "## 要点\n- x\n\n## 行动项\n- [ ] 完成报告 —— @李示例\n- [ ] 组织评审\n"
    database.put_note(source_id=None, kind="source", title="会议纪要", slug="纪要",
                      path="wiki/sources/j.md", summary="s", body_md=body,
                      tags=[], status="published", model="stub")
    items = collect(database)
    assert {i["text"] for i in items} == {"完成报告", "组织评审"}
    assert any(i["owner"] == "李示例" for i in items)


def test_lint_structural(env):
    cfg, database, vault = env
    (vault / "wiki/sources").mkdir(parents=True, exist_ok=True)
    (vault / "wiki/sources/a.md").write_text("---\ntitle: \"A\"\n---\n# A\n\n[[sources/b]] [[sources/nowhere]]\n",
                                             encoding="utf-8")
    (vault / "wiki/sources/b.md").write_text("---\ntitle: \"B\"\n---\n# B\n\n无链接\n", encoding="utf-8")
    res = structural(Vault())
    assert res["pages"] == 2
    assert res["broken"], "应识别断链"
    assert len(res["orphans"]) >= 1


def test_mindmap_builds_canvas(env):
    cfg, database, vault = env
    from opc.mindmap import build_canvas

    capture(kind="note", external_id="m-1", title="导图来源", text="素材内容测试系统", database=database)
    with database.connect() as c:
        sid = c.execute("SELECT id FROM sources ORDER BY id DESC LIMIT 1").fetchone()["id"]
    canvas = build_canvas(database, sid)
    assert canvas["nodes"] and canvas["edges"]
    assert canvas["nodes"][0]["text"].startswith("#")
    json.dumps(canvas)  # 必须可序列化


def test_rrf_prefers_title_match_over_meta_page(env):
    cfg, database, _ = env
    database.put_note(source_id=None, kind="synthetic", title="日报 2026-10-31", slug="daily",
                      path="wiki/synthetic/daily.md",
                      summary="今天讨论了架构与决策，也提到架构决定、决策流程等很多词",
                      body_md="## 要点\n- 架构 决策 架构决定 决策流程\n", tags=[], status="published",
                      model="stub")
    database.put_note(source_id=None, kind="source", title="OPC 中枢架构决定（2026-10-31）",
                      slug="arch", path="wiki/sources/arch.md", summary="架构决定：只做一层",
                      body_md="## 要点\n- 中枢只做一层\n", tags=[], status="published", model="stub")
    hits = database.search("架构 决策", limit=5)
    assert hits[0]["title"].startswith("OPC 中枢架构决定")
    assert hits[0]["channels"]


def test_query_expansion_cache(env, monkeypatch):
    cfg, database, _ = env
    import opc.query_expand as qe

    calls = {"n": 0}

    class Expander(StubLLM):
        def chat_json(self, *a, **k):
            calls["n"] += 1
            return {"terms": ["中枢", "架构"]}

    monkeypatch.setattr(qe, "llm", lambda *a, **k: Expander({}))
    cfg._d.setdefault("search", {})["expand_queries"] = True
    first = qe.expand("这个中枢的架构是什么", database, use_cache=True)
    second = qe.expand("这个中枢的架构是什么", database, use_cache=True)
    assert first == ["中枢", "架构"] == second
    assert calls["n"] == 1, "第二次应命中缓存"


def test_synthesize_topic(env, monkeypatch):
    cfg, database, vault = env
    capture(kind="note", external_id="s-1", title="来源甲", text="测试系统很重要", database=database)
    capture(kind="note", external_id="s-2", title="来源乙", text="测试系统再次出现", database=database)
    import opc.synthesize as syn

    payload = {
        "definition": "测试系统是用于验证的系统。",
        "timeline": [{"date": "2026", "event": "被首次记录"}],
        "perspectives": [{"who": "来源甲", "view": "很重要"}],
        "conflicts": [],
        "gaps": ["还没有验证方法"],
        "related": ["测试概念"],
    }
    monkeypatch.setattr(syn, "llm", lambda *a, **k: StubLLM(payload))
    res = syn.build("测试系统", database=database)
    page = vault / res["path"]
    assert page.exists()
    text = page.read_text(encoding="utf-8")
    assert "## 定义" in text and "## 时间线" in text and "## 来源" in text
    assert res["sources"] >= 1
    # 实体页要有回链
    entity = vault / "wiki/entities/测试系统.md"
    assert "[[synthetic/" in entity.read_text(encoding="utf-8")


def test_followup_rewrite_and_history(env, monkeypatch):
    cfg, database, _ = env
    cfg._d.setdefault("search", {})["expand_queries"] = False
    cfg._d.setdefault("ask", {})["rewrite_followups"] = True

    database.put_note(source_id=None, kind="source", title="OPC 中枢架构决定",
                      slug="arch", path="wiki/sources/arch.md",
                      summary="五条架构决定：单层中枢、唯一落点",
                      body_md="## 关键主张\n- 单层中枢\n- 唯一知识落点\n",
                      tags=[], status="published", model="stub")

    import opc.pipeline as pl

    class Recorder(StubLLM):
        def chat_json(self, *a, **k):
            return {"query": "OPC 中枢 架构决定 第五条"}

    monkeypatch.setattr(pl, "llm", lambda *a, **k: Recorder({}))
    history = [
        {"role": "user", "content": "架构决定是什么"},
        {"role": "assistant", "content": "五条：单层、唯一落点、四入口、幂等、只读"},
    ]
    out = pl.prepare_ask("那第五条呢？", database, history=history)
    assert out["search_query"] == "OPC 中枢 架构决定 第五条"
    msgs = out["messages"]
    assert msgs[1] == {"role": "user", "content": "架构决定是什么"}
    assert msgs[2]["role"] == "assistant"
    assert "知识库片段" in msgs[-1]["content"] and "那第五条呢" in msgs[-1]["content"]
    assert out["citations"] and out["citations"][0]["id"]


def test_rewrite_skipped_without_history(env, monkeypatch):
    cfg, database, _ = env
    import opc.pipeline as pl

    def boom(*a, **k):
        raise AssertionError("无历史时不应调用改写")

    monkeypatch.setattr(pl, "llm", boom)
    assert pl.rewrite_followup("原问题", None, database) == "原问题"


def test_conversations_crud(env):
    cfg, database, _ = env
    cid = database.create_conversation("测试会话")
    database.add_message(cid, "user", "问题")
    database.add_message(cid, "assistant", "回答", [{"n": 1, "title": "出处"}])
    conv = database.conversation(cid)
    assert conv and len(conv["messages"]) == 2
    assert conv["messages"][1]["citations"][0]["title"] == "出处"
    listing = database.list_conversations()
    assert listing[0]["n"] == 2 and listing[0]["title"] == "测试会话"
    database.delete_conversation(cid)
    assert database.conversation(cid) is None


def test_job_progress_and_result(env):
    cfg, database, _ = env
    jid = database.enqueue("capture", {"x": 1})
    database.update_job(jid, progress=40, stage="提炼中")
    job = database.job(jid)
    assert job["progress"] == 40 and job["stage"] == "提炼中"
    database.finish_job(jid, True, result={"path": "a.md"})
    job = database.job(jid)
    assert job["status"] == "done" and job["progress"] == 100
    assert job["result"]["path"] == "a.md"


def test_voice_pcm_conversion(tmp_path):
    import shutil
    import wave

    if not shutil.which("ffmpeg"):
        pytest.skip("没有 ffmpeg")
    from opc.voice import to_pcm

    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    pcm = to_pcm(wav)
    assert len(pcm) == 16000 * 4  # 16kHz 单声道 float32 = 每秒 64000 字节


def test_sensitive_capture_masks_prompt_then_unmasks(env, monkeypatch):
    """送模型的是 ORG_1，写进库/页面的必须是原名。"""
    cfg, database, vault = env
    cfg._d["policy"] = {"sensitive_enabled": True, "keywords": ["示例机构"], "mask_entities": True}
    seen: dict[str, str] = {}

    payload = {
        "title": "ORG_1 的会议",
        "summary": "ORG_1 召开了会议，讨论内部工作。",
        "key_points": ["ORG_1 强调内部"],
        "claims": [],
        "entities": [{"name": "ORG_1", "type": "org", "description": "被脱敏的机构", "aliases": []}],
        "concepts": [],
        "actions": [],
        "tags": [],
        "open_questions": [],
    }

    class Spy(StubLLM):
        def chat_json(self, messages, *a, **k):
            seen["prompt"] = json.dumps(messages, ensure_ascii=False)
            return payload

    monkeypatch.setattr(pipeline_mod, "llm", lambda *a, **k: Spy(payload))
    res = capture(kind="note", external_id="sens-1", title="示例机构会议",
                  text="示例机构召开会议，部署内部工作。", database=database)

    assert "示例机构" not in seen["prompt"], "prompt 里不能出现原名"
    assert "ORG_1" in seen["prompt"]
    page = (vault / res["path"]).read_text(encoding="utf-8")
    assert "示例机构" in page and "ORG_1" not in page
    with database.connect() as c:
        row = c.execute("SELECT meta FROM sources WHERE external_id='sens-1'").fetchone()
    assert '"sensitive":true' in row["meta"]


def test_sentence_pairs_require_same_sentence():
    from opc.cooccur import sentence_pairs

    ents = [{"id": 1, "name": "甲一"}, {"id": 2, "name": "乙二"}, {"id": 3, "name": "丙三"}]
    pairs = sentence_pairs("甲一和乙二在开会。丙三没有参加。", ents)
    assert len(pairs) == 1
    assert (pairs[0]["a"], pairs[0]["b"]) == (1, 2)
    assert "甲一和乙二" in pairs[0]["evidence"]


def test_add_edge_weight_is_source_count(env):
    cfg, database, _ = env
    database.add_edge("entity", 1, "entity", 2, "co_occurs", weight=1.0, source_id=1)
    database.add_edge("entity", 1, "entity", 2, "co_occurs", weight=1.0, source_id=1)
    with database.connect() as c:
        w = c.execute("SELECT weight FROM edges WHERE rel='co_occurs'").fetchone()[0]
    assert w == 1.0, "同一来源重跑必须幂等"
    database.add_edge("entity", 1, "entity", 2, "co_occurs", weight=1.0, source_id=2)
    with database.connect() as c:
        w = c.execute("SELECT weight FROM edges WHERE rel='co_occurs'").fetchone()[0]
    assert w == 2.0, "不同来源应把 weight 加成来源数"


def test_distill_is_idempotent_for_edges(env, monkeypatch):
    """同一来源连跑三次 distill：边数与各边 weight 必须完全不变。"""
    cfg, database, _ = env
    from opc.pipeline import distill_source

    res = capture(kind="note", external_id="idem-1", title="幂等来源",
                  text="测试系统负责测试概念。测试系统也负责另一件事。", database=database)

    def snapshot():
        with database.connect() as c:
            rows = c.execute(
                "SELECT src_type, src_id, dst_type, dst_id, rel, weight FROM edges"
                " ORDER BY src_type, src_id, dst_type, dst_id, rel"
            ).fetchall()
        return [tuple(r) for r in rows]

    first = snapshot()
    for _ in range(2):
        distill_source(res["source_id"], database=database)
    assert snapshot() == first


def test_single_source_entity_gets_no_page(env):
    cfg, database, vault = env
    capture(kind="note", external_id="one-1", title="单源", text="测试系统只出现一次",
            database=database)
    assert not (vault / "wiki/entities/测试系统.md").exists(), "单来源不应建页"
    with database.connect() as c:
        row = c.execute("SELECT src_count, status FROM entities WHERE name='测试系统'").fetchone()
    assert row["src_count"] == 1 and row["status"] != "confirmed"


def test_two_source_entity_gets_page(env):
    cfg, database, vault = env
    capture(kind="note", external_id="two-1", title="双源一", text="测试系统第一次出现",
            database=database)
    capture(kind="note", external_id="two-2", title="双源二", text="测试系统第二次出现",
            database=database)
    assert (vault / "wiki/entities/测试系统.md").exists(), "两个来源应建页"
    with database.connect() as c:
        row = c.execute("SELECT src_count, status FROM entities WHERE name='测试系统'").fetchone()
    assert row["src_count"] == 2 and row["status"] == "confirmed"
