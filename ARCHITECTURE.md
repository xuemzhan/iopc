# OPC 架构

## 1. 目标与非目标

**目标**
- 一个入口：无论内容来自会议、代码会话、网页、文件还是随手一记，都走同一条流水线。
- 一种落点：Obsidian 里可读、可链接、可被 Obsidian 自身插件（Dataview / Smart Connections / Graph）继续消费的 Markdown。
- 可追溯：每条结论都能回到原文；每次写入都可重跑、可审计。
- 可扩展：加一个工具 = 加一个适配器，不动中枢。

**非目标**
- 不做第二套笔记系统；不和 Obsidian 抢编辑器。
- 不做通用 Agent 编排器（那是 OpenCode / PI / WorkBuddy 的活），OPC 只做"记忆与知识"。
- 不做多用户/云端服务；这是单机、本地优先、个人尺度（10 万级文档以内）。

## 2. 分层

```
┌──────────────────────────── 入口层 ────────────────────────────┐
│ Web 控制台(:5370)   CLI   速记窗(Ctrl+Alt+O)   MCP stdio   REST │
└───────────────────────────────┬────────────────────────────────┘
                                │ 只调用服务层函数，不直接碰 SQL
┌───────────────────────────────▼────────────────────────────────┐
│                          服务层                                 │
│  capture() 幂等捕获 · distill_source() 提炼 · ask() 问答        │
│  write_canvas() 导图 · run_worker() 任务队列                    │
└───────┬─────────────────────────────────────┬──────────────────┘
        │                                     │
┌───────▼──────────────┐            ┌─────────▼──────────────────┐
│     连接器层          │            │        提炼层               │
│ imeeting / obsidian  │            │ chunk → LLM 抽取 → merge    │
│ opencode / pi        │            │ 实体归并 → 边 → Markdown    │
│ workbuddy / files    │            └─────────┬──────────────────┘
│ web / mindmap        │                      │
└───────┬──────────────┘                      │
        │                                     │
┌───────▼─────────────────────────────────────▼──────────────────┐
│  存储层：SQLite(data/opc.db) + Obsidian Markdown(wiki/)         │
│  sources / documents / chunks / notes / entities / concepts     │
│  edges / jobs / events / meta    +  FTS5(trigram)               │
└─────────────────────────────────────────────────────────────────┘
```

**为什么是 SQLite + Markdown 双写**
- SQLite 负责"机器视角"：结构化、可查询、可建边、可统计。
- Markdown 负责"人的视角"：Obsidian 里可读、可链、可被 50 个插件消费。
- 两者以 `slug` 对齐；Markdown 是产物，SQLite 是索引。丢了 SQLite 可以从 Markdown 重建索引，反过来不行——所以 **Markdown 优先**。
- 重建已实现：`opc rebuild --from-vault`（子进程建临时库 → 成功后原子替换 → 旧库留 `.bak-时间戳`）。

## 3. 数据模型

| 表 | 作用 | 关键约束 |
|---|---|---|
| `sources` | 一条来源（会议/笔记/文件/网页/会话） | `UNIQUE(kind, external_id)`，`content_hash` 用于幂等 |
| `documents` | 来源的规范化正文（一源一文） | `UNIQUE(source_id)` |
| `chunks` | 分块（供检索与长文抽取） | 与 `chunks_fts` rowid 对齐 |
| `notes` | 提炼后的知识页（含正文镜像） | `UNIQUE(slug, kind)` |
| `entities` | 实体（人/组织/系统/工具/项目/地点/事件） | `key` = 归一化名；`src_count`/`status` 控制是否建页进图 |
| `concepts` | 概念（方法/框架/术语/指标） | 同上 |
| `edges` | 关系边 | `UNIQUE(src,dst,rel)`，重复出现则 `weight += 1` |
| `jobs` | 任务队列 | 状态机 pending→running→done/failed |
| `events` | 审计事件流 | 只追加 |

关系边类型：

| rel | 语义 | 来源 |
|---|---|---|
| `mentions` | 笔记/来源提到某实体 | 抽取 |
| `covers` | 来源/笔记覆盖某概念 | 抽取 |
| `co_occurs` | 两个实体在同一来源共现 | 共现计算（weight 0.5） |
| `related_to` | 概念与实体相关 | LLM 给出的 `related_entities` |
| `distills` | 笔记由某来源提炼而来 | 写入时建立 |

`weight` 会随重复出现累加，因此"高频共现"天然浮到图谱中心，不需要额外算 PageRank。

## 4. 一次提炼的完整时序

