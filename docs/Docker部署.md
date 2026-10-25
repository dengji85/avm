# 片匣 · Docker 部署指南

面向 NAS（群晖 / 威联通 / Unraid / TrueNAS）与 Linux 服务器的容器化部署。

## 0. 设计要点

* **多阶段构建**：前端在 `node` 阶段构建，最终镜像只含运行期产物，无需提交 `web_dist`。
* **非 root 运行**：支持 `PUID` / `PGID` 对齐宿主机文件属主，避免 NAS 上卷权限问题。
* **`tini` 作为 init**：正确处理信号与僵尸进程，`docker stop` 能优雅退出。
* **开箱可用**：默认内置 `ffmpeg`（抽帧 / 预览）与 `chromium`（av-wiki 的 CDP 抓取）。
* **能力降级**：容器内没有桌面环境，前端会自动隐藏「用系统播放器」「在文件夹中显示」入口。

## 1. 快速开始（使用预构建镜像，推荐）

镜像由 GitHub Actions 自动构建并推送到 GHCR（`linux/amd64` + `linux/arm64`），NAS 上**无需编译**：

```bash
# 1) 准备目录
mkdir -p ./data

# 2) 编辑 docker-compose.yml，把媒体库路径改成你的实际目录：
#    - /path/to/your/media:/media:ro
#    compose 默认 image 已是 ghcr.io/dengji85/avm:latest

# 3) 拉取并启动
docker compose pull
docker compose up -d

# 4) 取得访问令牌（完整值只写在容器内的文件里，日志只打印掩码）
docker compose exec avm cat /data/access_token.txt
```

指定版本：

```bash
docker pull ghcr.io/dengji85/avm:1.15.0
```

> 首次使用需要把 GHCR 上的镜像包设为 **Public**（GitHub → 该仓库 → Packages → 对应镜像 → Package settings → Change visibility），
> 否则拉取时会要求登录。

### 本地构建（可选，无需 CI）

```bash
docker compose up -d --build
# 或
docker build -t avm:latest .
```

浏览器打开 `http://<NAS-IP>:8770`，把上面的令牌粘贴进去即可（也可用
`http://<NAS-IP>:8770/?token=<令牌>` 直达）。登录后在「设置 → 访问」里有二维码，
手机扫码直接登录（一次性短链，5 分钟有效，不携带长期令牌）。

## 2. 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TZ` | `Asia/Shanghai` | 时区。库内时间为本地时间，`UTC` 会导致时间显示偏差 |
| `PUID` | `1000` | 运行用户 UID，需与媒体库文件属主一致 |
| `PGID` | `1000` | 运行用户 GID |
| `UMASK` | `022` | 新建文件权限掩码 |
| `AVM_DATA_DIR` | `/data` | 数据目录（配置 / 数据库 / 封面 / 头像 / 预览） |
| `AVM_HOST` | `0.0.0.0` | 监听地址（镜像已设，一般不用改） |
| `AVM_PORT` | `8770` | 监听端口 |
| `AVM_ACCESS_TOKEN` | 空 | 预设访问令牌；留空则自动生成并打印在日志 |
| `AVM_PUBLIC_URL` | 空 | 对外访问地址，如 `http://192.168.1.10:8770`。容器内只能看到容器网段地址，填了它「设置 → 访问」才会显示手机能连上的地址（也可写进配置 `server.public_url`） |
| `AVM_CONTAINER` | `1` | 标记容器环境，前端据此隐藏桌面相关入口 |
| `AVM_CHROME_ARGS` | `--no-sandbox --disable-dev-shm-usage` | 传给内置 Chromium 的附加参数 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 空 | 刮削 / 下载走代理时填写。**容器不会继承宿主机的代理设置**，必须显式声明；推荐同时设 `NO_PROXY=localhost,127.0.0.1`，避免容器内部请求也被绕进代理 |
| `NO_PROXY` | 空 | 不走代理的地址列表（建议至少包含 `localhost,127.0.0.1`） |

