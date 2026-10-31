# OPC — 个人智能工具中枢

把 **看到的、听到的、学到的** 收进一个地方：自动提炼、自动建链、写进 Obsidian，然后一个入口就能问。

> 单机 · 本地优先 · 个人尺度。仓库里只有代码与工具；你的知识内容（笔记、会议、数据库）永远只在本机。

## 结构

```
connectors ──► capture ──► distill ──► link ──► Obsidian(wiki/)
                  │            │
                  ▼            ▼
             SQLite(索引)    Web / CLI / MCP / REST
```

- **捕获**：会议转写、AI 会话、网页、文件、语音、随手记
- **提炼**：实体 / 概念 / 关键主张 / 行动项（LLM，可按任务路由到不同模型）
- **建链**：句级共现 + 别名归并，边带原句证据
- **入口**：Web 控制台、CLI、MCP（给 OpenCode / Claude 等）、REST、PWA

## 快速开始

```bash
git clone <this-repo> opc && cd opc
python -m venv .venv
# Windows: .venv\Scripts\activate ；macOS/Linux: source .venv/bin/activate
pip install fastapi uvicorn httpx pydantic pyyaml jinja2 lxml pypdf typer rich

cp config/opc.toml.example config/opc.toml
cp .env.example .env                     # 填 LLM key

# PowerShell: $env:PYTHONPATH="<repo>"
export PYTHONPATH="$PWD"

python -m opc doctor
python -m opc serve                      # http://127.0.0.1:5370
```

## 常用命令

```bash
python -m opc status
python -m opc capture "一句话"
python -m opc ask "上次会议结论"
python -m opc search "关键词"
python -m opc meeting list
python -m opc meeting ingest 1
python -m opc ingest https://example.com
python -m opc watch                 # 值守：自动入库 / 日报 / 体检
python -m opc digest daily
python -m opc lint
python -m opc actions
python -m opc enrich -n 8
python -m opc synthesize "主题"
python -m opc repair --redistill
python -m opc rebuild --from-vault
```

## 设计要点

- **Markdown 优先**：SQLite 只是索引，删了可以 `opc rebuild --from-vault` 从 Obsidian 重建。
- **幂等**：同内容重复捕获直接跳过；同一来源重跑不改边权。
- **敏感闸**：命中关键词的来源在送模型前把机构/人名令牌化，回来再还原（`opc/policy.py`）。
- **多模型路由**：`for_task("extract" | "ask" | "verdict" | …)` 选模型，带每日预算护栏。
- **单来源不建页**：实体要出现在 ≥2 个来源才进 vault / 图谱（可配置）。

细节见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 测试

```bash
python -m pytest tests -q
```

## 许可

未指定 License，默认保留所有权利；如需开源授权请自行添加 LICENSE。
