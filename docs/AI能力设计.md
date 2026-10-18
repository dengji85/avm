# AI 能力设计

> 本文说明片匣 AI 相关代码的分层与约定。**新增 AI 功能前先读这篇**，
> 避免把模型调用散落在各个接口里，导致后面无法限流、无法统计、无法换模型。

## 1. 为什么分层

AI 功能早期都是「一个接口一段 OpenAI 调用」，看着简单，问题很快会集中爆发：

| 症状 | 根因 |
| --- | --- |
| 换个模型要改十几处 | HTTP 调用散落 |
| 接口偶发 502 无法定位 | 没有统一超时 / 重试 / 日志 |
| 同一个请求重复烧钱 | 没有缓存 |
| 加了功能就担心账单失控 | 没有预算与用量统计 |
| 批量跑一次 NAS 被打满 | 没有任务队列与限流 |

所以固定成四层，每层只解决一个问题。

## 2. 四层架构

```
┌──────────────────────────────────────────────────────┐
│ 应用层  app/ai.py                                     │
│   写「说什么」：简介 / 剧情分析 / 标签 / 搜索意图 /（未来）翻译、问答、纠错 │
├──────────────────────────────────────────────────────┤
│ 能力层  app/ai_core.py                                │
│   写「怎么和模型说话」：配置、统一 chat/chat_json、      │
│   重试退避、超时、缓存、统计、错误归一、提示词版本登记     │
├──────────────────────────────────────────────────────┤
│ 任务层  app/ai_jobs.py（规划中）                       │
│   批量、限流、预算、可取消、断点续跑、失败重试上限          │
├──────────────────────────────────────────────────────┤
│ 取材层  app/subtext.py（已有）、抽帧/向量（规划中）        │
│   内嵌字幕轨 / 外挂字幕 / 元数据 / 封面与抽帧               │
└──────────────────────────────────────────────────────┘
```

### 2.1 能力层 `app/ai_core.py`（已落地）

| 能力 | 说明 |
| --- | --- |
| `AIConfig.from_config(cfg)` | 读 `config["ai"]`：`enabled/base_url/api_key/model/temperature/timeout/max_retries/extra_headers` |
| `is_available(cfg)` | 是否开启 |
| `chat(cfg, messages, *, max_tokens, purpose, temperature, use_cache)` | 统一文本入口，返回字符串 |
| `chat_json(cfg, messages, ...)` | 统一 JSON 入口，自动截取 `{}` 段并解析 |
| 重试 | 网络错误 / 429 / 5xx 退避重试（默认 2 次，`ai.max_retries` 可配），4xx 不重试 |
| 超时 | 连接 8s、读超时 `ai.timeout`（默认 60s） |
| 缓存 | `use_cache=True` 时，模型+消息+参数完全相同的请求 10 分钟内复用 |
| 统计 | `stats()`：调用次数 / 失败 / 缓存命中 / 出入字符数 / 最近模型与错误 |
| 错误 | 一律抛 `AiError`，接口层转成可读的 4xx/5xx，不把 requests 异常抛给前端 |
| 提示词版本 | `register_prompt(name, version)` / `prompt_versions()`，对应关系见 `GET /api/ai/status` |

### 2.2 应用层 `app/ai.py`（已落地）

只放业务提示词与后处理，每个入口登记版本号：

| 函数 | 输入 | 提示词版本 |
| --- | --- | --- |
| `generate_synopsis(movie, cfg)` | 元数据（无字幕时的回退） | `synopsis v1` |
| `summarize_subtitles(text, movie, cfg)` | 字幕文本（分块 → 分段摘要 → 汇总） | `plot.subtitle v2` |
| `suggest_tags(movie, cfg)` | 元数据 | `tags v1` |
| `parse_search_intent(query, cfg)` | 自然语言 → JSON | `search.intent v1` |

约定：

1. **不允许**在应用层直接 `import requests` 调模型，一律走 `chat` / `chat_json`。
2. 提示词只在本层维护，版本号递增必须同步本文档表格。
3. 模型返回异常（非 JSON、空内容）由应用层决定降级策略，例如
   `parse_search_intent` 失败时回退为关键词搜索，不能让接口 500。
4. 所有写库结果必须能人工改 + 有来源标记（剧情已支持：`ai-subtitle/ai-meta/manual`）。

## 3. 接口约定

| 接口 | 行为 |
| --- | --- |
| `GET /api/ai/status` | `enabled/model/stats/prompts`，诊断 AI 是否配置正确 |
| `POST /api/ai/generate-synopsis/{id}` | 字幕优先 → 元数据回退；写回 `movies.plot`，记录 `plot_source/ai_plot_at` |
| `POST /api/ai/suggest-tags/{id}` | 返回标签建议，**不自动写入** |
| `POST /api/ai/search-intent` | 自然语言 → 检索条件，失败回退关键词 |

