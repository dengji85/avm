# 更新日志

## [1.8.0] - 2026-08-01

### 前端重构（原生 JS → Vue 3 组件化）

* 前端从单文件 `web/app.js`（约 2000 行）重构为 **Vue 3 + Vite 4** 单页应用，工程位于 `web_src/`，构建产物输出到 `web_dist/`。
* 按功能域拆分（每个组件 100–350 行，易维护）：
  * `App.vue`：根装配，含标签切换、详情抽屉、侧栏、任务进度条、全局快捷键（/ 搜索、r 随机、F 收藏、Esc 关闭）、跨视图 `avm-jump`/`avm-open` 事件。
  * 布局：`TopNav.vue`（搜索/扫描/抓取/标签）、`Sidebar.vue`（标记 chips + 分面筛选）。
  * 画廊：`GalleryView.vue` + `MovieGrid.vue` + `MovieCard.vue` + `Pager.vue` + `BulkBar.vue`（批量评分/已看/类型/女优/标签）。
  * 详情：`DetailDrawer.vue`（富资料、文件、观看进度、预览图墙、系统内播放会话）。
  * 女优：`ActressView.vue`（女优墙 + 详情 + 关注 + 代表作）。
  * 片单：`CollectionsView.vue`（手动 + 智能清单创建面板）。
  * 新功能视图：`RankingsView.vue`（观看时长/评分/收藏/播放榜）、`SwipeView.vue`（滑动评分，键盘 ←/→/F）。
  * 存储：`StorageView.vue`（体检 + 去重 + 磁盘分布）。
  * 统计：`StatsView.vue`（标签云 / 按年时长图 / 日历热力）。
  * 设置：`SettingsView.vue`（媒体目录、数据源、ffmpeg 路径、文件名解析预览、目录浏览）。
* 公共模块：`api.js`（统一请求封装）、`state.js`（全局响应式状态）、`utils.js`（格式化/DOM 辅助/toast）、`composables/useLibrary.js`（影片加载/筛选）。
* 开发：`npm run dev`（Vite 代理 `/api`、`/covers` 到后端）；生产：`npm run build` → `web_dist/`。
* 后端 `main.py` 静态托管改为 `web_dist`，`/assets` 挂载构建产物；`build.py` 与 `AV博物馆.spec` 打包 `web_dist`。

### 新增（8 项体验增强）

* **预览图墙**：详情页新增「预览图墙」生成/刷新。后端用 `ffmpeg` 在影片中等距抽帧存到 `covers/preview/<movie_id>/`，前端以缩略图墙展示；无 `ffmpeg` 时优雅提示「请在设置中配置 ffmpeg 路径」。设置页新增 `ffmpeg_path` 配置项。
* **滑动评分**：顶栏新增「滑动评分」标签页。按排序（最近添加 / 评分升序 / 未看 / 随机）拉出一批影片，左滑「跳过」、右滑「想看（加入片单）」、按 F「收藏」，支持键盘 ←/→/F，逐张快速筛选。
* **关注女优**：女优墙与女优详情支持「关注/取关」（`actresses.followed`）。女优墙新增「仅关注」筛选，可按关注排序。
* **排行榜**：新增「排行榜」标签页，按「观看时长 / 评分 / 收藏 / 播放次数」排序展示 Top 影片，点击进详情。
* **批量编辑扩展**：批量操作栏新增「评分 / 已看 / 未看 / 类型 / 女优」批量写入（类型与女优以并集方式追加或替换）。
* **文件体检**：存储页新增「开始体检」，扫描「缺失文件 / 缺封面 / 未识别番号 / 疑似重复（同大小）/ 分片不完整」五类问题并汇总计数，可逐项点开。
* **统计可视化增强**：统计页新增「标签云」「时长分布（按年）柱状图」，原有观看日历热力图基于真实观看时段。
* **智能清单**：片单页新增「智能清单」创建面板，按规则（未看 / 评分下限 / 类型 / 女优 / 排序）自动聚合影片，规则驱动、随库自动更新；智能清单不可手动增删影片。

### 工程