```
capture(kind, external_id, title, text)
  ├─ sha256(text) ── 与 sources.content_hash 比对
  │     └─ 相同且已发布 → 直接返回 skipped（幂等，不烧 token）
  ├─ 写 inbox/YYYY-MM-DD/<slug>.md（原文留档，只增）
  ├─ upsert sources / documents / chunks（+ FTS）
  └─ distill_source(source_id)
        ├─ chunk_text(≤24000 字/块)
        ├─ 每块一次 LLM 抽取（JSON：摘要/要点/主张/实体/概念/行动项/标签/待追问题）
        ├─ merge（同义实体合并、别名合并、去重、截断）
        ├─ resolve_and_link
        │     ├─ 实体按归一化 key 归并；页面已存在则复用其 slug
        │     ├─ 写 entities / concepts
        │     └─ 写边：source→entity、source→concept、entity↔entity、concept→entity
        ├─ render_body（Markdown：要点/主张/行动项/实体/概念/待追/原始记录）
        └─ publish
              ├─ wiki/sources/<slug>.md
              ├─ wiki/entities|concepts/<slug>.md（存在则只追加「关联来源」）
              ├─ wiki/_index.md（存在同链接则跳过）
              ├─ wiki/_log.md（追加一行）
              └─ 回写 notes 表 + 建立 distills/mentions/covers 边
```

**失败语义**：任何一步抛错，`sources` 与 `documents` 已经落库，可重跑 `distill` 而不用重新抓取。写入 Markdown 用"临时文件 + 原子替换"，不会出现半个文件。

**幂等语义**：`(kind, external_id)` 决定"这是不是同一条来源"；`content_hash` 决定"内容有没有变"。会议还在录制时导入，会得到一个半场版本；会议结束后再 ingest 一次，内容哈希变化 → 重新提炼并覆盖同一页（`_note_slug` 复用已有 slug）。

## 5. 连接器契约

```python
class Connector:
    name: str
    kind: str                      # http / vault / sqlite / fs
    def status(self) -> Status     # 必须：给控制台一个"活着吗"的回答
    def capabilities(self) -> list[str]
```

约定：
1. **只读优先**：外部工具的数据库用 `file:...?mode=ro` 打开；只采集，不回写（iMeeting 的 `push_action` 是唯一例外，且是显式调用）。
2. **错误隔离**：`status_all()` 逐个 try/except，一个工具挂了不影响其他工具与控制台。
3. **采集即 capture**：连接器只负责"把外部的东西变成 (kind, external_id, title, text)"，其余交给服务层。
4. **不猜测 schema**：碰到未知格式（如 OpenCode 的 `part.data`）就按 JSON 泛化解析文本域，读不到就跳过，不报错阻塞。

## 6. 检索设计（以及为什么暂不上向量）

v0.3 起是**三通道 RRF 融合 + 查询扩展**：

```
问题 ──┬─ 别名通道：命中实体/概念名 → 取图谱里关联的笔记
       ├─ trigram FTS：中文 3 字滑窗，bm25 排序
       ├─ LIKE：兜住 2 字短词
       └─ 查询扩展：LLM 生成 3-5 个近义检索词（带缓存），再各跑一遍 FTS/LIKE
                          ↓
              RRF 按「名次」融合（不按分数），再叠加两个先验：
              · 标题覆盖度：查询词出现在标题里 → 最多 ×2.3
              · 综述页降权：日报/周报/体检页会命中一切词 → ×0.55（除非问的就是它）
```

**为什么不是简单拼接**：v0.2 是"别名→FTS→LIKE"串行取前 N 个，等于把"别名通道的偏好"强加给用户；而日报这类页面会命中几乎所有词，串行版本经常把它排在正主前面。RRF 只比较名次，天然免疫各通道分数尺度不同的问题。

**实测（12 条真实查询，脚本 `scripts/eval_search.py`）**

| 方案 | Recall@1 | Recall@3 | MRR |
|---|---|---|---|
| v0.2 串行 | 0.83 | 0.92 | 0.861 |
| v0.3 RRF | 0.92 | 0.92 | 0.917 |
| v0.3 RRF + 查询扩展 | **1.00** | **1.00** | **1.000** |

单个失败样本（"今天日报说了什么"）在串行版本零召回，加扩展后 rank=1——因为扩展词里出现了"日报/今日/汇总"。

> 样本只有 12 条，属于小样本方向性验证，不是 benchmark。但三个版本走同一套查询与判定，比较是同一把尺子。

**中文分词的两个坑**
- `unicode61` 会把"总体能力与总部能力的关系是什么"整句当一个 token → 零召回；
- `trigram` 要求 ≥3 字才能匹配，2 字词（"会议""架构"）必须 `LIKE` 兜底。

**为什么不立即上向量**
- Obsidian 的 `smart-connections` 已有嵌入索引，重复建设收益低；
- 单机 10 万级文档，FTS + 图谱别名 + 查询扩展已能覆盖大部分问法；
- 上向量的正确姿势是本地模型（Ollama + bge-m3），见 `TOOLSTACK.md` P1。`search.rrf=false` 可随时退回旧实现做 A/B。

