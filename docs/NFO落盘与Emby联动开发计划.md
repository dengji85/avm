# 片匣 AVM · NFO 落盘与 Emby 联动 · 开发计划

> 关联 issue：[#8 刮削结果支持双向写回媒体目录](https://github.com/dengji85/avm/issues/8)
> 基线代码：v1.14.0 快照（2026-10-24）
> 文档性质：可执行的开发计划（含任务分解、改动落点、验收与风险），非最终 API 契约

---

## 0. 一句话结论

把「刮削结果落盘到影片目录」做成**默认关闭的兼容层**，配套**回流**设计；分 P0 / P1 / P2 三期落地。P0 交付 issue #8 的原始诉求 + 修复两个既有 bug（`<art>` 空值、评分刻度），并在同一期把「Emby 刷新媒体库」一起做完。

定位声明：**这是防御性功能（获客 + 信任），不是增长功能。**预算上限约为「一个小版本」，不得挤占播放体验的研发资源。

---

## 1. 背景与决策

### 1.1 用户诉求（issue #8）

用户把 AVM 当**元数据中枢**：媒体文件（含 `.strm` 网盘库）放在独立目录（如 `/volume1/docker/strm/av`），希望 AVM 刮削补全后，把 NFO 与封面直接落到视频所在目录，使 Emby / Jellyfin / Infuse 扫描即可识别，**不依赖 AVM 在线，也不依赖外网**。

后续评论追加：后台增加 **Emby 联动配置**（服务器地址 + API Key），刮削落库后**通知 Emby 刷新媒体库**。

### 1.2 产品判断

| 维度 | 结论 |
|---|---|
| 留存影响 | 中性偏负：落盘把「必须回访」的动作变成一次性；但目标用户本来就不在片匣播放池里，不做也留不住 |
| 获客影响 | 正：这是唯一能触达 Emby / Jellyfin 存量用户的入口，且带来的口碑语言易传播 |
| 定位风险 | 真实存在：功能清单往「刮削器」方向长，用户认知会被固化 |
| 数据责任 | 升级：刮削错了是「显示错」，落盘错了是「毁数据」，覆盖策略必须一次做对 |

**决定好坏的三件事**（本计划的设计约束）：

1. 必须有**回流**（P1）：Emby 侧进度回拉、详情页独占信息（女优谱系 / 评分分布 / 同类推荐）。
2. 必须**默认关闭**，放在「媒体库 → 导出」次级位置，不做首页卖点。
3. 必须**不碰播放动线**：不做「点击播放即跳 Emby」的默认深链。

### 1.3 明确不做（本期边界）

- **不承诺 Plex**：Plex 只认自家 agent，需第三方插件（XBMCnfoMoviesImporter）+ legacy agent，收益/成本比极差。
- **不做多套 NFO 模板**：只维护一套 Kodi 基线，参数化命名与覆盖策略。
- **不做 Emby 内嵌完整浏览**（iframe 整站）：P2 才评估。
- **不反向抓取/篡改他人元数据**：只做「补空 + 不动未知节点」的保守合并。

---

## 2. 目标与非目标

**目标**

1. 刮削完成后，NFO + 封面（+ fanart）可自动落到视频同目录，Emby / Jellyfin / Kodi 扫描即识别。
2. 支持「旁挂场景」：片匣不接管文件，仅按配置目录（或已入库文件路径）落盘。
3. NFO 兼容 Kodi 基线；对已存在的 NFO 默认**不覆盖**，支持保守合并。
4. 只读目录 / 无权限 / 网络盘异常时**静默跳过**，不影响刮削主流程，并向用户可见地报告原因。
5. 刮削后按需通知 Emby 刷新媒体库（可关闭、可防抖）。

**非目标**

1. 不做播放器跳转默认化（P1 只是「可选播放引擎」）。
2. 不做 Plex 适配、不做字幕/HDR 元数据回写。
3. 不改动现有刮削逻辑与数据源优先级。
4. 不引入新的外部依赖（`xml.etree` + 标准库足够）。

---

## 3. 现状核查（代码事实）

| # | 事实 | 位置 | 对本计划的影响 |
|---|---|---|---|
| 1 | 封面只落 `COVER_DIR`，`movies.cover` 存**文件名**（非路径/URL） | `app/config.py:29`、`app/images.py:74`、`store.py` UPDATE | 落盘需 `COVER_DIR / cover` 拼绝对路径后复制 |
| 2 | `store.movie_detail` **不返回** `cover_url` / `fanart_url` | `app/store.py:1128` | `build_nfo` 的 `<art>` 恒为空 → **必须修的 bug** |
| 3 | `movies.rating` 是**5 星制**（分档 `>=4` / `>=3` / `1-3`） | `app/store.py:865`、`db.py:36` | Kodi `<rating>` 为 0-10，直接写会**砍半** → 必须换算 |
| 4 | `movies.runtime` 存**分钟** | `store.py:770`（`m.runtime*60.0`） | 与 Kodi 一致，无需换算 |
| 5 | 单部导出写 `<视频名>.nfo`，无策略、无备份、无批量 | `app/api.py:2273-2286` | 需抽出公共模块 + 加策略 + 加批量 |
| 6 | 刮削统一落库入口 `apply_metadata`，被单部/批量/本地 NFO 导入三处调用 | `app/scraper.py:74`，调用点 `:376` `:751` `:524` | 落盘挂钩只此一处即可全覆盖 |
| 7 | 已有 sidecar 查找逻辑（含扩展名前空格容错） | `app/nfo.py:159 find_sidecar` | 落盘目标须与之对齐，避免自己写出读不回 |
| 8 | Docker 默认媒体库 `:ro` 只读挂载 | `README.md:73`、`docker-compose.yml` | 必须 fail-soft + 文档提示改 `rw` |
| 9 | 无任何 Emby / Jellyfin / Plex 集成代码 | 全仓 grep | 刷新能力需从零写（一个 HTTP POST） |
| 10 | 已有「短时令牌媒体出口」范式 | `app/dlna.py:219 make_token`、`/api/cast/media/{token}` | P1 交给外部播放器时复用，勿外发主凭据 |
| 11 | `watch_sessions.method` 已支持 `'external'` | `app/db.py:199-212` | P1 进度回拉无需改表 |
| 12 | 配置无 schema，`DEFAULT_CONFIG` 即契约；凭据需从 `GET /config` 剥除 | `app/config.py:85`、`app/api.py:2815` | 新增配置 3 处改动，别漏密钥剥除 |

---

## 4. 分期与里程碑

| 阶段 | 版本 | 范围 | 预估 | 验收 |
|---|---|---|---|---|
| **P0** | v1.15.0 | NFO + 封面落盘（含合并/跳过/备份）、批量导出、Emby 刷新、修 2 个 bug | 3-4 人日 | 第 9 章清单全绿 |
| **P1** | v1.16.0 | Emby 侧进度回拉、片匣「继续观看」合并展示、播放引擎可选「用 Emby 播放」 | 5-7 人日 | 回拉进度可续播；Emby 看完片匣有记录 |
| **P2** | 待定 | `media_server` adapter（Emby/Jellyfin 双实现）、片匣短链回跳、内嵌转码托管 | 待评估 | — |

拆分原则：P0 独立可发版，不依赖 P1；P1 出问题可整体回退，不影响 P0 能力。

---

## 5. P0 详细设计

### 5.1 配置项（`app/config.py:85 DEFAULT_CONFIG` 新增两节）

```jsonc
"nfo_export": {
  // 总开关。默认关闭：这是「导出」，不是核心功能
  "enabled": false,
  "write_nfo": true,
  "write_cover": true,
  "write_fanart": true,
  // numbered = <视频名>-poster.jpg（默认，同目录多部不冲突）
  // plain    = poster.jpg（文件夹型 / 单文件目录更接近 Kodi 推荐）
  "image_naming": "numbered",
  // match-video = <视频名>.nfo（默认）；movie = movie.nfo（文件夹型）
  "nfo_naming": "match-video",
  // skip = 已有 NFO 不动（默认）；merge = 只补空字段并保留未知节点；overwrite = 覆盖（自动 .bak）
  "on_conflict": "skip",
  "backup": true,
  // 额外落盘目录（旁挂场景：片匣不接管文件时按番号/路径映射写入）
  "extra_dirs": [],
  // 落盘后是否通知 Emby 刷新（需 emby.enabled）
  "notify_emby": true
},
"emby": {
  "enabled": false,
  "base_url": "",        // 如 http://192.168.1.10:8096
  "api_key": "",         // 凭据：必须从 GET /config 剥除
  "user_id": "",         // P1 进度同步用，P0 可留空
  "refresh_mode": "library",  // library = POST /Library/Refresh；item = 逐条刷新
  "refresh_debounce_sec": 30
}
```

同步改动：

- `app/api.py:2815` 的剥除逻辑追加 `cfg["emby"].pop("api_key", None)`（与 `privacy.pin_hash` 同处）。
- 新增 `config.py` 读取便捷函数 `nfo_export_cfg()` / `emby_cfg()`（参照 `:633 roots_to_scan` 风格）。
- 前端保存走既有 `PUT /config`，无需新 schema。

### 5.2 新增模块 `app/nfo_writer.py`

职责单一：**把一部已刮削的影片写成磁盘上的 sidecar**。所有函数**不抛异常**（内部 try/except 转成结果对象），因为它在刮削热路径上，写盘失败绝不能拖垮刮削。

```python
def export_movie(conn, movie_id, cfg=None, force=False) -> dict:
    """主入口。返回 {ok, nfo, poster, fanart, skipped, error}
    skipped 取值：disabled / no_file / read_only / exists_skip / no_cover / strm_target ...
    """

def _target_dirs(movie, cfg) -> list[Path]:
    """落盘目标目录：files[*].path 的父目录 + extra_dirs 映射；去重、去不存在的。"""

def _nfo_path(video_path: Path, cfg) -> Path:
    """match-video → <stem>.nfo；movie → movie.nfo（须与 nfo.find_sidecar 的 bases 一致）"""

def _poster_path(video_path: Path, cfg) -> Path:
    """numbered → <stem>-poster.jpg；plain → poster.jpg"""

def _write_nfo(target: Path, movie, cfg) -> str:
    """skip / merge / overwrite(+.bak)。merge 保留未知节点。"""

def _merge(existing_root: ET.Element, new_root: ET.Element) -> ET.Element:
    """以 existing 为骨架，仅覆盖 new 中非空叶子节点；<art> 逐子节点合并。"""

def _copy_image(src: Path, dst: Path) -> bool: ...

def _writable(folder: Path) -> bool:
    """一次 os.access(W_OK) + 试写临时文件；网络盘加 2s 超时保护。"""
```

关键实现约束：

- **合并而非重写**：`merge` 模式下读入已有 XML，用 `build_nfo_xml()` 生成的节点做「补空」，保留 `<streamdetails>` `<fileinfo>` `<banner>` 等未知节点——这是「通用」与否的分水岭，纯重写会抹掉 Emby/Kodi 写的技术信息。
- **图片复制用「不存在才写」或「内容不同才写」**，避免每次刮削都刷新 mtime 让媒体服务器重复扫描。
- **`.strm` 特殊处理**：目标目录是 strm 文件所在目录（不解析 URL），封面走本地复制，不写远程 URL。
- **`extra_dirs` 映射**：`[{ "from": "D:/AV", "to": "/media" }]`，用于宿主路径 ↔ 容器路径不一致；P0 只做前缀替换，不做智能匹配。

### 5.3 改造 `app/nfo.py`

| 改动 | 说明 |
|---|---|
| 新增 `build_nfo_xml(movie, opts) -> ET.Element` | 返回 Element，供 writer 合并复用；`build_nfo` 改为其字符串包装（现有调用点不变） |
| 修 `<art>` 空值 | 入参支持 `poster` / `fanart` **相对文件名**（如 `ABC-123-poster.jpg`），不再依赖不存在的 `cover_url` |
| **评分刻度** | `rating` × 2 并 clamp 到 0-10；默认写 `<userrating>`（片匣评分是**用户个人评分**，语义上是 user rating），`<rating>` 留空；提供配置 `rating_field`（`userrating` / `rating` / `both`）以便实测后调整 |
| `year` 兜底 | `movie.get("year")` 为空时从 `release_date[:4]` 推导 |
| `premiered` 校验 | 只接受 `YYYY-MM-DD`，否则不写（避免媒体服务器解析失败） |
| `<uniqueid>` | 追加 `<uniqueid type="avm">{code}</uniqueid>`（未知 type 被主流忽略，为 P2 的 item 映射留线索） |
| **禁用 `<id>`** | 明确注释：不得把番号写进 `<id>`（Kodi 库内主键，会造成条目错乱） |
| 保留扩展标签 | `<num>`（番号）、`<label>`（厂牌）、`<maker>`、`<tag>`（AV 刮削圈约定，主流忽略但无害） |

### 5.4 挂钩点

| 位置 | 改动 | 备注 |
|---|---|---|
| `app/scraper.py:74 apply_metadata` 末尾 | `if cfg["nfo_export"]["enabled"] and payload/cover 有变化: nfo_writer.export_movie(...)` | 一处覆盖单部 + 批量 + 本地 NFO 导入；必须包 try/except 并计入 job 统计 |
| `app/api.py:2273 export_nfo` | 改为调用 `nfo_writer.export_movie(force=True)`，保留原响应结构 + 增加 `skipped` / `poster` 字段 | 兼容老前端 |
| 新增批量接口（见 5.5） | 走 `jobs.py`，不复用刮削 job | 大批量可能几十秒，不能同步阻塞请求 |
| 扫描结束（可选） | `scanner.run_scan` 收尾做一次「有封面无 NFO」补写 | 默认不开启，避免首次全库扫描时大量写盘 |

### 5.5 API 设计

| 方法 / 路径 | 用途 | 说明 |
|---|---|---|
| `POST /api/movies/{id}/nfo` | 单部导出（改造） | body 可选 `{force: bool}`；返回 `{ok, path, poster, skipped}` |
| `POST /api/nfo/export` | 批量导出 | body `{ids?: [], filters?: {}, force?: bool}`；空body = 全库已刮削且未导出；返回 job 状态 |
| `GET /api/nfo/export/status` | 批量进度 | 复用 `jobs.py` 轮询风格，与 `/scrape/status` 一致 |
| `POST /api/emby/test` | 测试连接 | 请求 `GET {base}/System/Info`（`X-Emby-Token`），返回版本/服务器名或错误原因 |
| `POST /api/emby/refresh` | 手动刷新媒体库 | body `{mode?: "library"|"item", ids?: []}` |
| `PUT /api/config` | 保存配置（复用） | 无新增 |

新增 `app/jobs.py:129` 同级 `NFO_EXPORT = Job("nfo_export")`。

### 5.6 Emby 联动（P0 仅刷新）

- 刷新调用：`POST {base_url}/Library/Refresh`，头 `X-Emby-Token: {api_key}`；`refresh_mode=item` 时用 `POST {base_url}/Items/{itemId}/Refresh`（P0 无 item 映射，仅 library 模式可用）。
- **防抖**：批量刮削会产生上千次落盘，用一个 30 秒定时器合并成一次刷新（`refresh_debounce_sec`），避免把 Emby 打爆。
- **失败不阻断**：连接失败/401 只记日志 + 在任务结果里带 `emby_error`，前端 toast 提示，刮削结果不回滚。
- **可达性提示**：设置页明确写「地址是 Emby 服务器地址，且需片匣服务端可访问（Docker 场景用容器内可达地址）」。

### 5.7 前端改动

| 文件 | 改动 |
|---|---|
| `web_src/src/views/SettingsView.vue` | 在「媒体资源路径」面板（`:1313-1354`）之后新增「导出到媒体目录」面板：总开关、NFO/封面/fanart 子开关、图片命名、NFO 命名、冲突策略、备份、额外目录（可增删行）、「全库导出」按钮 + 进度 |
| 同文件，新增「Emby 联动」面板 | 地址、API Key（密码框，仅为掩码回填）、测试连接、刷新策略、防抖秒数、「立即刷新媒体库」 |
| `web_src/src/components/DetailDrawer.vue:1043` | 按钮文案改为「导出到媒体目录」，结果 toast 区分 `ok / skipped(原因)`，跳过时说明（如只读挂载） |
| `web_src/src/api.js` | 新增 `exportNfoBatch / exportNfoStatus / embyTest / embyRefresh` |
| `web_src/src/i18n/locales/{zh-CN,zh-TW,en,ja}.js` | 新增文案，4 文件同步（漏一个就会显示 key） |

文案基调：**「导出给 Emby / Jellyfin 用」**，不说「元数据中枢」，强化「数据归你、可带走」。

### 5.8 落盘产物示例

```
/media/av/ABC-123/
├─ ABC-123.mp4
├─ ABC-123.nfo            # nfo_naming=match-video
├─ ABC-123-poster.jpg     # image_naming=numbered
├─ ABC-123-fanart.jpg
└─ ABC-123.nfo.bak        # 仅 on_conflict=overwrite 且 backup=true
```

`.strm` 场景：

```
/media/strm/av/DEF-456/
├─ DEF-456.strm           # 内容为网盘直链
├─ DEF-456.nfo
└─ DEF-456-poster.jpg     # 本地复制，不依赖网盘可访问
```

---

## 6. 兼容规范（实现必须遵守）

| 目标软件 | 本地 NFO | 备注 |
|---|---|---|
| Kodi | 完全支持 | 事实标准；`<视频名>.nfo` / `movie.nfo` 均可 |
| Jellyfin | 支持（读 Kodi 格式） | 命名规则最严，作为主验证目标 |
| Emby | 支持（读 Kodi 格式） | 与 Jellyfin 同源，差异小 |
| Infuse | 部分支持 | 主要靠文件名 + TMDB，**必须实测后再决定是否写入 README** |
| Plex | 不支持 | 需第三方插件，不承诺 |

七条硬规则：

1. 评分换算 0-5 → 0-10（默认写 `<userrating>`，见 5.3）。
2. 图片默认 `<视频名>-poster.jpg`，禁止默认 `poster.jpg`（同目录多部互相覆盖）。
3. `<art>` 写**相对文件名**，禁止绝对路径 / `file://` / 远程 URL。
4. 禁止把番号写进 `<id>`。
5. `<uniqueid>` 只写 `avm` 自定义 type（或将来接到的 tmdb/imdb）。
6. 日期 `YYYY-MM-DD`，非法则不写；`runtime` 单位分钟（当前数据一致）。
7. UTF-8 无 BOM（`ElementTree` + `minidom` 已满足），特殊字符由 XML 库转义。

---

## 7. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| `media:ro` 只读挂载（Docker 默认） | 全部跳过，用户以为功能坏了 | fail-soft + 设置页/README 明确提示需 `rw`；结果里返回 `read_only` 原因 |
| 宿主机路径 ≠ 容器路径 | 写错目录或写不进 | `extra_dirs` 前缀映射；落盘前校验路径存在 |
| 同目录多部影片 | `poster.jpg` 互相覆盖 | 默认 `numbered` 命名 |
| NFO 被覆盖，丢失 Emby/Kodi 写的技术信息 | **最伤的信任事故** | 默认 `skip`；`merge` 保留未知节点；`overwrite` 强制 `.bak` |
| 网络盘 / 掉线盘写入卡死 | 刮削线程挂住 | 写盘加超时（2s 探测 + 复制分块），异常即跳过并记录 |
| 批量落盘拖慢刮削 | 用户体感变慢 | 落盘失败不计入失败数；批量导出独立 job；可关闭自动落盘只留手动 |
| 中文 / 特殊字符文件名 | 部分文件系统乱码 | 统一 `Path` + `os.fsencode` 交给系统，不自造编码 |
| Emby 被打爆（刷新风暴） | Emby 卡顿、用户投诉 | 防抖 30s + 单次刷新 + 开关可关 |
| 版本漂移（Emby 4.7 / 4.8、Jellyfin） | 刷新接口/路由变化 | P0 只用最稳定的 `/Library/Refresh`；P2 抽 adapter |
| 定位漂移 | 用户认知固化为「刮削器」 | 默认关闭 + 次级入口 + README 不列为卖点 + P1 强制回流 |

---

## 8. 测试矩阵

| # | 场景 | 期望 |
|---|---|---|
| 1 | 单文件 + 同目录多部 | 各自 `<name>.nfo` / `<name>-poster.jpg`，互不覆盖 |
| 2 | 多分片 `-cd1/-cd2` | 不生成互相冲突的图片；NFO 落在主文件旁 |
| 3 | 文件夹型（内含 `movie.nfo`） | `nfo_naming=movie` 时识别并合并，不产生第二份 |
| 4 | `.strm` 网络串流 | NFO + 本地封面正常，不因目标不可达失败 |
| 5 | 只读挂载 | 跳过并返回 `read_only`，刮削整体成功 |
| 6 | 中文 / `&` / 引号 / 超长名 | XML 正确转义，文件名合法 |
| 7 | 已有 Kodi 写的 NFO（含 `<streamdetails>`） | `skip` 不动；`merge` 保留 `<streamdetails>` |
| 8 | 已有 Emby 写的 NFO | 同上，且不破坏 Emby 的 `<UniqueId>` |
| 9 | 损坏 / 非 XML 的 `.nfo` | `merge` 降级为 `.bak` + 重写，不抛错 |
| 10 | 无封面影片 | 只落 NFO，`skipped=no_cover` 可解释 |
| 11 | `rating` = 0 / 3.5 / 5 | Kodi/Emby 显示 0 / 7 / 10（或 `userrating` 对应值） |
| 12 | `release_date` 只有年份或为空 | 不写非法 `premiered`，`year` 有兜底 |
| 13 | Emby 地址错误 / Key 失效 | 落盘成功，刷新失败仅提示，不回滚 |
| 14 | 批量导出 1000 部 | job 进度正常，可中断，重复执行幂等 |

验证环境：Kodi（必测）、Jellyfin（主验证目标）、Emby（必测）、Infuse（选测，结果写进文档而不是承诺）。

---

## 9. P0 验收清单

- [ ] `nfo_export.enabled=false` 时全链路零行为变化（回归：现有导出按钮、刮削、扫描）
- [ ] 开启后单部刮削 → 目录内出现 `<name>.nfo` + `<name>-poster.jpg`，Emby 扫描能识别封面与女优
- [ ] `rating` 刻度正确（5 星 → 10 分制），`<art>` 不再为空
- [ ] `on_conflict=skip` 时已有 NFO 一个字节都不变
- [ ] `on_conflict=overwrite` 时生成 `.bak`，内容可人工恢复
- [ ] 只读目录不产生异常堆栈，任务结果里能看到跳过原因
- [ ] 批量导出可中断、可重复执行、结果幂等
- [ ] Emby 测试连接成功/失败都有明确提示；刷新有防抖
- [ ] 4 个 i18n 文件无缺 key；设置页在移动端不破版
- [ ] README / `docs/Docker部署.md` 补充「开启落盘需把媒体库挂载改为 `rw`」

---

## 10. 上线后指标（决定 P1 是否需要加速）

| 指标 | 观察窗口 | 判读 |
|---|---|---|
| 写回落盘开启率 | 2 周 | 低于 5% → 需求是伪需求，停止继续投入 |
| 开启落盘用户的 7 日留存 | 2 周 | 与未开启用户对比，明显更低 → 确认「工具化」，P1 回流必须提前 |
| 开启落盘用户的首周播放次数 | 2 周 | 趋零 → 认知已固化，应停止加刮削功能，转向回流 |
| 「数据被改坏」类 issue | 持续 | **必须为 0**，出现即视为 P0 缺陷，优先修 |

---

## 11. P1 / P2 方向（本计划不展开）

**P1（回流，决定产品是否被工具化）**

1. Emby 进度回拉：`GET /Users/{uid}/Items/{iid}/UserData` → 写 `watch_progress` / `watch_sessions(method='external')`；冲突按「更新时间较晚者胜」。
2. `movie ↔ emby item` 映射表：`GET /Items?Recursive=true&IncludeItemTypes=Movie&Fields=Path` 拉全量建 path→id 映射，落库缓存，番号兜底匹配。
3. 片匣播放按钮增加「用 Emby 播放」选项（默认仍走片匣）；播完进度回写片匣。
4. 详情页补齐独占信息（女优谱系、评分分布、同类推荐），让「从 Emby 回来」有理由。

**P2（生态与体验）**

1. `media_server` adapter：Emby / Jellyfin 双实现（两者 API 兼容度约 90%，但不要把 Emby 写死）。
2. 片匣短链回跳：复用 `?c=<一次性 code>`（`app/main.py:189`），在 Emby 侧描述里放链接一键回片匣详情。
3. 内嵌转码托管：用 Emby HLS 流喂现有 video.js（解决 HEVC / 10bit 本机播不了的问题），复用 `dlna.make_token` 的短时令牌思路。

---

## 12. 待定问题（开发前需确认）

1. **`rating` 字段落点**：`<userrating>`（语义正确，但需实测 Emby 是否展示）vs `<rating>`（各端都展示，但会污染「官方评分」语义）。当前默认 `userrating`，实测后定。
2. **是否提供 `extra_dirs` 映射的首版实现**：旁挂用户可能不需要映射（容器内路径已统一），先做前缀替换，不做智能匹配。
3. **自动落盘 vs 只做手动批量导出**：issue 反馈要「自动」。方案是自动但默认关闭，并在设置页提供「一键补齐全库」。
4. **`merge` 的实现深度**：只合并叶子节点（简单、够用）vs 完整树合并（复杂）。先做叶子级。
5. **是否需要「落盘后校验」**（读回 NFO 确认可解析）：建议做，成本低，能防写坏。

---

## 附：本次要修的既有 bug 清单

| bug | 位置 | 修复 |
|---|---|---|
| 导出 NFO 的 `<art>` 恒为空 | `app/nfo.py:216-220` + `store.py:1128` | writer 传入相对文件名，不再依赖 `cover_url` |
| 评分刻度 5→10 未换算 | `app/nfo.py:201` | ×2 clamp，默认写 `<userrating>` |
| 单部导出无覆盖策略、无备份 | `app/api.py:2273-2286` | 统一走 `nfo_writer.export_movie` |
