"""MCP stdio 服务器：让 OpenCode / PI-Desktop / Claude / DSH 都能把 OPC 当工具用。

用法（任一客户端配置里）：
  command: python
  args: ["-m", "opc.mcp_server"]
  env:  { PYTHONPATH: "E:\\opc" }
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any

from .capture import capture_note
from .connectors import get as get_connector
from .connectors import status_all
from .db import db
from .pipeline import ask as ask_pipeline

PROTOCOL = "2024-11-05"
from . import __version__

SERVER = {"name": "opc-hub", "version": __version__}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "opc_capture",
        "description": "把一段内容记进第二大脑：自动提炼摘要/实体/概念/主张，建立知识链接并写入 Obsidian。"
                       "当你和用户讨论出值得长期保留的结论、决策、资料时调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要记的原文（可长）"},
                "title": {"type": "string", "description": "可选标题"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "opc_ask",
        "description": "基于个人知识库回答问题，返回答案与出处。回答任何关于用户过往会议、笔记、资料的问题前先调用它。",
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "opc_search",
        "description": "全文检索知识库，返回命中的笔记与片段（比 opc_ask 更快更便宜）。",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 8}},
            "required": ["query"],
        },
    },
    {
        "name": "opc_ingest_meeting",
        "description": "把本地 iMeeting 的一场会议（转写+纪要+思维导图）提炼进知识库。",
        "inputSchema": {
            "type": "object",
            "properties": {"meeting_id": {"type": "integer"}},
            "required": ["meeting_id"],
        },
    },
    {
        "name": "opc_meetings",
        "description": "列出本地 iMeeting 最近会议。",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 10}}},
    },
    {
        "name": "opc_ingest",
        "description": "导入一个文件路径或网页 URL 到知识库。",
        "inputSchema": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
    },
    {
        "name": "opc_tools",
        "description": "查看已接线的工具栈与在线状态。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "opc_synthesize",
        "description": "把一个实体/概念散落在多份来源里的信息合成一张主题综述页（定义/时间线/不同说法/分歧/未解）。"
                       "当用户问「关于 X 我们到底知道什么」时调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "实体或概念名，如「总体能力」"},
                "kind": {"type": "string", "enum": ["entity", "concept"]},
            },
            "required": ["name"],
        },
    },
    {
        "name": "opc_graph",
        "description": "取知识图谱的节点与边（用于可视化或推理关联）。",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 150}}},
    },
    {
        "name": "opc_ontology_status",
        "description": "查看本体治理状态：类型分布、待审队列数量、开放合并候选（只读）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "opc_ontology_merge",
        "description": "把两个实体归并（A 保留、B 并入）：别名合并、边重指、状态 merged。不可撤销，先确认。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "string", "description": "保留端（id 或名称）"},
                "b": {"type": "string", "description": "被并入端（id 或名称）"},
                "reason": {"type": "string"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "opc_ontology_review",
        "description": "人工审阅实体：confirm（确认）或 reject（退场，保留行但从图/检索隐藏）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ident": {"type": "string", "description": "实体 id 或名称"},
                "action": {"type": "string", "enum": ["confirm", "reject"]},
            },
            "required": ["ident", "action"],
        },
    },
    {
        "name": "opc_note",
        "description": "随手注记（不触发 LLM）：开会/看视频/读论文时记一条，自动挂当前会议时间戳或来源位置。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "kind": {"type": "string", "enum": ["note", "question"]},
                "ctx": {"type": "string", "description": "当时在做的事（可选）"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "opc_notes",
        "description": "列出注记（recent/focused/open/answered/archived）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "default": "recent"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "opc_graph_edit",
        "description": "编辑知识图谱：rename（改名）/ link（连线）/ unlink（断线），写台账可撤销。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["rename", "link", "unlink"]},
                "kind": {"type": "string", "enum": ["entity", "concept"]},
                "node_id": {"type": "integer"},
                "name": {"type": "string"},
                "type": {"type": "string", "description": "rename 时可选的新类型"},
                "rel": {"type": "string", "default": "related_to"},
                "src_type": {"type": "string"},
                "src_id": {"type": "integer"},
                "dst_type": {"type": "string"},
                "dst_id": {"type": "integer"},
                "edge_id": {"type": "integer"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "opc_capabilities",
        "description": "列出 OPC 的能力注册表（REST/MCP/CLI/UI 同源清单），可先看有什么再调用。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _call(name: str, args: dict[str, Any]) -> Any:
    if name == "opc_capture":
        return capture_note(args["text"], args.get("title"))
    if name == "opc_ask":
        return ask_pipeline(args["question"])
    if name == "opc_search":
        return {"hits": db().search(args["query"], limit=int(args.get("limit", 8)))}
    if name == "opc_ingest_meeting":
        return get_connector("imeeting").ingest(int(args["meeting_id"]))  # type: ignore[attr-defined]
    if name == "opc_meetings":
        return {"meetings": get_connector("imeeting").list_meetings(limit=int(args.get("limit", 10)))}  # type: ignore[attr-defined]
    if name == "opc_ingest":
        target = args["target"]
        if target.startswith(("http://", "https://")):
            return get_connector("web").ingest(target)  # type: ignore[attr-defined]
        return get_connector("files").ingest(target)  # type: ignore[attr-defined]
    if name == "opc_tools":
        return {"tools": status_all(), "stats": db().stats()}
    if name == "opc_synthesize":
        from .synthesize import build as synth_build

        return synth_build(args["name"], kind=args.get("kind"))
    if name == "opc_graph":
        return db().graph(int(args.get("limit", 150)))
    if name == "opc_ontology_status":
        from . import ontology

        database = db()
        t = ontology.types(database, "entity")
        with database.connect() as c:
            proposed = c.execute(
                "SELECT COUNT(*) FROM entities WHERE status='proposed'"
                " AND merged_into IS NULL").fetchone()[0]
            open_c = [dict(r) for r in c.execute(
                "SELECT a_id, b_id, score, reason FROM merge_candidates"
                " WHERE status='open' ORDER BY score DESC LIMIT 10").fetchall()]
        return {"types": t, "proposed": int(proposed),
                "candidates_open": len(open_c), "candidates": open_c}
    if name == "opc_ontology_merge":
        from . import ontology

        return ontology.merge(db(), args["a"], args["b"], reason=str(args.get("reason") or ""))
    if name == "opc_ontology_review":
        from . import ontology

        return ontology.review(db(), "entity", args["ident"], action=args["action"])
    if name == "opc_note":
        from .annotate import add_note

        return add_note(db(), args["text"], kind=args.get("kind", "note"),
                        context=args.get("ctx"))
    if name == "opc_notes":
        return {"items": db().list_annotations(scope=args.get("scope", "recent"),
                                               limit=int(args.get("limit", 20)))}
    if name == "opc_graph_edit":
        from . import graph_edit

        action = args["action"]
        if action == "rename":
            return graph_edit.rename_node(db(), args["kind"], int(args["node_id"]),
                                          name=args.get("name"),
                                          type_=args.get("type"))
        if action == "link":
            return graph_edit.create_edge(
                db(), src_type=args["src_type"], src_id=int(args["src_id"]),
                dst_type=args["dst_type"], dst_id=int(args["dst_id"]),
                rel=args.get("rel", "related_to"))
        if action == "unlink":
            return graph_edit.remove_edge(db(), int(args["edge_id"]))
        raise ValueError(f"未知 action：{action}")
    if name == "opc_capabilities":
        from . import capabilities

        return capabilities.summary()
    raise KeyError(f"未知工具 {name}")


def handle(req: dict[str, Any]) -> dict[str, Any] | None:
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER,
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _ok(rid, {})
    if method == "tools/list":
        return _ok(rid, {"tools": TOOLS})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            result = _call(name, args)
            return _ok(rid, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]})
        except Exception as exc:  # noqa: BLE001
            return _ok(rid, {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            })
    if method in ("resources/list", "prompts/list"):
        key = "resources" if method.startswith("resources") else "prompts"
        return _ok(rid, {key: []})
    return _err(rid, -32601, f"Method not found: {method}")


def _ok(rid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def serve_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    print("[opc-mcp] ready", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle(req)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            resp = _err(None, -32700, str(exc))
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def main() -> None:
    serve_stdio()


if __name__ == "__main__":
    main()
