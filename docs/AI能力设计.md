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

## 4. 任务层 `app/ai_jobs.py`（已落地 · 2026-10-25 改为批次模型）

批量能力由任务层提供，单部点按钮仍走同步接口。**一次提交 = 一个批次**（`ai_batches` 一行），
逐片任务在 `ai_jobs` 里靠 `batch_id` 关联；列表页先看批次，点进去看逐片明细。

```python
ai_jobs.register_handler("translate", fn)   # fn(movie_id, payload) -> dict
batch_id, ids = ai_jobs.submit("translate", [{"movie_id": 1}, ...],
                               payload={"target": "zh-Hans"},
                               meta={"scope": "untranslated", "budget": 2000000})
```

| 机制 | 行为 |
| --- | --- |
| 状态机 | 批 `queued → running → done / cancelled`；片 `queued → running → done / failed / skipped / cancelled` |
| 范围 | 预设 + 可选筛选（见下表），由 `api._ai_scope_ids` 解析成影片 id |
| 并发 | `ai.job_concurrency`（默认 2），**可按批次覆盖**；预算生效时自动降为单线程（避免并发同时通过预算检查） |
| 预算 | `ai.budget_chars_per_run`（0 = 不限），按本批实际送出的字符累计；跑前检查 + 用尽即跳过剩余 |
| 取消 | `POST /ai/jobs/{id}/cancel`：本批排队项标 cancelled，运行中的跑完即止；不再影响其他批次 |
| 重试 | `POST /ai/jobs/{id}/retry`：把本批 failed/skipped/cancelled 复制成新批次重跑 |
| 隔离 | 单任务失败只记 `error`，不影响其他任务与库内数据 |
| 查询 | `GET /ai/jobs`（批次列表 + 当前批次 + scopes/targets）、`GET /ai/jobs/{id}`（按状态筛选 + 分页明细） |
| 清理 | 只保留最近 30 个批次，超出连同明细删除 |

**范围预设**

| kind | scope | 含义 |
| --- | --- | --- |
| translate | `untranslated` | 有可用字幕（外挂或有内嵌文本轨）且没有目标语言译文 |
| translate | `all_subtitled` | 所有有字幕的（会重译覆盖） |
| plot | `no_plot` | `plot` 为空 |
| plot | `from_meta` | `plot_source='ai-meta'`（想改用字幕重新生成） |
| 两者 | `failed` | 上一批失败/跳过/取消的片（可传 `from_batch` 指定批次） |
| 两者 | `ids` / `latest` / `filters` | 手动挑片 / 最近 N 部 / 按类型·女优·厂牌·年份筛 |