> **刮削走代理**：若 NAS 访问外网需要代理，请在 compose 里取消注释 `HTTP_PROXY` / `HTTPS_PROXY`，
> 例如企业内网或大陆网络环境（javdb / javbus 等站点常需代理）。修改后 `docker compose up -d` 重建容器生效。

## 3. 卷

| 容器路径 | 用途 | 建议 |
| --- | --- | --- |
| `/data` | 配置、数据库、封面、头像、预览图 | **必须持久化**，升级时保留 |
| `/media` | 媒体库 | 按实际路径挂载，建议 `:ro` 只读；要用「导出到媒体目录」时改为 `rw` |

> 「导出到媒体目录」（设置 → 媒体库）会把 NFO 与封面写到影片所在目录，供 Emby / Jellyfin / Kodi 识别。
> 该功能需要媒体库**可写**：挂载保持 `:ro` 时导出会被自动跳过（任务里会提示目录不可写），不会报错也不影响扫描与播放。

> 媒体库路径要在「设置 → 媒体库 → 目录」中填写**容器内路径**（如 `/media`），不是宿主机路径。

## 4. 添加媒体库

1. 进「设置 → 媒体库」，把路径填成 `/media`（或 `/media/子目录`）；
2. 点「保存设置」后再点「扫描媒体库」；
3. 扫描是增量且幂等的，容器重启后可继续。

## 5. NAS 平台提示

**群晖 DSM（Container Manager）**
* 用「项目」导入本仓库的 `docker-compose.yml`，或在「映像 → 新增」用 Dockerfile 构建。
* 注意 DSM 的共享文件夹 UID 通常不是 1000，可用 `SSH` 执行 `id <用户名>` 查真实 UID/GID 后填入 `PUID`/`PGID`。
* 若映射 `/volume1/video`，容器内即为 `/volume1/video`（或自定路径），设置页填对应容器路径。

**威联通 QNAP（Container Station）**
* 支持直接粘贴 compose；媒体库挂载建议用「共享文件夹」而非「卷」。

**Unraid**
* 用 Community Applications 模板或以 compose 方式创建；`/mnt/user/media` 映射到 `/media`。

**TrueNAS SCALE**
* Apps → Custom App，或使用 `docker compose`；存储挂载选 `Host Path`。

## 6. 可选组件与镜像体积

| 构建参数 | 效果 |
| --- | --- |
| `INSTALL_CHROMIUM=1`（默认） | 含 Chromium，`avwiki` 数据源可用（镜像较大，约 +300MB） |
| `INSTALL_CHROMIUM=0` | 精简镜像；其余数据源（javbus / javdb / local_nfo / 自定义源）不受影响 |
| `WITH_ASR=1` | 内置语音对白提取依赖（faster-whisper，约 +170MB）；**仅 x86_64**。模型（约 460MB）不进镜像，放挂载的 `/data/models/faster-whisper-small` |

```bash
# 精简构建
docker build --build-arg INSTALL_CHROMIUM=0 -t avm:slim .
```

`ffmpeg` 默认内置，用于封面抽帧与预览图；如需自备，可在设置页填写 `ffmpeg_path`。

### 6.1 语音对白提取（NAS 上跑 Whisper）

**方式一：全量插件版镜像（推荐，零安装）**

CI 在每次发版时同时发布 `-full` 后缀镜像（`latest-full` / `x.y.z-full`），已内置 faster-whisper 依赖，改一行 compose 即可：

```yaml
    image: ghcr.io/dengji85/avm:latest-full
```

模型不随镜像，放到挂载卷（约 460MB，可用 `python tools/fetch_asr_model.py` 下载，或在扩展组件页下载）：

```yaml
    volumes:
      - ./data:/data
      - ./models:/data/models     # 里面放 faster-whisper-small/
```

**方式二：精简镜像 + 页面安装**

`设置 → 扩展组件` 里「语音识别依赖」一键安装（同样仅 x86_64；断点续传、失败换源）。