## 4. 任务层 `app/ai_jobs.py`（已落地）

批量能力由任务层提供，单部点按钮仍走同步接口：

```python
ai_jobs.register_handler("translate", fn)   # fn(movie_id, payload) -> dict
ai_jobs.submit("translate", [{"movie_id": 1}, ...], payload={"target": "zh-Hans"})
```

| 机制 | 行为 |
| --- | --- |
| 状态机 | `queued → running → done / failed / cancelled / skipped`，每任务一行 `ai_jobs` |
| 并发 | `ai.job_concurrency`（默认 2）；**配了预算上限时自动降为单线程**，避免并发同时通过预算检查 |
| 预算 | `ai.budget_chars_per_run`（按本批次实际送出的 prompt 字符计），跑前检查 + 用尽即跳过剩余 |
| 取消 | `POST /api/ai/jobs/cancel`：后续排队任务标记 cancelled，运行中的跑完再退 |
| 隔离 | 单任务失败只记 `error`，不影响其他任务与库内数据 |
| 查询 | `GET /api/ai/jobs`：运行状态 + 最近 40 条 + 状态统计 |

批量入口：`POST /api/ai/jobs`，`{"kind":"translate|plot", "ids":[...]}` 或 `{"kind":..., "latest":N}`（最近 N 部）。

## 5. 字幕翻译 `app/translate.py`（已落地）

```
取材（subtext.collect_cues：内嵌中文文本轨 → 其他文本轨 → 外挂字幕）
  → 按条打包（每包约 1400 原文）请求 chat_json_list，索引对齐
  → 术语表强制照译（ai_glossary）
  → 合并成 SRT（原文+译文 或 仅译文），写 data/subs/{movie_id}/
  → 登记 movie_subtitles(source='ai-translate')，可下载 / 删除
```

要点：

- 返回是 **JSON 数组** `[{"i":0,"t":"译文"}]`，所以应用层自己解析（`_chat_json_list`），
  不用 `chat_json`（那个只处理对象）
- 打包失败自动降级为逐条；单条仍失败保留原文，**时间码与条数绝不错位**
- 单部最多处理 4000 条字幕（防异常字幕爆预算）
- 术语表：女优名 / 系列名 / 常规称呼，翻译时严格照译，解决译名漂移

## 6. 数据与缓存

| 表 | 用途 |
| --- | --- |
| `subtitle_cache(movie_id, lang, kind, path, model)` | 翻译 / 转写字幕产物，避免重复生成 |
| `embeddings(movie_id, kind, vector, model)` | 字幕向量（剧情问答）、封面向量（以图搜片） |
| `glossary(term, translation, kind)` | 术语表：女优名 / 番号 / 系列固定译法，跨片复用，解决翻译译名漂移 |

## 7. 隐私与成本红线

1. 默认不外发任何内容；云端调用必须显式开启，并在界面上说明「即将发送什么到哪个端点」。
2. 批量 AI 必须有预算上限与二次确认。
3. 缓存键包含原文内容，落盘前必须考虑敏感性（当前缓存仅在内存，10 分钟过期）。
4. 日志只记录长度 / 模型 / 错误摘要，**不记录字幕正文与密钥**。

## 7. 路线图

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| A | 取材：内嵌字幕探测 / 字幕文本提取 / 元数据回退 | 已完成 |
| B | 应用：剧情分析、标签建议、搜索意图 | 已完成 |
| C | 能力/应用分层：`ai_core` + `ai`、重试、缓存、统计、提示词版本 | 本次完成 |
| D | 任务层：`ai_jobs` 队列 + 批量预算/限流 | 已完成（2026-10-18） |
| E | 字幕翻译（术语表 + 双语 + 播放器挂载） | 翻译与产物已完成；播放器内挂载待做 |
| F | 剧情 RAG 问答（字幕向量 + 时间戳引用） | 待办 |
| G | 视觉：封面质检 / 视觉标签 / 以图搜片 | 待办 |
| H | 规则推荐 → embedding 推荐 | 待办 |

## 8. 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-10-18 | 新增任务层 `ai_jobs`（批量 / 限并发 / 预算 / 取消）与字幕翻译 `translate`（cue 级打包、术语表、双语 SRT、`ai_glossary` 术语表）；新增 `ai.job_concurrency`、`ai.budget_chars_per_run` 配置与 `/api/ai/{jobs,glossary,translate}` 接口；详情页与设置页接入。 |
