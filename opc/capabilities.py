"""能力注册表（WP-06）：REST / MCP / CLI / UI 的清单同源。

一个能力在这里登记一次，四个入口的「可见性」由本表校验：
- CLI 顶层命令集合 == 注册表 cli 字段集合
- MCP 工具集合 == 注册表 mcp 字段集合
- 注册表声明的 REST 路由必须真实存在

不加新框架：`validate()` 只做集合比较，供 `opc caps --check`、REST `/api/capabilities`、
MCP `opc_capabilities` 与 UI「能力清单」共用。
"""
from __future__ import annotations

from typing import Any

# kind: read / write / ops（运维）
CAPABILITIES: list[dict[str, Any]] = [
    {"id": "capture", "label": "记一条", "kind": "write", "cli": "capture",
     "mcp": ["opc_capture"], "rest": ("POST", "/api/capture"),
     "desc": "幂等捕获 + 提炼 + 写知识页"},
    {"id": "ask", "label": "带出处问答", "kind": "read", "cli": "ask",
     "mcp": ["opc_ask"], "rest": ("POST", "/api/ask"),
     "desc": "检索 + 生成 + 引用"},
    {"id": "search", "label": "检索知识库", "kind": "read", "cli": "search",
     "mcp": ["opc_search"], "rest": ("GET", "/api/search"),
     "desc": "别名 + FTS + LIKE + RRF"},
    {"id": "meeting", "label": "会议入库", "kind": "write", "cli": "meeting",
     "mcp": ["opc_ingest_meeting", "opc_meetings"],
     "rest": ("POST", "/api/meetings/{meeting_id}/ingest"),
     "desc": "iMeeting 转写/纪要/导图/行动项"},
    {"id": "ingest", "label": "导入文件或网页", "kind": "write", "cli": "ingest",
     "mcp": ["opc_ingest"], "rest": ("POST", "/api/ingest"),
     "desc": "URL / 本地文件进流水线"},
    {"id": "sync", "label": "扫描收件箱与剪藏", "kind": "write", "cli": "sync",
     "mcp": [], "rest": ("POST", "/api/sync"),
     "desc": "inbox + Obsidian raw/"},
    {"id": "harvest", "label": "回收 AI 会话", "kind": "write", "cli": "harvest",
     "mcp": [], "rest": ("POST", "/api/harvest"),
     "desc": "OpenCode / PI / WorkBuddy（只读）"},
    {"id": "voice", "label": "语音转写", "kind": "write", "cli": "voice",
     "mcp": [], "rest": ("POST", "/api/voice"),
     "desc": "浏览器录音 → iMeeting FunASR"},
    {"id": "note", "label": "随手注记", "kind": "write", "cli": "note",
     "mcp": ["opc_note"], "rest": ("POST", "/api/annotations"),
     "desc": "秒级落库、自动锚点、不触发 LLM"},
    {"id": "notes", "label": "注记列表", "kind": "read", "cli": "notes",
     "mcp": ["opc_notes"], "rest": ("GET", "/api/annotations"),
     "desc": "recent / focused / open / answered / archived"},
    {"id": "actions", "label": "行动项汇总", "kind": "write", "cli": "actions",
     "mcp": [], "rest": ("GET", "/api/actions"),
     "desc": "保留勾选；可显式回写 iMeeting"},
    {"id": "digest", "label": "日报/周复盘", "kind": "write", "cli": "digest",
     "mcp": [], "rest": ("POST", "/api/digest"),
     "desc": "写入 wiki/synthetic/"},
    {"id": "lint", "label": "知识库体检", "kind": "write", "cli": "lint",
     "mcp": [], "rest": ("POST", "/api/lint"),
     "desc": "孤儿/断链/空壳/重复/矛盾"},
    {"id": "enrich", "label": "空壳页补全", "kind": "write", "cli": "enrich",
     "mcp": [], "rest": ("POST", "/api/enrich"),
     "desc": "只用已关联来源，素材不足少写"},
    {"id": "prune", "label": "伪实体清理", "kind": "write", "cli": "prune",
     "mcp": [], "rest": None, "desc": "预览 + 文件名校验"},
    {"id": "synthesize", "label": "跨来源综述", "kind": "write", "cli": "synthesize",
     "mcp": ["opc_synthesize"], "rest": ("POST", "/api/synthesize"),
     "desc": "定义/时间线/分歧/未解"},
    {"id": "mindmap", "label": "导出思维导图", "kind": "write", "cli": "mindmap",
     "mcp": [], "rest": ("POST", "/api/mindmap/{source_id}"),
     "desc": "Obsidian Canvas"},
    {"id": "graph", "label": "知识图谱", "kind": "read", "cli": "graph",
     "mcp": ["opc_graph"], "rest": ("GET", "/api/graph"),
     "desc": "节点/边/证据"},
    {"id": "graph_edit", "label": "图谱编辑", "kind": "write", "cli": None,
     "mcp": ["opc_graph_edit"], "rest": ("PATCH", "/api/graph/nodes/{kind}/{node_id}"),
     "desc": "改名/连线/断线，写台账可撤销"},
    {"id": "ontology_types", "label": "类型分布", "kind": "read", "cli": "ontology",
     "mcp": ["opc_ontology_status"], "rest": ("GET", "/api/ontology/types"),
     "desc": "枚举外占比 + 状态计数"},
    {"id": "ontology_typefix", "label": "类型校正", "kind": "write", "cli": "ontology",
     "mcp": [], "rest": ("POST", "/api/ontology/typefix"),
     "desc": "规则 + LLM，dry-run 默认"},
    {"id": "ontology_candidates", "label": "归并候选", "kind": "read", "cli": "ontology",
     "mcp": [], "rest": ("GET", "/api/ontology/candidates"),
     "desc": "同名/别名/邻居 Jaccard"},
    {"id": "ontology_merge", "label": "执行归并", "kind": "write", "cli": "ontology",
     "mcp": ["opc_ontology_merge"], "rest": ("POST", "/api/ontology/merge"),
     "desc": "边重指/别名并入/状态 merged"},
    {"id": "ontology_review", "label": "实体审阅", "kind": "write", "cli": "ontology",
     "mcp": ["opc_ontology_review"], "rest": ("POST", "/api/ontology/approve"),
     "desc": "confirm / reject（保留行）"},
    {"id": "ontology_relock", "label": "锁定恢复", "kind": "write", "cli": "ontology",
     "mcp": [], "rest": ("POST", "/api/ontology/relock"),
     "desc": "按页面 frontmatter 恢复 locked"},
    {"id": "sources", "label": "来源列表", "kind": "read", "cli": "sources",
     "mcp": [], "rest": ("GET", "/api/sources"), "desc": "最近来源"},
    {"id": "jobs", "label": "任务队列", "kind": "ops", "cli": "jobs",
     "mcp": [], "rest": ("GET", "/api/jobs"), "desc": "状态/进度/取消/重试"},
    {"id": "worker", "label": "worker 排空", "kind": "ops", "cli": "worker",
     "mcp": [], "rest": ("POST", "/api/worker"), "desc": "手动处理队列"},
    {"id": "watch", "label": "值守状态", "kind": "ops", "cli": "watch",
     "mcp": [], "rest": ("GET", "/api/watch"), "desc": "会议/inbox/日报/体检"},
    {"id": "status", "label": "状态与工具", "kind": "ops", "cli": "status",
     "mcp": ["opc_tools"], "rest": ("GET", "/api/status"), "desc": "深探测连接器"},
    {"id": "doctor", "label": "自检", "kind": "ops", "cli": "doctor",
     "mcp": [], "rest": None, "desc": "配置/密钥/库结构"},
    {"id": "stats", "label": "用量统计", "kind": "ops", "cli": "stats",
     "mcp": [], "rest": None, "desc": "token 用量与库体量"},
    {"id": "version", "label": "版本", "kind": "ops", "cli": "version",
     "mcp": [], "rest": None, "desc": "opc.__version__"},
    {"id": "token", "label": "局域网令牌", "kind": "ops", "cli": "token",
     "mcp": [], "rest": None, "desc": "写入 .env OPC_TOKEN"},
    {"id": "serve", "label": "启动中枢", "kind": "ops", "cli": "serve",
     "mcp": [], "rest": None, "desc": "uvicorn 单进程"},
    {"id": "mcp", "label": "MCP 配置/服务", "kind": "ops", "cli": "mcp",
     "mcp": [], "rest": None, "desc": "打印配置或 --serve"},
    {"id": "rebuild", "label": "删库重建", "kind": "ops", "cli": "rebuild",
     "mcp": [], "rest": None, "desc": "从 wiki/ 重建索引"},
    {"id": "repair", "label": "存量修复", "kind": "ops", "cli": "repair",
     "mcp": [], "rest": None, "desc": "重建共现边/related_to 归一"},
    {"id": "caps", "label": "能力清单", "kind": "ops", "cli": "caps",
     "mcp": ["opc_capabilities"], "rest": ("GET", "/api/capabilities"),
     "desc": "四入口同源注册表 + 一致性校验"},
    {"id": "embed", "label": "语义索引", "kind": "ops", "cli": "embed",
     "mcp": [], "rest": None, "desc": "笔记向量化（敏感来源跳过）"},
    {"id": "related", "label": "相关笔记", "kind": "read", "cli": None,
     "mcp": [], "rest": ("GET", "/api/related"),
     "desc": "共享实体/概念的其它笔记"},
]


