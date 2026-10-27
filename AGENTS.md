# AGENTS.md

> 本文件是**项目约定**，对人和 AI 编码助手同时生效。
> 目标：让任何一次改动都「落在对的位置、用对的方式、按同一套标准验收」。
> 约定变了就改这里，别让下一个人（或下一次会话）靠猜。

---

## 0. 一句话

片匣 AVM：本地的 AV 影片库管理工具。Python 3.11 + FastAPI + SQLite 后端，Vue3 + Vite（**无 UI 框架**，自写 CSS）前端；一条命令启动，也可打包成 exe / Docker。

---

## 1. 快速命令

| 目的 | 命令 |
|---|---|
| 开发后端（8770，热重载） | `python run.py serve --reload --no-browser` |
| 开发前端（5173，代理 /api → 8770） | `cd web_src && npm run dev` |
| 构建前端到 `web_dist/` | `cd web_src && npm run build` |
| 语法检查 | `python -m compileall -q app` |
| 干跑导入（能抓出大部分低级错误） | `python -c "from app.main import app; print(len(app.openapi()['paths']))"` |
| CLI 子命令 | `python run.py scan` / `dedupe` / `stats` / `export` / `organize` / `mcp` / `ai-check` / `web-check` |

**没有单元测试框架**。验收靠下面第 6 节的四道闸，别假装有测试覆盖。

开发环境的访问令牌在 `data/access_token.txt`；调 API 时带 `X-Access-Token`。

---

## 2. 仓库地图

```
app/                后端（扁平模块，职责见下）
  api.py            路由薄壳（本该只做参数校验 + 调用；正在按域外迁）
  routers/          按领域拆出的子路由（covers / ext …，统一挂在 /api 下）
  store.py          SQL 与数据装配（最大的单体，改动需谨慎）
  db.py             建表 + 迁移（PRAGMA table_info → ALTER TABLE）
  config.py         默认配置（deep merge，新键写这里）
  images.py         封面 / 竖版海报 / 头像 / fanart
  asr.py            faster-whisper 对白提取（后台任务范本）
  translate.py      字幕翻译（批次、术语表、进度回调）
  ai_*.py           AI 能力（core / library / extra / jobs / prompts）
  agent.py          助手 agent（工具调用）
  scraper.py        刮削编排；providers/ 各站点解析
  subtitles.py      外挂字幕登记与匹配
  threadpool.py     线程池（长 / 短分离）
  poster_job.py     批量海报任务（内存态）
  translate_job.py  单部翻译任务（内存态）
  ext_components.py 扩展组件（ffmpeg / asr 依赖与模型）
web_src/src/        前端
  views/            页面级组件
  components/       可复用组件（DetailDrawer 是最大的一个）
  composables/      useLibrary / useAiAssistant / aiWorkspace 等
  api.js            所有后端调用的唯一出口
  state.js          全局状态
  styles/           layout / ui / assistant 三份 + 组件内 scoped
  i18n/locales/     zh-CN / zh-TW / ja / en（四份必须同步）
docs/               使用手册 / 开发文档 / 打包指南 / Docker / AI 能力 / 安全 等
tools/              可选二进制与脚本（脚本入库，产物忽略）
```

---

## 3. 新增代码放哪里（决策表）

| 你要加的东西 | 放哪 | 不要做什么 |
|---|---|---|
| 新的 HTTP 接口（已有领域） | 对应 `app/routers/<域>.py`；没有该域才新建 | 别再往 `api.py` 堆长逻辑，只留薄壳 |
| 新的**长时间**任务（分钟级） | **新建 `app/xxx_job.py`**，照 `poster_job.py` / `translate_job.py` | 不要同步阻塞请求；不要塞进别的 job 模块 |
| 秒级 / IO 任务 | 直接 `submit_task()` | 不要为它建模块 |
| 给已有能力加参数/分支 | **原地改**（如 `translate_cues(progress=...)`） | 不要为"加一个参数"新建模块 |
| 新配置项 | `config.py` 默认值 + 使用处 `.get()` 兜底 + 前端设置项 + 四语言文案 | 不要在代码里散落魔法默认值 |
| 新数据库列 | `db.py` 的迁移段（`PRAGMA table_info` 判断 → `ALTER TABLE`） | 不要改动已有列定义；不要写破坏性迁移 |
| 新 UI 文案 | 四份 locale 同时加，键名语义化 | **只加中文**（会显示成 key） |
| 新刮削源 | `app/providers/` 下新文件 | 不要把站点逻辑写进 `scraper.py` |
| 新文档 | `docs/`，并同步 `README` 或本文件的索引 | 不要在根目录散放临时说明 |

**模块边界判断标准**：出现了**新的职责**才建模块；只是给现有职责加分支就在原处改。两个模块抢同一件事比"文件少"更糟。

---

## 4. 后端约定

### 4.1 路由
- 前缀统一 `/api`；`api.py` 里 `router = APIRouter(prefix="/api")`，子路由 `include_router` 即可。
- 新路由文件用 `from .. import xxx`（**两级**），`from .` 会指向 `app.routers` 而报 ImportError。
- 业务错误用 `HTTPException(400/404, "中文可读原因")`；上游失败用 502。前端直接展示 `detail`。