* `app/db.py`：`_migrate` 增加 `actresses.followed`、`collections.kind`/`rule`、`movie_previews` 表。
* `app/store.py`：新增 `toggle_follow` / `rankings` / `health_check` / `smart_query` / `stats_enhanced` / `generate_previews` / `get_previews` / `set_previews`；扩展 `actress_wall`、`batch_update`、`create_collection`、`collection_movies`、`_build_where`（支持顶层 flag 与评分区间）。
* `app/api.py`：新增 `POST /actresses/{id}/follow`、`GET /rankings`、`GET /stats-enhanced`、`POST /collections`（kind/rule）、`GET /health-check`、`GET /movies/{id}/previews`、`GET /covers/preview/{movie_id}/{fname}`。
* `web/`：新增排行榜 / 滑动评分视图、关注筛选、批量编辑栏、体检面板、统计可视化与智能清单 UI；设置页增加 ffmpeg 路径配置。

## [1.7.1] - 2026-08-01

### 改进（系统播放器也能记录详细历史）

* **A 档·通用监控（零配置）**：点「系统播放」后，后端启动后台守护线程，周期性探测「目标文件是否仍被播放器占用」（Windows 用 `ctypes` 以独占方式试探打开，命中共享冲突即判为「在播」）。把「文件被占用」的时间段合并为观看区间，播放器关闭后落库到 `watch_sessions`。
  * 因此**任何系统播放器（VLC / PotPlayer / MPC / mpv …）**都能自动记录「看了多久」+「哪几段时间段」，无需配置、不依赖播放器内部接口，并彻底绕开内置播放器对 MKV / H.265 解码差的问题。
  * 点击「系统播放」不再建空壳前端场次，改由后端监控线程统一负责起止与落库；误点（未真正打开文件）的空场次会被自动丢弃。
  * 说明：分段精度为「文件被占用的区间」（≈秒级），能区分多次打开 / 关闭的各次观看，但同一次连续播放内的拖拽不细分。

### 工程

* 新增 `app/monitor.py`：文件占用探测、`_merge` 区间合并、后台落库线程 `start_external_monitor`。
* `app/api.py`：`play_movie` 在「系统播放」时建 `watch_sessions` 场次并启动监控；`_open_path` 返回子进程 PID（非 Windows 用作存活判定）。
* `web/app.js`：系统播放去掉前端空壳 session，改为调用后端后刷新历史区。

## [1.7.0] - 2026-08-01

### 新增（观看历史 / 真实分段 / 偏好分析）

* **会话级观看历史**：新增 `watch_sessions` 表，每次「系统播放」或「内置播放」都记一场（开始/结束时间、起始/结束位置、观看秒数、是否看完）。详情页「观看历史」区展示每片场次、累计时长与覆盖率。
* **A 档·真实分段（内置播放器）**：详情页新增「🎬 内置播放」，用浏览器 `<video>` 流式播放（`/stream/{id}`，支持 Range 断点续传）并 hook `play/pause/seek/ended` 记录观看区间 `[start,end]`，实时上报，关闭/看完时落库。覆盖率地图直观显示「看了哪几段」。
  * 浏览器无法解码的格式（多为 MKV / H.265）会提示改用「系统播放」，并优雅降级（仍记场次）。
* **B 档·偏好分析「我的观影」页**：顶栏新增「我的观影」标签，基于**真实观看时长**聚合——
  * 总观看时长 / 总场次 / 看过影片 / 活跃天数 / 日均场次 / 场均时长。
  * 偏好画像：类型 / 女优 / 厂商 / 系列 / 导演，按观看秒数加权（比收藏更反映真实口味）。
  * 活跃时段热力图、观看最多的影片、最近观看流（均可点开详情）。

### 工程

* `app/db.py`：`watch_sessions` 入 SCHEMA 与 `_migrate`。
* `app/store.py`：新增 `start_session` / `update_session` / `end_session` / `movie_sessions` / `movie_primary_file` / `watch_analytics`。
* `app/api.py`：新增 `POST /movies/{id}/session/start`、`…/update`、`…/end`、`GET /movies/{id}/sessions`、`GET /watch-analytics`、`GET /stream/{id}`。
* `web/`：新增「我的观影」视图、详情历史区、内置播放器弹窗与分段捕获逻辑。

## [1.6.0] - 2026-08-01

### 新增（女优 Rich 档案）