def as_list() -> list[dict[str, Any]]:
    out = []
    for c in CAPABILITIES:
        rest = c.get("rest")
        out.append({
            "id": c["id"], "label": c["label"], "kind": c["kind"],
            "desc": c.get("desc", ""),
            "cli": c.get("cli"), "mcp": list(c.get("mcp") or []),
            "rest": ({"method": rest[0], "path": rest[1]} if rest else None),
        })
    return out


def mcp_names() -> set[str]:
    out: set[str] = set()
    for c in CAPABILITIES:
        out.update(c.get("mcp") or [])
    return out


def cli_names() -> set[str]:
    return {c["cli"] for c in CAPABILITIES if c.get("cli")}


def validate() -> list[str]:
    """四个入口与注册表的一致性检查；返回问题清单（空 = 一致）。"""
    problems: list[str] = []
    from .cli import app as cli_app
    from .hub.app import app as hub_app
    from .mcp_server import TOOLS

    # CLI：顶层命令 + 命令组集合必须与注册表一致（双向）
    registered = {c.name or c.callback.__name__ for c in cli_app.registered_commands}
    registered |= {g.name for g in cli_app.registered_groups}
    declared = cli_names()
    if missing := registered - declared:
        problems.append(f"CLI 命令未登记：{sorted(missing)}")
    if extra := declared - registered:
        problems.append(f"注册表声明了不存在的 CLI 命令：{sorted(extra)}")

    # MCP：工具集合必须与注册表一致（双向）
    tools = {t["name"] for t in TOOLS}
    declared_mcp = mcp_names()
    if missing := tools - declared_mcp:
        problems.append(f"MCP 工具未登记：{sorted(missing)}")
    if extra := declared_mcp - tools:
        problems.append(f"注册表声明了不存在的 MCP 工具：{sorted(extra)}")

    # REST：声明的路由必须真实存在
    routes = set()
    for route in hub_app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        for m in methods:
            routes.add((m, path))
    bad = []
    for c in CAPABILITIES:
        rest = c.get("rest")
        if rest and rest not in routes:
            bad.append(f"{c['id']} -> {rest[0]} {rest[1]}")
    if bad:
        problems.append(f"注册表声明的 REST 路由不存在：{bad}")
    return problems


def summary() -> dict[str, Any]:
    items = as_list()
    return {
        "total": len(items),
        "by_kind": {k: sum(1 for c in items if c["kind"] == k)
                    for k in sorted({c["kind"] for c in items})},
        "capabilities": items,
    }