**性能参考（CPU 版 faster-whisper-small，int8，以实测为准）**

| 机器 | `fast` 档（beam=1） |
| --- | --- |
| N100 / N5105 四核 | 约 3–6 倍实时，2 小时片 20–40 分钟 |
| 老款 Celeron（J4105 级别） | 约 1–2 倍实时 |
| ARM NAS | 慢于实时，不建议 |

* 内存峰值约 0.7–1GB；任务**串行**执行（同一时间只跑一部），跑的时候 CPU 占满属正常，播放 / 扫描会被短暂挤压，建议在空闲时段批量跑。
* 建议在 `config.json` 把 `asr.quality` 设为 `"fast"`：这类片对白清晰、基本单人说话，束宽几乎不影响准确度，速度快 2–3 倍。
* 依赖装了但没模型时，接口会明确提示缺模型，其他 AI 功能不受影响。

**升级**：普通升级 `docker compose pull && docker compose up -d` 即可——页面安装的组件在 `/data/ext`、模型在 `/data/models`，都在挂载卷里，**升级不丢、无需重装**；从精简镜像换 `latest-full` 也一样，换完依赖直接可用，`/data/ext` 里的旧安装可留可卸。库与配置自动迁移，媒体库无需重建。

## 7. 反向代理与 HTTPS

容器内服务是明文 HTTP，建议由 NAS 的反向代理（Nginx / Traefik / 群晖反代）终止 TLS：

* 代理到 `http://<容器IP或宿主IP>:8770`；
* **首次访问**：页面弹出登录框，输入访问令牌（用 `?token=xxx` 打开也可以 —— 后端会立刻把它换成会话并 `302` 去掉地址栏参数），之后由 **HttpOnly 会话 Cookie** 维持登录，不必重复输入；
* 所有来源一视同仁：本机 `127.0.0.1`、局域网、反代过来的请求**都要令牌**（已取消「本机免令牌」）；
* **真实客户端 IP**：只有当直连来源位于「信任的代理」列表（默认 `127.0.0.1, ::1`，即反代与容器同机）时，后端才采信 `X-Forwarded-For`。反代在**其它主机 / 容器**时，请在「设置 → 访问 → 信任的代理」补上它的地址，否则登录限速与审计里记录的 IP 会失真；
* **HTTPS**：让反代传 `X-Forwarded-Proto: https`，会话 Cookie 会自动带 `Secure`、响应自动带 HSTS，无需额外配置；
* **上线前请对照 [docs/安全与隐私设计.md](安全与隐私设计.md) 的「9.2 NAS 上线检查清单」逐条确认**（访问保护、信任代理、会话策略、令牌位置等）。

## 8. 升级

```bash
git pull
docker compose up -d --build
```

`/data` 卷保留即可，数据库会自动迁移（如新增 `missing_since` 列），无需重建库。

## 9. 常见问题

**Q：媒体库文件显示「缺失」？**
容器内路径必须与设置页填写的一致；宿主机换了挂载点后请同步修改设置。

**Q：删除时没有进回收站？**
容器内 `send2trash` 通常不可用，因此首次启动的默认配置是「仅移出媒体库」（保留文件）。
需要真正删文件时，请在设置页显式开启回收站（或选「永久删除」，谨慎）。

**Q：av-wiki 抓取失败？**
确认镜像含 Chromium（`INSTALL_CHROMIUM=1`），必要时调整 `AVM_CHROME_ARGS`；
同时给容器足够的共享内存（compose 里 `shm_size: "256m"`）。

**Q：抽帧 / 预览图失败？**
确认镜像内 `ffmpeg` 可用：`docker compose exec avm ffmpeg -version`。

**Q：时间不对（差 8 小时）？**
设置 `TZ=Asia/Shanghai`（镜像已默认），并确保宿主机时间正确。

**Q：卷权限报错？**
`PUID`/`PGID` 需与媒体库文件属主一致；必要时在宿主机执行
`chown -R <uid>:<gid> ./data` 后重启容器。