* **女优 Rich 档案**：女优详情页升级为数据化人物页。
  * 头部展示头像、收藏爱心、资料编辑（别名 / 生日 / 备注）。
  * 统计卡片：作品数、活跃年份（首演–最新）、总时长、平均评分、总体积、已看 / 收藏。
  * 分布条形图：类型分布、厂商分布、系列分布（点击分布条直接跳转对应筛选）。
  * 代表作：按评分取高分 Top4 卡片。
  * 保留「常合作女优」跳转云。

### 工程

* `app/store.py`：新增 `actress_stats` 聚合（数值指标 + 类型 / 厂商 / 系列分布 + 代表作），挂到 `actress_detail`。
* `web/`：重写 `renderActressDetail` 为 Rich 档案版式，新增 `editActress` 资料编辑与 `#actress-rich` 容器。

## [1.5.0] - 2026-08-01

### 新增（发现与策展）

* **续看进度**：详情页可记录观看进度（百分比），首页出现「继续观看」货架，带进度条一键续看；可一键清空进度。
* **相似推荐（猜你喜欢）**：详情页按「共演女优(权重10) / 同类型(4) / 同厂商(3) / 同系列(3) / 同导演(2)」加权打分推荐相似作品。
* **自建片单**：新增「片单」标签页，可新建命名片单（如「年度十佳」「周末片单」），详情页「＋ 加入片单」归档，片单内可移出、重命名、删除、CSV 导出；卡片显示日期/体积（修复历史显示问题）。

### 工程

* `app/db.py`：新增 `watch_progress`、`collections`、`collection_items` 三张表（含旧库升级迁移）。
* `app/store.py`：新增相似度、续看、合集的查询与聚合。
* `app/api.py`：新增 `/movies/{id}/similar`、`/movies/{id}/progress`、`/continue-watching`、`/collections` 系列接口。
* `web/`：详情抽屉相似推荐、续看货架、片单抽屉与「加入片单」弹窗。

## [1.4.0] - 2026-08-01

### 新增（爱好者向「逛片」体验）

* **作品评分**：详情抽屉内 1–5 星评分，写入数据库；卡片显示 `★N` 徽标，支持按「评分最高」排序与「已评分」筛选。
* **多人共演优化**：多选女优默认改为 **OR（任一满足）**，避免单人 solo 片导致「选多个即空」；侧栏可切回 **AND（同时出现）** 做收敛。
* **多人优先排序 + 仅多人筛选**：排序新增 `多人优先`（共演女优数多的置顶）；筛选新增「仅多人」（女优数 ≥ 2）。
* **女优合作网**：女优详情页列出最常合作的 Top12 女优，点击直接跳转 OR 筛选。
* **快速筛选条**：工具栏下方新增 `随机一部 / 只看收藏 / 未看 / 想看清单 / 多人共演`，带数量角标，点击即筛选。
* **键盘流**：`/` 搜索、`r` 随机、`←/→` 翻看、`f` 收藏、`Esc` 关闭/清空。
* **网格密度切换**：工具栏「密度」按钮在宽松 / 紧凑间切换。

### 修复

* 多选女优筛选返回 0 条（原默认 AND 逻辑对 solo 片不友好）→ 默认改为 OR。
* 女优详情页作品卡片点击无反应（事件委托选择器误用 `data-card`）→ 改为 `.card` / `dataset.id`。

### 工程

* `app/store.py`：新增评分、多人、合作网相关聚合查询与排序。
* `app/config.py`：支持 PyInstaller 冻结环境路径（`_MEIPASS` / `exe` 同级 `data/`）。
* 新增 `build.py` 一键打包为 `dist/AV博物馆/AV博物馆.exe`（单文件夹，双击即运行）。

## [1.3.0] - 早前

* 女优墙、统计页、NFO/CSV 导出、观看记录、收藏。
* 元数据源可插拔（`local_nfo` / `http_json` / `http_html`）。

## [1.0.0] - 初始版本

* 媒体库扫描、番号识别与文件名清洗、分片归并、标记识别。
* 封面嗅探 / 下载 / 占位图。
* 关键字 + 女优/类型/厂商/系列/年份 + 快捷标记检索。
* FastAPI + 原生前端（HTML/CSS/JS，无需构建）。
