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
def ask(
    question: str = typer.Argument(..., help="要问的问题"),
    save: bool = typer.Option(False, "--save", "-s", help="把答案存成 wiki/synthetic 页"),
) -> None:
    """基于知识库回答，带出处。"""
    with console.status("[cyan]检索 + 生成…"):
        res = ask_pipeline(question, save=save)
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
def mcp() -> None:
    """以 MCP stdio 服务器运行（供 OpenCode / PI / Claude / DSH 调用）。"""
    from .mcp_server import serve_stdio

    serve_stdio()


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
    console.print(f"[dim]处理来源 {res['sources']} 个，写入共现对 {res['pairs']} 条[/]")


@app.command()
def rebuild(from_vault: bool = typer.Option(False, "--from-vault",
                                            help="从 Obsidian 库重建 SQLite 索引"),
            force: bool = typer.Option(False, "--force", help="Hub 运行时也强制替换")) -> None:
    """删库恢复：用 wiki/ 里的 Markdown 重建 sources / notes / entities / edges。"""
    from .rebuild import rebuild as do_rebuild

    if not from_vault:
        console.print("用法：opc rebuild --from-vault（会先备份 data/opc.db）")
        return
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