**Q：「导出到媒体目录」没反应 / 提示目录不可写？**
媒体库默认以 `:ro` 只读方式挂载，导出会被跳过。把 compose 里的 `:/media:ro` 改成 `:/media:rw`
（或 `:/media`）后重启容器即可；只想偶尔导出时，也可以只给需要写回的目录单独挂一个可写卷。

## 10. 与 Windows 桌面版的差异

| 功能 | 容器内 |
| --- | --- |
| 浏览 / 扫描 / 刮削 / 统计 / 片单 / 播放（网页） | ✅ 完整可用 |
| 调用系统播放器 / 在文件夹中显示 | ❌ 前端自动隐藏 |
| 删除到系统回收站 | ⚠️ 默认关闭（改为仅移出库） |
| av-wiki（CDP 抓取） | ⚠️ 需镜像含 Chromium |
| 封面抽帧 / 预览图 / 时长探测 | ⚠️ 需镜像含 ffmpeg |

> 从 Windows 版迁移旧库时要注意：数据库里保存的是 Windows 绝对路径（`D:\...`），
> 在容器内不可访问，需要重新扫描建库，或先把媒体文件按新路径整理后用「重解析」校正。

## 11. 自动构建（CI）

### 11.1 容器镜像 `.github/workflows/docker.yml`

| 触发 | 产出标签 |
| --- | --- |
| 推送 `v*` 标签（如 `v1.15.0`） | `1.15.0`、`1.15`、`latest`；并行产出 `-full` 全量插件版（amd64） |
| 推送 `main` 分支 | `main`；另有 `main-full` |
| 推送 `main` 分支 | `main` |
| 手动触发（Actions → Run workflow） | 按当前分支规则 |

* 平台：`linux/amd64` + `linux/arm64`（ARM NAS 直接用 arm64 镜像）；
* 构建缓存用 GitHub Actions Cache（`type=gha`），二次构建明显加速；
* 默认 `--build-arg INSTALL_CHROMIUM=1`（含 av-wiki 的 CDP 抓取），可改为 `0` 减体积。

> `latest` 只在正式打 `v*` 标签时更新，避免被 `main` 的临时构建覆盖。

### 11.2 Windows 单文件 exe `.github/workflows/release.yml`

| 触发 | 行为 |
| --- | --- |
| 推送 `v*` 标签 | 构建前端 → PyInstaller 打包 → `SHA256.txt` → 创建 / 更新 GitHub Release（正文取 `RELEASE_NOTES.md`） |
| 手动触发（Actions → Run workflow） | 只构建并上传为 Actions 工件，**不创建 Release**（用于零打扰验证打包链路） |

> **发布节奏**：纯构建 / CI 类改动不建议单独发版——用户看不到功能变化，容易觉得"更新了个寂寞"。
> 更规范的做法是随下一个有实际内容的版本一起发布；期间用「手动触发」验证打包是否正常。

### 11.3 发布流程

```
改版本号（app/__init__.py、web_src/package.json）
  → 更新 CHANGELOG.md / RELEASE_NOTES.md
  → 提交、推送
  → git tag vX.Y.Z && git push origin vX.Y.Z
```

一条 tag 同时产出：**Windows exe + SHA256**（GitHub Release）与 **amd64/arm64 镜像**（GHCR）。
NAS 端升级：`docker compose pull && docker compose up -d`。

### 11.4 为什么不做 macOS / Linux 原生二进制

| 平台 | 方案 | 说明 |
| --- | --- | --- |
| Windows | 单文件 exe | 桌面用户主力，PyInstaller 打包简单、收益高 |
| Linux / NAS / ARM | 多架构容器镜像 | 已覆盖全部 Linux 场景，且免去发行版碎片化问题 |
| macOS | ❌ 默认不做 | 未签名/公证会被 Gatekeeper 拦截，需 Apple 开发者账号与公证流程，成本高收益低 |
| 源码 | GitHub 自动提供 zip/tar | 无需额外工作 |
