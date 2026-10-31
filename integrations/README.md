# 把 OPC 接进你的 AI 客户端

三种方式，任选：

## 方式 A：MCP（推荐，OpenCode / Claude / PI 都能用）

MCP 服务器不需要常驻，客户端会按需拉起 `python -m opc.mcp_server`。

### OpenCode

编辑 `~\.config\opencode\opencode.jsonc`，加入顶层 `mcp` 字段：

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "opc": {
      "type": "local",
      "command": ["python", "-m", "opc.mcp_server"],
      "enabled": true,
      "environment": { "PYTHONPATH": "<repo>" }
    }
  }
}
```

重启 OpenCode 后，在会话里问"我上次会议说了什么"时它会自动调 `opc_ask`。

### Claude Desktop / 其它 MCP 客户端

`%APPDATA%\Claude\claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "opc": {
      "command": "python",
      "args": ["-m", "opc.mcp_server"],
      "env": { "PYTHONPATH": "<repo>" }
    }
  }
}
```

### 可用工具

| 工具 | 作用 |
|---|---|
| `opc_capture` | 记一段内容，自动提炼入库 |
| `opc_ask` | 基于知识库问答（带出处） |
| `opc_search` | 全文检索（快、便宜） |
| `opc_meetings` | 列本地会议 |
| `opc_ingest_meeting` | 导入某场会议 |
| `opc_ingest` | 导入文件/URL |
| `opc_tools` | 工具栈状态 |
| `opc_synthesize` | 跨来源综述一个主题 |
| `opc_graph` | 取图谱节点与边 |
| `opc_ontology_status` | 本体状态：类型分布/待审数/候选 |
| `opc_ontology_merge` | 归并两个实体（A 保留、B 并入） |
| `opc_ontology_review` | 实体确认 / 退场 |
| `opc_note` | 随手注记（带当前锚点，不触发 LLM） |
| `opc_notes` | 列出注记（recent/focused/…） |
| `opc_graph_edit` | 图谱改名 / 连线 / 断线（可撤销） |

在本机运行 `python -m opc mcp` 会直接打印可粘贴的 OpenCode / Claude 配置与工具清单。

## 方式 B：HTTP（任何能发请求的程序）

```powershell
# 记一条
$body = @{ text = "要记的内容" } | ConvertTo-Json -Compress
Invoke-RestMethod http://127.0.0.1:5370/api/capture -Method Post `
  -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
  -ContentType "application/json; charset=utf-8"

# 问一句
Invoke-RestMethod "http://127.0.0.1:5370/api/ask" -Method Post `
  -Body ([Text.Encoding]::UTF8.GetBytes((@{ question = "上次会议结论" } | ConvertTo-Json -Compress))) `
  -ContentType "application/json; charset=utf-8"

# 流式（SSE，v0.9 起只支持 POST；GET 版本已因成本/CSRF 移除）
curl.exe -N -X POST http://127.0.0.1:5370/api/ask/stream `
  -H "Content-Type: application/json" `
  -d '{"question":"总体能力"}'
```

完整接口：<http://127.0.0.1:5370/api/docs>

## 方式 C：CLI（脚本 / 计划任务）

```powershell
$env:PYTHONPATH="<repo>"
python -m opc meeting ingest 1
python -m opc sync
```

配合 Windows 计划任务，可做「每天 22:00 扫一遍目录 + 生成日报」。