**接口**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/ai/jobs/preview` | 只估算命中数量、样例与预计字符量，不提交 |
| POST | `/api/ai/jobs` | `{kind, scope, filters?, limit, target?, bilingual?, concurrency?, budget?}` → `{batch_id, count}` |
| GET | `/api/ai/jobs` | 批次列表 + 当前批次 + `scopes` / `targets` / 并发与预算默认值 |
| GET | `/api/ai/jobs/{batch_id}` | 批次详情 + 明细（`?status=&page=&size=`） |
| POST | `/api/ai/jobs/{batch_id}/cancel` | 取消该批 |
| POST | `/api/ai/jobs/{batch_id}/retry` | 重试该批失败项（生成新批次） |
| DELETE | `/api/ai/jobs/{batch_id}` | 删除批次与明细（不动影片数据） |

> 兼容：旧的 `POST /api/ai/jobs` 只传 `{"latest": N}` 仍可用（走 `latest` 范围）；旧的 `POST /api/ai/jobs/cancel` 会取消当前在跑的批次。

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
- 打包失败自动降级为逐条（**降级请求同样带术语表**）；单条仍失败保留原文，**时间码与条数绝不错位**
- 单部最多处理 4000 条字幕（防异常字幕爆预算）
- 术语表：**只注入本批字幕里真正出现过的条目**（`translate.glossary_hits`，子串匹配，单批上限 200 条），
  术语表再大也不会随批次数线性膨胀；条目可按目标语言隔离（`ai_glossary.lang`，空 / `any` = 通用），
  避免翻英文时把中文译名当噪声；录入支持 `=` / `=>` / Tab / 中英文冒号与 `[zh-Hant]` 语言前缀，解析失败的行会提示
- 计费口径：`translate_movie` 返回的 `chars` 是**实际送出的 prompt 字符数**（含 system 与术语表），
  与批次预算同一口径；原文 cue 字符数另存 `cue_chars`
- 候选生成：`GET /api/ai/glossary/suggest` 从库里生成三类候选（不落库、不做机器翻译猜测）——
  `alias → name` 别名映射、含假名的女优名「保持原名」、≥2 部作品的系列名「不译」；
  已存在于术语表的 term 自动跳过。前端只把候选追加到编辑框，用户确认保存后才生效。

## 6. 数据与缓存

| 表 | 用途 |
| --- | --- |
| `subtitle_cache(movie_id, lang, kind, path, model)` | 翻译 / 转写字幕产物，避免重复生成 |
| `embeddings(movie_id, kind, vector, model)` | 字幕向量（剧情问答）、封面向量（以图搜片） |
| `ai_glossary(term, translation, kind, lang)` | 术语表：女优名 / 番号 / 系列固定译法，跨片复用；`lang` 为空 / `any` 表示通用，其余值只在翻成该语言时注入 |

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

## 8. 问库（库模式）与快捷提问

自然语言问库：模型只看到**问题本身**，把它解析成 `{kind, params}` 规格，统计与筛选全部在本机执行。

| kind | 用途 |
| --- | --- |
| `search` | 按条件找片（keywords / genres / actress / studio / series / year / flags / sort） |
| `recent` | 最近入库 |
| `taste` | 口味画像（8 个维度，按观看时长占比） |
| `watch_stats` | 看过多少 / 场次 / 累计时长 / 本月场次 |
| `health` | 库健康（总数、容量、缺简介/女优/封面、磁盘、组件状态） |
| `recommend` | 口味加权推荐（规则计算，不花 token） |

**快捷提问**（助手面板里的 chips）走 `POST /api/ai/library-run`：前端直接传 `{kind, params}`，
**不经过模型**——点一下立即出结果、不消耗 token，AI 未被启用时同样可用。
`polish: true` 时才会在本地执行后调用模型做「AI 解读」（仍只发送聚合摘要，见第 7 节）。

chip 按动机分四组：决策（今晚看什么 / 最近入库）、我的口味与数据（口味画像 / 今年口味 / 看了多少）、
快速找片（最高评分未看 / 收藏未看 / 内嵌中文字幕 / 4K / 无码 / 群星荟萃）、
库管理（库健康 / 未刮削 / 没封面）。新增 chip 只需在 `AiWorkbench.libGroups` 里加一条
`{label, kind, params}`，后端无需改动。

### 8.1 库字典 + 规划/重规划（口语 → 查询条件）

**问题**：只给模型一段「参数说明」时，它会凭常识猜类型名。库里存的是 `パンスト・タイツ`、
`ナース`、`人妻`，用户说的是「丝袜」「护士」「已婚」，猜中全靠运气；猜不中就是一个条件都
匹配不上——如果参数名再写错一个（`genres` 写成 `genre`），条件会被静默忽略，直接返回全库。

**做法**（`app/ai_library.ask_library` + `app/vocab.py`）：

```
用户口语 + 库字典（真实类型/标签/片商/系列/女优 + 数量）
      ↓  规划（chat_json，purpose=library.qa）
   {kind, params, why}
      ↓  本机执行（execute_spec → store.search_movies）
   0 条？ → 把「上次规格 + 0 条」反馈回去，带着库字典再规划一次（purpose=library.qa.repair）
      ↓
   结果 + plan{why}（前端显示「理解为：…」）
