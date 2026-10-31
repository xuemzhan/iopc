"""MCP stdio 服务器：让 OpenCode / PI-Desktop / Claude / DSH 都能把 OPC 当工具用。

用法（任一客户端配置里）：
  command: python
  args: ["-m", "opc.mcp_server"]
  env:  { PYTHONPATH: "<repo>" }
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
SERVER = {"name": "opc-hub", "version": "0.1.0"}

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