**用量记账**：每次 LLM 调用写一行 `data/usage.jsonl`，`opc stats` 汇总每天调用与 token。不做精确计费，只回答"今天烧了多少"。

## 7. 入口层

| 入口 | 实现 | 说明 |
|---|---|---|
| Web 控制台 | 单文件 `opc/hub/static/index.html`（无构建、无 CDN） | 自动判断记/问；POST+SSE 多轮对话；引用可点开笔记预览；命令补全；Canvas 图谱 |
| CLI | `typer` | 全能力覆盖，适合脚本与排障 |
| MCP | 手写 JSON-RPC stdio（无第三方 SDK） | 9 个工具：capture / ask / search / meetings / ingest_meeting / ingest / tools / synthesize / graph |
| REST | FastAPI | 与 Web 同源，`/api/docs` 有 OpenAPI；`/api/voice` 接录音上传 |
| PWA | `manifest.webmanifest` + `sw.js` + 图标 | 可「添加到主屏幕」；只缓存静态壳，`/api/*` 永远走网络 |
| 语音 | 浏览器 MediaRecorder → `/api/voice` → ffmpeg 转 16k f32 → iMeeting `ws://…/ws/stream` | 复用已加载的 FunASR，不额外起服务；`opc voice --file` 走同一路径 |

SSE 细节：`POST /api/ask/stream` 先推 `citations`（含 `search_query` 与 `expansions`），再逐 token 推 `delta`，最后 `done`。LLM 调用是同步 httpx，用 `iterate_in_threadpool` 转成异步流，避免阻塞事件循环。

**多轮追问**：前端把最近 3 轮放进 `history`；服务端先用一次便宜调用把「那第二条呢」改写成可检索的问题（`ask.rewrite_followups`），再拿改写结果去检索，最后把 history + 检索结果一起交给模型。没有这一步，追问句在检索端等于零信息。

**会话存储（v0.5）**：`conversations` + `conversation_messages` 两张表。`/api/ask*` 不带 `conversation_id` 时自动建会话，服务端自己取最近 6 条历史——刷新页面、换设备都不丢上下文；右侧「对话」标签只是这张表的视图。

**任务队列（v0.5）**：`capture` / `ingest_meeting` / `voice` 都可异步。Hub 启动时拉起 `JobWorker` 线程轮询 `jobs` 表，用 `progress` / `stage` / `result` 三列对外汇报；前端每 1.2s 轮询 `/api/jobs/{id}` 画进度条。不传 `background=true` 时行为与同步版完全一致。

**访问控制（v0.5）**：`OPC_TOKEN` 存在时，非回环地址的请求必须带 `X-OPC-Token` 或 `?token=`；本机永远放行。`opc serve --lan` 绑 `0.0.0.0`，`opc token` 生成令牌写 `.env`。静态壳（`/`、`/sw.js`、图标）不校验令牌，否则登录页都打不开。

**语音限制**：浏览器录音要求安全上下文（`127.0.0.1` 或 HTTPS）。局域网 http 下 `getUserMedia` 会被浏览器禁用，手机语音需要 HTTPS 反代（Caddy/nginx + 自签证书）。

## 8. 部署与运维

- 单进程：`python -m opc serve`（uvicorn，默认 127.0.0.1:5370）。
- 端口占用为 5370（避开 iMeeting 的 8000/5173、DSH 的 5282、WorkBuddy 的 5122）。
- 日志：`data/*.log`、`events` 表；`opc jobs` 看队列，`opc worker --once` 手动排空。
- 备份：`data/opc.db` + Obsidian 库。Markdown 在 OneDrive 里本身有版本历史；`obsidian-git` 插件已装，建议开自动提交。

## 9. 值守与自维护（v0.2）

写入是一次性的，维护是长期的。v0.2 加了四件"自己会做"的事：

```
scheduler.Watcher（随 hub 启动的守护线程，默认 90s 一轮）
├─ check_meetings   会议 status=finished → ingest → 同步行动项
├─ check_inbox      inbox/ 新文件 → 扫描入库
├─ maybe_digest     到点（默认 22:00）→ 日报；周日再出周复盘
└─ maybe_lint       每周固定日（默认周日 21:30）→ 体检报告
```

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `scheduler.py` | 轮询与调度 | 任务间互相隔离；状态写 `meta` 表（`watch:last_digest`），重启不重复 |
| `digest.py` | 日报 / 周复盘 | 无新增来源则不写；一天只写一次（`force` 覆盖） |
| `lint.py` | 体检 | 结构检查纯本地；矛盾检测才调 LLM，且限次（默认 6） |
| `actions.py` | 行动项汇总 | 从 `body_md` 解析；已勾选状态在重写时保留 |
| `enrich.py` | 空壳页补全 | 只用已关联来源的摘要；素材不足就少写 |
| `prune.py` | 伪实体清理 | 删页前校验文件名与实体名一致，不一致只清库不动文件 |