```

要点：

- **库字典**：`store.facets()` 取真实值，按出现次数排序，每类目有字符预算，整体约 6k 字符；
  按「影片数 + 最大 id」做签名缓存，5 分钟复用。只含分类词与计数，不含片名/简介。
- **值域约束**：提示词要求 `genres`/`tags`/`actress` 必须用字典原文，找不到才用 `like` 模糊匹配；
  跨语言对应（丝袜 → `パンスト・タイツ`）由模型完成，不再靠硬编码同义词表。
  `app/concepts.py` 的同义词表保留为**无模型时的兜底**。
- **一次重规划**：只对「movies 且 0 条」触发，最多 2 次模型调用；仍为空则如实返回 0。
- **可观测**：结果里带 `plan.why`、`repaired`，前端直接展示「理解为：…」，猜歪了用户能立刻发现。
- **成本**：字典约 1.2–1.5k token；短句（≤12 字且本地可解析）走本地条件，不花 token。

**顺带修掉的两个静默 bug**（都会表现为「不管问什么都返回全库」）：

1. `store._build_where` 只认 `genre`/`tag` 单数，而意图规格给的是 `genres`/`tags` → 条件被丢弃；
2. `ai_library._movie_items` 没有把 `keywords` 翻译成检索层的 `q` → 关键词条件被丢弃。

### 8.2 function calling（`ai_core.chat_tools`）

规划器不再只靠「让模型吐 JSON」：

| 层 | 行为 |
| --- | --- |
| `ai_core.chat_tools` | 带 `tools` / `tool_choice` 的一次调用，返回 `{content, tool_calls}`；重试、超时、统计、轨迹与 `chat` 同口径 |
| `ai_library._TOOLS` | `search_movies`（全部检索参数，`why` 必填）、`library_stats`（taste / watch_stats / health / recent / recommend） |
| 回退 | 端点返回 4xx（多半不认 `tools`）→ 抛 `retryable=False`，`_tools_ok=False` 记住，本进程后续直接走 JSON 模式，不再白付一次失败请求 |
| 一致性 | 两条路都产出 `{kind, params, why}`，`execute_spec` 与结果卡完全无感 |

注意：`chat_tools` 最终抛出的异常必须保留 `retryable`，否则回退判断永远不生效（踩过一次）。

### 8.3 MCP 服务器（`app/mcp_server.py`）

不自己写 agent，而是把库能力做成 **MCP 工具**，交给现成客户端（Claude Desktop / Cursor / Cherry Studio 等）驱动——规划、联网搜索、多轮调用都由客户端提供。

```
pip install -r requirements-mcp.txt
python run.py mcp        # stdio
```

| 工具 | 用途 |
| --- | --- |
| `library_vocab` | 库字典（真实类型/标签/片商/系列/女优 + 数量），检索前先看 |
| `search_movies` | 按条件检索；返回 `understood_as` 与紧凑卡片（不含封面路径/文件列表） |
| `movie_detail` | 按番号或 id 取完整资料（裁剪到 18 个有用字段） |
| `library_stats` | 口味 / 观看统计 / 库健康 / 最近入库 / 推荐 |

约束：全部只读本地 SQLite，不联网、不写库；`mcp` 是可选依赖，未安装时 `avm mcp` 给出安装提示并退出码 2。

### 8.4 内置 agent（`app/agent.py`）

助手面板跑的是**多步 agent**，不是「一次规划」：

| 项 | 说明 |
| --- | --- |
| 工具 | `search_movies` / `movie_detail` / `library_stats` / `library_vocab`（与 MCP 同一批实现，卡片裁剪共用 `agent.card/detail`） |
| 循环 | 模型调工具 → 结果回灌 → 再调，直到给出结论；`max_steps=4` 硬上限，`stopped=max_steps` 表示被截断 |
| 容错 | 工具异常、参数非法 → 转成工具错误回灌，不中断整轮；单次结果超 3500 字符截断再回灌 |
| 返回 | `{answer, steps, items, rows, note, stopped}`；`steps` 在结果卡里折叠展示（工具名 + 摘要 + 耗时） |
| 降级链 | 前端：agent → 单步规划 `/ai/library-qa` → 本地条件解析。任一层失败都还有结果，不报错 |
| 接口 | `POST /api/ai/agent {question}` |
| 流式 | `POST /api/ai/agent/stream`（SSE）：每完成一次工具调用推一条 `step`，结论走 `app/ai_core.chat_stream` 逐字推 `delta`，最后一条 `done` 与非流式返回同构。收尾固定用**不带工具的一次流式调用**——模型没有工具可调就只能把话说完，不会把步数烧光还不给结论。前端 `api.aiAgentStream` 用 fetch + ReadableStream 解析（EventSource 发不了 POST）。 |

**为什么不引入 agent 框架**：`ai_core` 已经管着重试、超时、用量、轨迹与模型池；框架自带 HTTP 客户端，接进来要么绕开这些能力，要么写一层适配器，而循环本身只有几十行。真正决定效果的是工具集与提示词。若以后要换引擎，`agent.TOOLS` 的 schema 可直接喂给 smolagents / PydanticAI。

### 8.5 联网查资料（`app/websearch.py`）

针对「影片没有元数据」：给 agent 加第 5 个工具 `web_lookup`——搜索 → 取前几条 → 逐页抽正文（bs4）→ 截断后交给模型汇总，**结论必须标注来源域名，抓不到就明说**。

| 项 | 说明 |
| --- | --- |
| 搜索后端 | 可插拔：`ai.websearch_url`（SearXNG，推荐自建）或 `ai.websearch_key`（Tavily）。**默认都不配 = 永不联网**；没配时该工具不出现在工具清单里 |
| 隐私 | 查询词（番号等）会发给所配服务，所以做成显式 opt-in；AI 中心有开关卡与「测试连接」自检 |
| 健壮性 | 403（未开 JSON 输出）/429（限流）/Key 无效都被翻译成可操作的中文提示；单页失败不拖垮整体；结果缓存 30 分钟、两次搜索间隔 ≥1s、读超时 10s |
| 上下文 | 前端把当前影片的番号/标题随请求带给 agent（`movie` 字段），「这部讲什么，网上查查」才能落到位 |
| 自检 | `python run.py web-check "查询词"` |

## 9. 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-10-25 | 内置 **多步 agent**（`app/agent.py` + `POST /api/ai/agent`）：模型可连续调用 `search_movies` / `movie_detail` / `library_stats` / `library_vocab`，0 条时自行换词重试，最后给一句话结论；步数上限 4、工具异常不中断、结果回灌截断 3500 字符；前端按 agent → 单步规划 → 本地条件三级降级，结果卡折叠展示执行步骤。 |
| 2026-10-25 | 库查询改为 **function calling**：新增 `ai_core.chat_tools`（tools/tool_choice，与 `chat` 同口径的重试与统计），`ai_library` 优先用 `search_movies` / `library_stats` 两个工具，端点不支持时自动回退 JSON 模式；新增 **MCP 服务器** `app/mcp_server.py`（`python run.py mcp`），暴露 `library_vocab` / `search_movies` / `movie_detail` / `library_stats`，`mcp` 为可选依赖（`requirements-mcp.txt`）。 |
| 2026-10-25 | 问库改为「库字典 + 规划/重规划」：`app/vocab.py` 把真实类型/标签/片商/系列/女优（含数量）拼成字典喂给模型，跨语言对上分类名；`ask_library` 在 0 条时带失败反馈自动重规划一次，结果附 `plan.why` 供界面展示「理解为：…」。修复 `genre`/`tag` 只认单数与 `keywords` 未翻译成 `q` 导致条件被静默丢弃、总是返回全库的问题；新增 `actress_age_min/max`、`like`、`year_min/max` 检索条件。 |
| 2026-10-25 | 术语表升级：只注入本批字幕中命中的条目（上限 200，`glossary_hits`）、降级为逐条时也带术语表、新增 `ai_glossary.lang` 按目标语言隔离、录入支持 `=>` / Tab / 全角冒号与 `[lang]` 前缀、`translate_movie.chars` 改为实际 prompt 字符（与预算同口径）。 |
| 2026-10-25 | 任务层改为**批次模型**：新增 `ai_batches` 表与 `ai_jobs.batch_id/started_at/ended_at/duration_ms`；提交支持范围预设（未翻译 / 无简介 / 上次失败 / 指定筛选）、目标语言与双语、批次级并发与预算；新增 `preview` / 批次明细 / 按批取消 / 重试 / 删除接口；`ai.job_concurrency`、`ai.budget_chars_per_run` 补进默认配置，AI 中心批量任务页重做。 |
| 2026-10-18 | 新增任务层 `ai_jobs`（批量 / 限并发 / 预算 / 取消）与字幕翻译 `translate`（cue 级打包、术语表、双语 SRT、`ai_glossary` 术语表）；新增 `ai.job_concurrency`、`ai.budget_chars_per_run` 配置与 `/api/ai/{jobs,glossary,translate}` 接口；详情页与设置页接入。 |