### 4.2 长任务标准写法
```python
_JOB = {"running": False, "total": 0, "done": 0, "percent": 0,
        "message": "", "cancel": False, ...}   # 状态契约，前端直接轮询
def start(...): submit_long(_run, ...)          # 长任务一律 submit_long
def state(): ...                                 # 返回不可变副本
def cancel(): _JOB["cancel"] = True              # 取消=置标志，循环内检查
```
- 长任务必须**可取消**、**有百分比**、**重复点击返回同一任务**（不要排队两次）。
- 阻塞型长任务（ASR 模型）用锁串行，不要靠线程数硬顶。

### 4.3 线程池
- `submit_long()`：2 线程，长任务（对白提取、翻译、批量海报、预览抽帧、批量抓取）。
- `submit_task()`：4 线程，短任务（下载、探测、单次抽帧）。
- **新增分钟级任务若用短池 = bug**：会饿死交互请求。

### 4.4 外部依赖
- ffmpeg 一律走 `ext_components.ffmpeg_exe(cfg)` / `ffprobe_exe(cfg)`，别直接写 `"ffmpeg"`。
- 网络请求失败要能降级（返回 None / 跳过），不要让单点失败炸整批。

### 4.5 数据与文件
- 运行时数据全部在 `data/`（不入库）；用户可配置目录（模型 / 头像 / fanart）存**相对名或绝对路径**，DB 只存相对名。
- 生成物命名有约定（如海报 `<封面名>_poster.jpg` + 同名 `.json` 元信息），改名前先搜引用。

---

## 5. 前端约定

- 所有 HTTP 调用走 `api.js`，组件里**不要**直接 `fetch`。
- 跨组件共享状态放 `state.js` 或 composable；临时 UI 状态留组件内。
- 样式：全局只有 `styles/{layout,ui,assistant}.css`，其余写在组件 `<style scoped>`。
- 颜色/间距用 CSS 变量（`--c-*` / `--sp-*`），不要硬编码色值。
- 图片一律 `object-fit: cover` + 容器固定比例；横图进竖位时给 `object-position` 或用竖版海报接口。
- 长任务的进度用轮询（1s），不要用长连接硬等。

---

## 6. 验收四道闸（改完必跑）

1. **语法**：`python -m compileall -q app`（前端则 `npm run build`）
2. **干跑导入**：`python -c "from app.main import app"` —— 能抓住"删了还在用的导入""相对层级错"这类致命错
3. **重启 + 冒烟**：重启后端，用脚本打真实接口（`data/access_token.txt` 里的令牌）
4. **前端构建 + 目视**：`npm run build` 无警告错误，浏览器里点一遍改动路径

**路由重构额外一步**：改前导出全部路由（方法+路径），改后用 `app.openapi()['paths']` 逐条比对，必须 0 缺失。
**改数据库**：迁移要幂等（重复执行不报错）。

---

## 7. 提交与发布

- **过程文件不入库**：临时脚本（`_*.py` / `*.mjs`）、日志（`*.log`）、`data/`、`dist/`、`*.spec` 均被 ignore；提交前 `git status --short` 自查。
- **提交信息**：`feat:` / `fix:` / `chore:` / `docs:` + 中文摘要，正文用 `-` 列要点（参见 `git log`）。
- **CHANGELOG**：每条改动写进 `## [版本] → ### 改进 / 修复` 顶部，面向用户描述效果而非实现。
- **版本号单一真源**：`app/__init__.__version__`；打 tag 触发 Release（产物 + SHA256 + ASR 组件 zip）。
- 未经要求不提交、不打 tag；`main` 不做 force push。

---

## 8. 已知坑（都真实踩过）

| 坑 | 避法 |
|---|---|
| 子路由里 `from . import x` 报 ImportError | 用 `from ..` |
| 清理"未使用导入"误删仍在用的名字 | 删前全仓搜该标识符；删后必跑干跑导入 |
| PowerShell 内联 `python -c` 引号地狱 | **写临时 `.py` 文件**再执行，跑完删除 |
| 直接改 `.md` 因 CRLF/BOM 匹配失败 | 用 Python 脚本按行插入，保留原行尾 |
| i18n 只加一种语言 | 四份 locale 一次加齐（zh-CN / zh-TW / ja / en） |
| 同步请求跑分钟级任务 | 会被浏览器 / 反代 60s 判超时，改后台任务 |
| 海报 / 缩略图被拉成糊图 | 只缩小不放大；优先官方竖版源 |
| `--reload` 子进程杀不干净占端口 | 按端口找 `OwningProcess` 杀，再启动 |
| 前端 `npm` 直接 `Start-Process` 失败 | 用 `cmd /c npm run dev` |

---

## 9. 文档索引

| 文档 | 内容 |
|---|---|
| `docs/使用手册.md` | 面向用户的功能说明 |
| `docs/开发文档.md` | 架构、模块、构建细节 |
| `docs/扩展组件手册.md` | ffmpeg / ASR 依赖与模型 |
| `docs/AI能力设计.md` | AI 能力与提示词设计 |
| `docs/安全与隐私设计.md` | 令牌、权限、隐私边界 |
| `docs/打包指南.md` / `Docker部署.md` / `升级指南.md` | 发布相关 |

---

## 10. 安全底线

- 不打印、不提交、不回显 API Key / 访问令牌；`GET /config` 回传时密钥必须掩码。
- 外站抓取默认校验证书（`server.insecure_skip_verify` 关闭时）；不要在代码里硬编码 `verify=False`。
- 删除类操作（影片、字幕、标签、文件）必须有确认，且不可逆操作要在文案里说清。
- 不要为了"跑通"而放宽鉴权中间件。