**成本控制**：一次捕获 = 1 次 LLM 调用（长文按 24k 字分块）；日报 = 1 次；体检 = 最多 6 次矛盾比对；补全 = 每页 1 次。值守里会议检查是"先取哈希再决定是否提炼"，内容没变不烧 token。

**幂等与可重跑**：`capture` 用 `content_hash` 判重；`digest` 用文件存在性判重；`lint`/`actions` 每次覆盖同一文件；`enrich` 只填空位。任何一步失败都不破坏已有数据。

## 10. 扩展点与路线图

已完成（v0.1）
- [x] 8 个连接器、幂等捕获、LLM 提炼、实体/概念/边、Markdown 发布
- [x] 中文检索、问答带出处、SSE
- [x] Web / CLI / MCP / REST 四入口
- [x] Canvas 思维导图导出
- [x] 速记窗 + 全局热键

已完成（v0.2）
- [x] 值守：会议结束自动入库 + inbox 自动扫
- [x] 每日日报 / 每周复盘
- [x] 知识库体检（孤儿/断链/空壳/重复/矛盾）
- [x] 行动项汇总（保留勾选状态）+ 回写 iMeeting
- [x] 空壳实体/概念补全（grounded，不编造）
- [x] 伪实体清理 + 抽取阶段的名字过滤
- [x] 11 个单元测试（pytest，不碰网络与真实库）

已完成（v0.3）
- [x] 混合检索 v2：三通道 RRF + 标题覆盖度 + 综述页降权
- [x] 查询扩展（LLM 生成近义检索词，按查询哈希缓存 30 天）
- [x] 跨来源主题综述（`opc synthesize` / `/topic` / MCP `opc_synthesize`）
- [x] LLM 用量记账（`data/usage.jsonl` + `opc stats`）
- [x] 检索评测脚本 `scripts/eval_search.py`（12 条查询，三方案对比）
- [x] 14 个单元测试

已完成（v0.5 · 入口层）
- [x] 会话持久化 + 历史侧栏（服务端保存，刷新/换设备不丢）
- [x] 长任务异步化：jobs 的 progress/stage/result + 前端进度条（capture / ingest / voice）
- [x] 语音入口：浏览器录音 + `opc voice`，复用 iMeeting FunASR（TTS→ASR 往返验证通过）
- [x] PWA（manifest + service worker + 图标）与局域网令牌（127.0.0.1 放行，LAN 需 token）
- [x] 19 个单元测试

已完成（v0.6 · P0-B 五项补丁）
- [x] 敏感闸：关键词分级 + 机构/人名牌照化，回来还原（`opc/policy.py`）
- [x] 多模型路由：`for_task()` + 惰性密钥 + 每日预算 + `llm_usage` 表
- [x] 图形状：句级共现（evidence=原句）+ `weight`=来源数 + `opc repair --redistill`
- [x] 删库恢复：`opc rebuild --from-vault`（幂等，先备份后原子替换）
- [x] 单来源不建页（`src_count>=2` 或 `confirmed`）+ index 只写已有节
- [x] 38 个单元测试

下一步（按价值排序）
1. **语义检索**：Ollama + bge-m3 嵌入，`embeddings` 表 + 与 RRF 融合（`search()` 已是唯一入口）。
2. **浏览器/系统级捕获**：Web Clipper、截图 OCR、系统音频回环。
3. **矛盾自动落笔**：`lint --apply` 已能写 `> [!warning]`，下一步让值守默认执行并去重。
4. **会议中途快照**：录制中按 N 分钟做阶段性入库（默认关，避免 token 消耗）。
5. **主题页自动刷新**：来源新增后，已有主题综述标记「可能过期」并排队重建。

## 11. 已知限制

- LLM 抽取会有噪声（把"应该"当成行动项、把泛称当实体）；已限制条数、过滤文件名/路径类伪实体，并提供 `protect_existing`，但**仍需人工过一遍**。
- `wiki/_index.md` 与 Obsidian 插件 `karpathywiki` 的自动生成可能互相追加；当前策略是"存在即跳过"，不会重复，但格式由插件主导。
- 会议转写质量取决于 FunASR 与麦克风；说话人未标注时会显示"未标注"。
- 单机单用户：默认只监听 `127.0.0.1`；LAN 模式必须配 `OPC_TOKEN`，并校验 Host 白名单（防 DNS rebinding）。没有细粒度权限模型。
