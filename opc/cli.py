"""OPC 命令行：一个入口，调度所有工具。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .capture import capture_note, pending_jobs, run_worker, source_brief
from .config import cfg
from .connectors import get as get_connector
from .connectors import status_all
from .db import db
from .pipeline import ask as ask_pipeline
from .util import today

app = typer.Typer(add_completion=False, help="OPC —— 把所有工具接成一个第二大脑")
ontology_app = typer.Typer(help="数据本体：锁定（不可变基准）/ 审核 / 冲突仲裁")
app.add_typer(ontology_app, name="ontology")
console = Console()


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


@app.callback()
def _main() -> None:
    _utf8()


@app.command()
def version() -> None:
    """显示版本。"""
    console.print(f"OPC v{__version__}")


@app.command()
def status() -> None:
    """看所有工具的接线状态与知识库体量。"""
    table = Table(title="工具栈接线状态", show_lines=False)
    table.add_column("工具", style="cyan", no_wrap=True)
    table.add_column("类型", style="dim")
    table.add_column("状态")
    table.add_column("说明", overflow="fold")
    for s in status_all():
        mark = "[green]●[/]" if s["ok"] else "[red]○[/]"
        table.add_row(s["name"], s["kind"], f"{mark} {'启用' if s['enabled'] else '停用'}", s["detail"])
    console.print(table)

    st = db().stats()
    console.print(
        Panel(
            "\n".join(
                [
                    f"来源 [bold]{st['sources']}[/] · 文档 {st['documents']} · 笔记 {st['notes']}",
                    f"实体 [bold]{st['entities']}[/] · 概念 {st['concepts']} · 关系边 [bold]{st['edges']}[/]",
                    f"待处理任务 {st['jobs_pending']} · 失败 {st['jobs_failed']}",
                    f"知识库：{cfg().get('vault.path')}",
                ]
            ),
            title="中枢体量",
            border_style="cyan",
        )
    )


@app.command()
def capture(
    text: Optional[str] = typer.Argument(None, help="要记的内容；省略则读 stdin"),
    title: Optional[str] = typer.Option(None, "--title", "-t"),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="从文件捕获"),
) -> None:
    """捕获一段内容：自动落库、提炼、建链、写进 Obsidian。"""
    if file:
        from .util import read_text_any

        text = read_text_any(file)
        title = title or file.stem
    if not text:
        text = sys.stdin.read()
    with console.status("[cyan]提炼中（抽取实体/概念/主张 → 建链 → 写入 wiki）…"):
        res = capture_note(text, title)
    _print_result(res)


@app.command()
def note(
    text: str = typer.Argument(..., help="随手记的内容（不触发提炼，秒级落库）"),
    kind: str = typer.Option("note", "--kind", "-k", help="note | question"),
    at: str = typer.Option("", "--at", help="位置：会议/视频 timestamp（12:34）或页码（p12）"),
    meeting: str = typer.Option("", "--meeting", "-m", help="会议 id；auto = 自动探测录制中会议"),
    source_id: Optional[int] = typer.Option(None, "--source", "-s", help="关联已入库来源 id"),
    ctx: str = typer.Option("", "--ctx", help="当时的其他工作，如 'VS Code — pipeline.py'"),
) -> None:
    """开会/看视频/读论文时随手记：带锚点落库，回应后自动成为关注重点。"""
    from . import annotate
    from .db import db

    kw: dict = {"anchor_pos": at or None, "anchor_source_id": source_id}
    if meeting:
        mid = meeting
        if meeting.lower() == "auto":
            a = annotate.current_anchor(db())
            if a.get("anchor_kind") != "meeting":
                console.print("[yellow]没有探测到录制中的会议，按无锚点记录。[/]")
                mid = ""
            else:
                kw["anchor_pos"] = at or a.get("anchor_pos")
        if mid:
            kw.update({"anchor_kind": "meeting", "anchor_ref": mid})
            with db().connect() as c:
                row = c.execute("SELECT title FROM sources WHERE kind='meeting' AND external_id=?",
                                (mid,)).fetchone()
            if row:
                kw["anchor_title"] = row["title"]
    row = annotate.add_note(db(), text, kind=kind, context=ctx or None, **kw)
    where = f"{row['anchor_kind']}:{row['anchor_ref'] or ''}"
    pos = f" @{row['anchor_pos']}" if row.get("anchor_pos") else ""
    console.print(f"[green]已记录[/] #{row['id']} · {where}{pos}")


@app.command()
def notes(
    focused: bool = typer.Option(False, "--focused", "-f", help="只看关注重点"),
    scope: str = typer.Option("recent", "--scope", help="recent | open | answered | archived"),
    anchor: str = typer.Option("", "--anchor", "-a", help="过滤锚点，如 meeting:12"),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """列出注记（默认最近）。"""
    from .db import db

    rows = db().list_annotations(scope="focused" if focused else scope,
                                 anchor=anchor or None, limit=limit)
    if not rows:
        console.print("[dim]没有注记。[/]")
        return
    table = Table(title="注记", show_lines=False)
    table.add_column("ID", style="dim")
    table.add_column("锚点", overflow="fold")
    table.add_column("内容", overflow="fold")
    table.add_column("状态", style="dim")
    for r in rows:
        flag = " ⭐" if r.get("focused") else ""
        anchor_txt = f"{r['anchor_kind']}:{r['anchor_ref'] or ''}"
        if r.get("anchor_pos"):
            anchor_txt += f" @{r['anchor_pos']}"
        tag = "↳ " if r.get("parent_id") else ""
        table.add_row(str(r["id"]), anchor_txt, f"{tag}{r['text']}{flag}", r["status"])
    console.print(table)


@app.command()
def ask(
    question: str = typer.Argument(..., help="要问的问题"),
    save: bool = typer.Option(False, "--save", "-s", help="把答案存成 wiki/synthetic 页"),
    source_id: Optional[int] = typer.Option(None, "--source-id", help="只问某个来源"),
    kind: Optional[str] = typer.Option(None, "--kind", help="只问某类来源（meeting/file/web…）"),
    since: Optional[str] = typer.Option(None, "--since", help="起始日期 YYYY-MM-DD"),
    until: Optional[str] = typer.Option(None, "--until", help="截止日期 YYYY-MM-DD"),
) -> None:
    """基于知识库回答，带出处；可限定来源/类型/时间窗。"""
    scope = {k: v for k, v in
             (("source_id", source_id), ("kind", kind), ("since", since), ("until", until))
             if v}
    with console.status("[cyan]检索 + 生成…"):
        res = ask_pipeline(question, save=save, scope=scope or None)
    console.print(Panel(res["answer"], title=f"问：{question}", border_style="green"))
    if res.get("citations"):
        console.print("[dim]出处：[/]")
        for c in res["citations"]:
            console.print(f"  [{c['n']}] {c['title']}  [dim]{c.get('path', '')}[/]")
    if res.get("saved"):
        console.print(f"[green]已存[/] {res['saved']['path']}")


@app.command("search")
def search_cmd(query: str, limit: int = 8) -> None:
    """全文检索知识库。"""
    hits = db().search(query, limit=limit)
    if not hits:
        console.print("[yellow]没有命中[/]")
        return
    for h in hits:
        console.print(f"[cyan]{h['type']}[/] [bold]{h['title']}[/] [dim]{h.get('path', '')}[/]")
        console.print(f"   {h.get('snippet', '')[:200]}")


@app.command()
def meeting(
    action: str = typer.Argument("list", help="list | ingest <id> | render <id>"),
    meeting_id: Optional[int] = typer.Argument(None),
    limit: int = 20,
) -> None:
    """iMeeting：列会议 / 导入会议（转写+纪要+导图 → 知识库）。"""
    conn = get_connector("imeeting")
    if action == "list":
        rows = conn.list_meetings(limit=limit)
        t = Table(title="本地会议")
        t.add_column("ID", justify="right")
        t.add_column("标题", overflow="fold")
        t.add_column("状态")
        t.add_column("时长")
        t.add_column("片段", justify="right")
        for m in rows:
            t.add_row(
                str(m.get("id")),
                str(m.get("title", ""))[:44],
                str(m.get("status", "")),
                f"{int(m.get('duration_ms') or 0) // 60000}min",
                str(m.get("segment_count", "")),
            )
        console.print(t)
    elif action == "ingest" and meeting_id is not None:
        with console.status("[cyan]拉取转写/纪要/导图 → 提炼 → 写入…"):
            res = conn.ingest(meeting_id)
        _print_result(res)
    elif action == "render" and meeting_id is not None:
        r = conn.render(meeting_id)
        console.print(Panel(r["text"][:3000], title=r["title"]))
    else:
        console.print("用法：opc meeting list | opc meeting ingest <id>")


@app.command()
def ingest(
    target: str = typer.Argument(..., help="文件路径或 URL"),
    publish: bool = typer.Option(True, "--publish/--draft"),
) -> None:
    """导入单个文件 / 网页。"""
    if target.startswith(("http://", "https://")):
        res = get_connector("web").ingest(target, publish=publish)
    else:
        res = get_connector("files").ingest(target, publish=publish)
    _print_result(res)


@app.command()
def sync(
    limit: int = 20,
    publish: bool = typer.Option(True, "--publish/--draft"),
) -> None:
    """扫一遍监听目录 + Obsidian raw/，把新文档吸进中枢。"""
    files = get_connector("files")
    results = files.scan(limit=limit, publish=publish)
    ok = sum(1 for r in results if not r.get("error") and not r.get("skipped"))
    console.print(f"[green]文件：{ok}/{len(results)} 已入库[/]")
    for r in results:
        flag = "[red]×[/]" if r.get("error") else ("[yellow]=[/]" if r.get("skipped") else "[green]✓[/]")
        console.print(f" {flag} {Path(r.get('path', '')).name}  {r.get('title', r.get('error', ''))}")

    obs = get_connector("obsidian")
    if getattr(obs, "enabled", True):
        try:
            raws = obs.ingest_raw(limit=limit, publish=publish)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — 一个入口失败不影响文件扫描
            console.print(f"[yellow]！Obsidian raw/ 扫描失败：{type(exc).__name__}: {exc}[/]")
            return
        ok = sum(1 for r in raws if not r.get("error") and not r.get("skipped"))
        console.print(f"[green]剪藏：{ok}/{len(raws)} 已入库[/]")
        for r in raws:
            flag = "[red]×[/]" if r.get("error") else ("[yellow]=[/]" if r.get("skipped") else "[green]✓[/]")
            console.print(f" {flag} {r.get('title') or r.get('path')}  {r.get('error', '')}")


@app.command()
def harvest(
    tool: str = typer.Argument("opencode", help="opencode | pi | workbuddy"),
    limit: int = 3,
) -> None:
    """把 AI 会话/记忆回收进知识库（只读采集）。"""
    conn = get_connector({"pi": "pi_desktop"}.get(tool, tool))
    with console.status(f"[cyan]采集 {tool} 最近 {limit} 条…"):
        results = conn.harvest(limit=limit)  # type: ignore[attr-defined]
    for r in results:
        if r.get("error"):
            console.print(f"[red]× {r['error']}[/]")
        else:
            console.print(f"[green]✓[/] {r.get('title')} → {r.get('path')}")


@app.command()
def sources(limit: int = 20) -> None:
    """最近入库的来源。"""
    for s in source_brief(limit=limit):
        console.print(f"[cyan]#{s['id']}[/] [{s['kind']}] {s['title']}  [dim]{s.get('note_path') or ''}[/]")


@app.command()
def worker(once: bool = typer.Option(False, "--once"), max_jobs: int = 100) -> None:
    """处理后台任务队列。"""
    n = run_worker(once=once, max_jobs=max_jobs)
    console.print(f"处理了 {n} 个任务")


@app.command()
def jobs() -> None:
    """看任务队列。"""
    for j in pending_jobs():
        console.print(f"#{j['id']} [{j['status']}] {j['kind']} attempts={j['attempts']} {j.get('error') or ''}")


@app.command()
def serve(host: Optional[str] = None, port: Optional[int] = None, reload: bool = False,
          lan: bool = typer.Option(False, "--lan", help="绑定 0.0.0.0，供手机/平板访问（需令牌）")) -> None:
    """启动中枢 HTTP 服务 + Web 控制台。"""
    import os

    import uvicorn

    h = host or ("0.0.0.0" if lan else cfg().get("hub.host", "127.0.0.1"))
    p = port or int(cfg().get("hub.port", 5370))
    console.print(f"[cyan]OPC 中枢 → http://{'127.0.0.1' if h == '0.0.0.0' else h}:{p}[/]")
    if h == "0.0.0.0":
        token = os.environ.get("OPC_TOKEN") or cfg().get("security.token", "")
        if not token:
            console.print("[yellow]！局域网模式下没有令牌，任何同网设备都能访问。先运行 `python -m opc token`[/]")
        else:
            console.print(f"[dim]手机访问：http://{_lan_ip()}:{p}/?token={token[:6]}…[/]")
    uvicorn.run("opc.hub.app:app", host=h, port=p, reload=reload, log_level="info")


def _lan_ip() -> str:
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.command()
def token() -> None:
    """生成局域网访问令牌，写入 .env 的 OPC_TOKEN。"""
    import secrets
    from pathlib import Path

    env = Path(__file__).resolve().parent.parent / ".env"
    new = secrets.token_urlsafe(24)
    lines = []
    if env.exists():
        lines = [ln for ln in env.read_text(encoding="utf-8").splitlines()
                 if not ln.strip().startswith("OPC_TOKEN=")]
    lines.append(f"OPC_TOKEN={new}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]已写入 .env[/] OPC_TOKEN={new}")
    console.print(f"[dim]启动：python -m opc serve --lan → http://{_lan_ip()}:{cfg().get('hub.port', 5370)}/?token={new}[/]")


@app.command()
def mcp(serve: bool = typer.Option(False, "--serve", help="以 stdio 服务器运行（客户端会自己拉起）")) -> None:
    """打印 MCP 接入配置；`--serve` 时以 stdio 服务器运行。"""
    from .mcp_server import TOOLS, serve_stdio

    if serve:
        serve_stdio()
        return
    root = str(Path(__file__).resolve().parent.parent)
    opencode = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"opc": {"type": "local", "command": ["python", "-m", "opc.mcp_server"],
                        "enabled": True, "environment": {"PYTHONPATH": root}}},
    }
    claude = {"mcpServers": {"opc": {"command": "python", "args": ["-m", "opc.mcp_server"],
                                     "env": {"PYTHONPATH": root}}}}
    console.print(f"[cyan]MCP 工具 {len(TOOLS)} 个：[/]" +
                  " ".join(t["name"] for t in TOOLS))
    console.print("[dim]OpenCode（~/.config/opencode/opencode.jsonc）[/]")
    console.print_json(json.dumps(opencode, ensure_ascii=False))
    console.print("[dim]Claude Desktop（claude_desktop_config.json）[/]")
    console.print_json(json.dumps(claude, ensure_ascii=False))
    console.print("[dim]也可以直接运行：python -m opc.mcp_server（或 opc mcp --serve）[/]")


@app.command()
def graph(limit: int = 200) -> None:
    """输出图谱 JSON（可喂给前端/可视化）。"""
    console.print_json(json.dumps(db().graph(limit), ensure_ascii=False))


@app.command()
def mindmap(source_id: int = typer.Argument(1, help="来源 ID（opc sources 可查）")) -> None:
    """把一条知识页导出成 Obsidian Canvas 思维导图。"""
    from .mindmap import write_canvas

    path = write_canvas(source_id=source_id)
    console.print(f"[green]已生成[/] {path}")
    console.print("[dim]在 Obsidian 里双击即可打开，节点可拖动、可继续加子节点。[/]")


@app.command()
def watch(once: bool = typer.Option(False, "--once", help="只跑一轮就退出")) -> None:
    """值守：会议结束自动入库、inbox 自动扫、到点出日报、每周体检。"""
    from .scheduler import Watcher

    w = Watcher(log=lambda m: console.print(f"[dim]{m}[/]"))
    if once:
        console.print_json(json.dumps(w.tick(), ensure_ascii=False))
        return
    console.print("[cyan]值守中… Ctrl+C 退出[/]")
    try:
        w.run_forever()
    except KeyboardInterrupt:
        w.stop()
        console.print("已停止")


@app.command()
def digest(
    kind: str = typer.Argument("daily", help="daily | weekly"),
    date: Optional[str] = typer.Option(None, "--date", "-d", help="YYYY-MM-DD"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """生成日报 / 周复盘（写进 wiki/synthetic/）。"""
    from .digest import daily as daily_fn
    from .digest import weekly as weekly_fn

    with console.status("[cyan]汇总中…"):
        res = weekly_fn(date, force=force) if kind.startswith("week") else daily_fn(date, force=force)
    if res.get("skipped"):
        console.print(f"[yellow]跳过：{res.get('reason')}[/]")
        return
    console.print(f"[green]已生成[/] {res['path']}")
    if res.get("headline"):
        console.print(Panel(res["headline"], title="主线", border_style="green"))


@app.command()
def lint(apply: bool = typer.Option(False, "--apply", help="把冲突警告直接写进页面")) -> None:
    """知识库体检：孤儿页 / 断链 / 空壳 / 重复 / 矛盾。"""
    from .lint import run as lint_run

    with console.status("[cyan]体检中（含矛盾抽查）…"):
        res = lint_run(apply_warnings=apply)
    table = Table(title=f"体检结果 · {res['pages']} 页")
    table.add_column("项")
    table.add_column("数量", justify="right")
    for k, label in (("orphans", "孤儿页"), ("broken", "断链页"), ("stubs", "空壳页"),
                     ("duplicates", "疑似重复"), ("conflicts", "矛盾")):
        v = res[k] if k != "conflicts" else len(res["conflicts"])
        table.add_row(label, str(v))
    console.print(table)
    console.print(f"[green]报告[/] {res['path']}")
    for c in res.get("conflicts", [])[:5]:
        console.print(f"[yellow]![/] {c['a_title']} ↔ {c['b_title']}：{c['reason']}")


@app.command()
def actions(
    push: Optional[int] = typer.Option(None, "--push", help="把该 source 的行动项回写 iMeeting"),
) -> None:
    """汇总所有知识页的行动项到 Obsidian，保持勾选状态。"""
    from .actions import push_to_imeeting, sync_page

    if push:
        res = push_to_imeeting(push)
        console.print_json(json.dumps(res, ensure_ascii=False))
    res = sync_page()
    console.print(f"[green]已同步[/] {res['path']}：待办 {res['todo']} / 完成 {res['done']}")


@app.command()
def enrich(
    limit: int = typer.Option(8, "--limit", "-n"),
    kind: Optional[str] = typer.Option(None, "--kind", help="entity | concept"),
) -> None:
    """补全空壳页：用已关联来源填 Key Facts（不编造，素材不足就少写）。"""
    from .enrich import run as enrich_run

    with console.status("[cyan]补全中…"):
        res = enrich_run(limit=limit, kind=kind)
    for d in res["enriched"]:
        console.print(f"[green]✓[/] {d['name']}  {len(d['facts'])} 条事实")
    for s in res["skipped"][:5]:
        console.print(f"[yellow]·[/] {s.get('name')}：{s.get('skipped') or s.get('error')}")
    console.print(f"[dim]剩余空壳页 {res['remaining']}[/]")


@app.command()
def prune(apply: bool = typer.Option(False, "--apply", help="真的删（默认只预览）")) -> None:
    """清理伪实体：被抽成实体的文件名/路径/URL。"""
    from .prune import prune as prune_run

    res = prune_run(apply=apply)
    for it in res["items"][:20]:
        console.print(f"- [{it['kind']}] {it['name']}  {it.get('path', '')}")
    console.print(f"[{'green' if apply else 'yellow'}]{'已删除' if apply else '预览'} {res['count']} 个[/]"
                  + ("" if apply else "，加 --apply 执行"))


@app.command()
def synthesize(
    name: str = typer.Argument(..., help="实体或概念名，如「总体能力」"),
    kind: Optional[str] = typer.Option(None, "--kind", help="entity | concept"),
    limit: int = typer.Option(8, "--limit", "-n"),
) -> None:
    """跨来源综述：把一个主题的多份材料合成一页（定义/时间线/分歧/未解）。"""
    from .synthesize import build as synth_build

    with console.status("[cyan]跨来源综述中…"):
        res = synth_build(name, kind=kind, limit=limit)
    console.print(f"[green]已生成[/] {res['path']}（{res['sources']} 份来源）")
    console.print(Panel(res.get("definition", ""), title=f"主题：{res['subject']}", border_style="cyan"))
    console.print(f"[dim]时间线 {res['timeline']} 条 · 分歧 {res['conflicts']} 处[/]")


@app.command()
def embed(
    limit: int = typer.Option(0, "--limit", help="最多处理多少条笔记（0=全部）"),
    rebuild: bool = typer.Option(False, "--rebuild", help="全部重嵌（换模型时用）"),
) -> None:
    """给笔记建语义向量（云端 embedding，一次批量）。"""
    from .semantic import index_pending

    with console.status("[cyan]批量 embedding…"):
        res = index_pending(db(), limit=limit or None, rebuild=rebuild)
    console.print(f"[green]模型 {res['model']}：扫描 {res['scanned']}，"
                  f"新嵌 {res['embedded']}，向量总数 {res['total_vectors']}[/]")


@app.command()
def stats(days: int = typer.Option(7, "--days", "-d")) -> None:
    """LLM 用量：每天调用次数与 token。"""
    from .usage import summary as usage_summary

    data = usage_summary(days=days)
    table = Table(title=f"最近 {days} 天 LLM 用量")
    table.add_column("日期")
    table.add_column("调用", justify="right")
    table.add_column("输入", justify="right")
    table.add_column("输出", justify="right")
    table.add_column("合计", justify="right")
    for day, v in data["recent"].items():
        table.add_row(day, str(v["calls"]), f"{v['prompt']:,}", f"{v['completion']:,}", f"{v['total']:,}")
    console.print(table)
    t = data["total"]
    console.print(f"[dim]累计 {t['calls']} 次 · {t['total']:,} tokens[/]")
    if data["today"]:
        console.print(f"今天：{data['today']['calls']} 次 · {data['today']['total']:,} tokens")


@app.command()
def voice(
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="已有音频文件（wav/m4a/webm）"),
    seconds: int = typer.Option(8, "--seconds", "-s", help="录音秒数（需要配置 [voice].mic）"),
) -> None:
    """语音速记：录音/音频 → 本地 FunASR 转写 → 提炼入库。"""
    from .voice import record, transcribe_and_capture

    audio = str(file) if file else record(seconds)
    with console.status("[cyan]转写 + 提炼中…"):
        res = transcribe_and_capture(audio)
    console.print(Panel(res.get("transcript", ""), title="转写", border_style="cyan"))
    console.print(f"[green]已入库[/] {res.get('path')}（{res.get('seconds')} 秒）")


@app.command()
def repair(redistill: bool = typer.Option(False, "--redistill",
                                          help="把同源全连的 co_occurs 换成句级证据边")) -> None:
    """存量修复：重建共现边（句级证据、weight=来源数）。"""
    from .repair import redistill_cooccur

    if not redistill:
        console.print("用法：opc repair --redistill")
        return
    with console.status("[cyan]重建句级共现…"):
        res = redistill_cooccur()
    b, a = res["before"], res["after"]
    table = Table(title="共现边重建前后")
    table.add_column("指标")
    table.add_column("前", justify="right")
    table.add_column("后", justify="right")
    table.add_row("总边数", str(b["edges"]), str(a["edges"]))
    table.add_row("co_occurs", str(b["co_occurs"]), str(a["co_occurs"]))
    table.add_row("占比", f"{b['co_ratio']:.1%}", f"{a['co_ratio']:.1%}")
    table.add_row("top1 度", str(b["top1_degree"]), str(a["top1_degree"]))
    table.add_row("top10 平均度", str(b["top10_avg_degree"]), str(a["top10_avg_degree"]))
    console.print(table)
    console.print(f"[dim]处理来源 {res['sources']} 个，写入共现对 {res['pairs']} 条；"
                  f"related_to 归一 {res.get('related_to_normalized', 0)} 条[/]")


@app.command()
def rebuild(from_vault: bool = typer.Option(False, "--from-vault",
                                            help="从 Obsidian 库重建 SQLite 索引"),
            force: bool = typer.Option(False, "--force", help="Hub 运行时也强制替换")) -> None:
    """删库恢复：用 wiki/ 里的 Markdown 重建 sources / notes / entities / edges。"""
    from .rebuild import rebuild as do_rebuild

    if not from_vault:
        console.print("用法：opc rebuild --from-vault（会先备份 data/opc.db）")
        return
    from .db import db

    before_locked = before_pageless = 0
    try:
        with db().connect() as c:
            before_locked = c.execute(
                "SELECT (SELECT COUNT(*) FROM entities WHERE locked=1)"
                " + (SELECT COUNT(*) FROM concepts WHERE locked=1)").fetchone()[0]
            before_pageless = c.execute(
                "SELECT (SELECT COUNT(*) FROM entities WHERE locked=1"
                "        AND COALESCE(page_path,'')='')"
                " + (SELECT COUNT(*) FROM concepts WHERE locked=1"
                "   AND COALESCE(page_path,'')='')").fetchone()[0]
    except Exception:  # noqa: BLE001 — 旧库不可读时继续（重建本来就是修它）
        pass
    with console.status("[cyan]从库重建索引…"):
        res = do_rebuild(from_vault=True, force=force)
    table = Table(title="重建结果")
    table.add_column("对象")
    table.add_column("数量", justify="right")
    for k, v in res["counts"].items():
        table.add_row(k, str(v))
    console.print(table)
    console.print(f"[green]已切换[/] {res['swapped_to']}")
    console.print(f"[dim]旧库备份：{res['backup']}（重启 Hub 后生效）[/]")
    restored = int(res["counts"].get("locked") or 0)
    if before_locked and restored < before_locked:
        console.print(
            f"[yellow]！重建前锁定 {before_locked} 条，恢复 {restored} 条；"
            f"其中 {max(0, before_locked - restored)} 条没有页面可依（多为单来源实体），"
            f"需重新 approve 或先建页再锁。[/]")
        if before_pageless:
            console.print(f"[dim]其中无页面的锁定条目：{before_pageless} 条[/]")


@app.command()
def doctor() -> None:
    """自检：配置、密钥、库、连接器。"""
    problems: list[str] = []
    c = cfg()
    try:
        c.vault_path.exists() or problems.append(f"知识库路径不存在：{c.vault_path}")
    except Exception as exc:
        problems.append(str(exc))
    try:
        c.api_key()
    except Exception as exc:
        problems.append(str(exc))
    st = db().stats()
    if st["sources"] == 0:
        problems.append("还没有任何来源，先跑一次 opc capture 或 opc meeting ingest 1")
    for s in status_all():
        if s["enabled"] and not s["ok"]:
            problems.append(f"{s['name']}：{s['detail']}")
    if problems:
        for p in problems:
            console.print(f"[yellow]![/] {p}")
    else:
        console.print("[green]一切正常[/]")
    console.print(f"[dim]数据目录 {c.data_dir} · 库 {c.db_path}[/]")


def _print_result(res: dict) -> None:
    if res.get("skipped"):
        console.print(f"[yellow]跳过（{res.get('reason')}）[/] source={res.get('source_id')}")
        return
    console.print(Panel(res.get("summary", "")[:800], title=f"✓ {res.get('title')}", border_style="green"))
    if res.get("path"):
        console.print(f"[cyan]已写入[/] {res['path']}")
    if res.get("entities"):
        console.print("[dim]实体：[/]" + "、".join(e["name"] for e in res["entities"][:10]))
    if res.get("concepts"):
        console.print("[dim]概念：[/]" + "、".join(c["name"] for c in res["concepts"][:8]))
    if res.get("actions"):
        console.print("[dim]行动项：[/]" + f"{len(res['actions'])} 条")


# ---------------------------------------------------------------- 数据本体
@ontology_app.command("lock")
def ontology_lock(
    kind: str = typer.Argument(..., help="entity | concept"),
    ident: str = typer.Argument(..., help="实体/概念的 id 或名称 key"),
    by: str = typer.Option("human", "--by", help="锁定人标识（审计用）"),
    force: bool = typer.Option(False, "--force", help="跳过模型判断直接锁定"),
) -> None:
    """锁定为不可变基准：人工确认 × 模型判断合理（AND）。"""
    import json as _json

    from . import ontology
    from .db import db

    rot = ontology.approve(db(), kind, ident, force=force, by=by)
    console.print(_json.dumps(rot, ensure_ascii=False, indent=2))


@ontology_app.command("unlock")
def ontology_unlock(
    kind: str = typer.Argument(..., help="entity | concept"),
    ident: str = typer.Argument(..., help="id 或名称 key"),
) -> None:
    """解除锁定（恢复可修改，必留审计）。"""
    import json as _json

    from . import ontology
    from .db import db

    console.print(_json.dumps(ontology.unlock(db(), kind, ident), ensure_ascii=False, indent=2))


@ontology_app.command("approve")
def ontology_approve(
    kind: str = typer.Argument(..., help="entity | concept"),
    ident: str = typer.Argument(..., help="id 或名称 key"),
    force: bool = typer.Option(False, "--force", help="模型判断未通过时仍锁定（审计 override）"),
    confirm_only: bool = typer.Option(False, "--confirm", help="只确认（confirmed），不锁定"),
    reject: bool = typer.Option(False, "--reject", help="退场（rejected）：保留行，从图/检索隐藏"),
) -> None:
    """审核队列：默认「模型判据 + 锁定」；`--confirm` 只确认，`--reject` 退场。"""
    import json as _json

    from . import ontology
    from .db import db

    if confirm_only or reject:
        out = ontology.review(db(), kind, ident,
                              action="reject" if reject else "confirm")
    else:
        out = ontology.approve(db(), kind, ident, force=force)
    console.print(_json.dumps(out, ensure_ascii=False, indent=2))


@ontology_app.command("conflicts")
def ontology_conflicts(
    auto: bool = typer.Option(False, "--auto", help="对 open 冲突批量跑 LLM 仲裁"),
    limit: int = typer.Option(6, "--limit", help="--auto 时每次最多仲裁几条"),
) -> None:
    """冲突台账：列表或自动仲裁。仲裁不修改锁定条目，overturn/unsure 均转人工。"""
    import json as _json

    from . import ontology
    from .db import db

    database = db()
    if auto:
        results = ontology.adjudicate_pending(database, limit=limit)
        console.print(_json.dumps(results, ensure_ascii=False, indent=2))
        return
    items = database.open_conflicts(limit=50)
    if not items:
        console.print("[green]没有 open 状态的冲突。[/]")
        return
    table = Table(title="冲突台账（open）", show_lines=True)
    table.add_column("ID", style="dim")
    table.add_column("对象", overflow="fold")
    table.add_column("字段")
    table.add_column("锁定值", overflow="fold")
    table.add_column("新数据", style="yellow", overflow="fold")
    for it in items:
        row = database.object_row(it["kind"], int(it["ref_id"]))
        table.add_row(str(it["id"]), f'{it["kind"]}:{row["name"] if row else it["ref_id"]}',
                      it["field"], it["locked_value"] or "-", it["incoming_value"] or "-")
    console.print(table)
    console.print("[dim]逐条仲裁：python -m opc ontology conflicts --auto，或人工处置后在 UI/REST 里 dismiss。[/]")


@ontology_app.command("locked")
def ontology_locked() -> None:
    """盘点已锁定的不可变基准。"""
    from .db import db

    database = db()
    table = Table(title="不可变基准（locked）")
    table.add_column("类型")
    table.add_column("ID", style="dim")
    table.add_column("名称", overflow="fold")
    table.add_column("类型值/type")
    table.add_column("锁定于")
    table.add_column("锁定人", style="dim")
    with database.connect() as c:
        for tbl, kind in (("entities", "entity"), ("concepts", "concept")):
            for r in c.execute(f"SELECT * FROM {tbl} WHERE locked=1").fetchall():
                table.add_row(kind, str(r["id"]), r["name"],
                              (r["type"] if kind == "entity" else "-") or "-",
                              r["locked_at"] or "-", r["locked_by"] or "-")
    console.print(table)


@ontology_app.command("relock")
def ontology_relock(apply: bool = typer.Option(False, "--apply",
                                               help="真的写回库（默认只统计）")) -> None:
    """从页面 frontmatter 恢复锁定态（重建后/手工改页后）。"""
    import json as _json

    from . import ontology
    from .db import db

    console.print(_json.dumps(ontology.relock(db(), apply=apply),
                              ensure_ascii=False, indent=2))
    if not apply:
        console.print("[dim]落库：python -m opc ontology relock --apply[/]")


@ontology_app.command("types")
def ontology_types(kind: str = typer.Option("entity", "--kind", help="entity | concept")) -> None:
    """类型分布（M1）：各类型计数/占比、枚举外占比、状态计数。"""
    from . import ontology
    from .db import db

    res = ontology.types(db(), kind)
    table = Table(title=f"类型分布（{kind}，共 {res['total']}）")
    table.add_column("类型")
    table.add_column("数量", justify="right")
    table.add_column("占比", justify="right")
    for d in res["types"]:
        table.add_row(d["type"], str(d["count"]), f"{d['ratio']:.1%}")
    console.print(table)
    console.print(f"[dim]枚举外 {res['outside_enum']}（{res['outside_ratio']:.1%}）"
                  f" · other {res['other_ratio']:.1%} · locked {res['locked']}"
                  f" · merged {res['merged']}[/]")
    if res["status"]:
        console.print("[dim]状态：" + " ".join(f"{k or '(空)'}={v}"
                                              for k, v in sorted(res["status"].items())) + "[/]")


@ontology_app.command("typefix")
def ontology_typefix(
    apply: bool = typer.Option(False, "--apply", help="落库（默认 dry-run）"),
    no_llm: bool = typer.Option(False, "--no-llm", help="只跑尾缀规则，不调模型"),
    limit: int = typer.Option(0, "--limit", help="最多处理多少条（0=全部）"),
) -> None:
    """存量类型校正：规则 + LLM 批量，默认只出方案。"""
    import json as _json

    from . import ontology
    from .db import db

    res = ontology.typefix(db(), apply=apply, use_llm=not no_llm,
                           limit=limit or None)
    console.print(f"[cyan]扫描 {res['scanned']} 条，提议 {res['proposed']} 条"
                  f"（{'已落库' if apply else 'dry-run'}）[/]")
    if res["changes"]:
        table = Table(title="类型变更")
        table.add_column("ID", style="dim")
        table.add_column("名称", overflow="fold")
        table.add_column("原类型")
        table.add_column("新类型")
        table.add_column("通道", style="dim")
        for ch in res["changes"][:50]:
            table.add_row(str(ch["id"]), ch["name"], ch["from"] or "-", ch["to"], ch["by"])
        console.print(table)
    before = res["before"]["other_ratio"]
    after = res["after"]["other_ratio"] if res.get("after") else None
    console.print(f"[dim]other 占比：{before:.1%}" +
                  (f" → {after:.1%}" if after is not None else "（apply 后看 after）") + "[/]")
    if not apply:
        console.print("[dim]落库：python -m opc ontology typefix --apply[/]")


@ontology_app.command("candidates")
def ontology_candidates(
    limit: int = typer.Option(30, "--limit"),
    min_jaccard: float = typer.Option(0.7, "--jaccard", help="共现邻居 Jaccard 阈值"),
    min_neighbors: int = typer.Option(3, "--min-neighbors", help="邻居通道的最小度数"),
) -> None:
    """归并候选：同名/别名/邻居三通道，只提议不合并。"""
    from . import ontology
    from .db import db

    res = ontology.candidates(db(), limit=limit, min_jaccard=min_jaccard,
                              min_neighbors=min_neighbors)
    console.print(f"[cyan]生成候选 {res['generated']} 对，open {len(res['open'])} 条[/]")
    if not res["open"]:
        return
    table = Table(title="归并候选（open）")
    table.add_column("ID", style="dim")
    table.add_column("A", overflow="fold")
    table.add_column("B", overflow="fold")
    table.add_column("分数", justify="right")
    table.add_column("理由", overflow="fold")
    for r in res["open"]:
        table.add_row(str(r["id"]), r["a_name"], r["b_name"],
                      f"{r['score']:.2f}", r.get("reason") or "")
    console.print(table)
    console.print("[dim]合并：python -m opc ontology merge <A> <B>[/]")


@ontology_app.command("merge")
def ontology_merge(
    a: str = typer.Argument(..., help="保留端（id 或名称）"),
    b: str = typer.Argument(..., help="被并入端（id 或名称）"),
    by: str = typer.Option("human", "--by"),
    reason: str = typer.Option("", "--reason"),
) -> None:
    """执行归并：别名并入、边重指、来源集合合并、状态 merged、审计。"""
    import json as _json

    from . import ontology
    from .db import db

    console.print(_json.dumps(ontology.merge(db(), a, b, by=by, reason=reason),
                              ensure_ascii=False, indent=2))


@app.command()
def caps(check: bool = typer.Option(False, "--check", help="只校验四个入口与注册表是否一致")) -> None:
    """能力注册表：REST / MCP / CLI / UI 的同一份清单。"""
    from . import capabilities

    problems = capabilities.validate()
    if check:
        if problems:
            for p in problems:
                console.print(f"[red]×[/] {p}")
            raise typer.Exit(1)
        n = len(capabilities.CAPABILITIES)
        console.print(f"[green]能力注册表一致（{n} 项）[/]")
        return
    res = capabilities.summary()
    table = Table(title=f"能力注册表（{res['total']} 项）")
    table.add_column("ID", style="dim")
    table.add_column("名称", overflow="fold")
    table.add_column("类型", style="dim")
    table.add_column("CLI", style="dim")
    table.add_column("MCP", style="dim", overflow="fold")
    table.add_column("REST", style="dim", overflow="fold")
    for c in res["capabilities"]:
        rest = f"{c['rest']['method']} {c['rest']['path']}" if c["rest"] else "-"
        table.add_row(c["id"], c["label"], c["kind"], c["cli"] or "-",
                      ", ".join(c["mcp"]) or "-", rest)
    console.print(table)
    if problems:
        console.print("[yellow]！一致性检查未通过：[/]")
        for p in problems:
            console.print(f"  - {p}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
