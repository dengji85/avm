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

# 4) 查看日志，取得访问令牌
docker compose logs avm | grep 远程访问令牌
```

指定版本：

```bash
docker pull ghcr.io/dengji85/avm:1.13.0
```

> 首次使用需要把 GHCR 上的镜像包设为 **Public**（GitHub → 该仓库 → Packages → 对应镜像 → Package settings → Change visibility），
> 否则拉取时会要求登录。

### 本地构建（可选，无需 CI）

```bash
docker compose up -d --build
# 或
docker build -t avm:latest .
```

浏览器打开 `http://<NAS-IP>:8770`，若提示需要令牌，把上面的令牌粘贴进去（也可用
`http://<NAS-IP>:8770/?token=<令牌>` 直达）。

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
| `AVM_CONTAINER` | `1` | 标记容器环境，前端据此隐藏桌面相关入口 |
| `AVM_CHROME_ARGS` | `--no-sandbox --disable-dev-shm-usage` | 传给内置 Chromium 的附加参数 |

## 3. 卷

| 容器路径 | 用途 | 建议 |
| --- | --- | --- |
| `/data` | 配置、数据库、封面、头像、预览图 | **必须持久化**，升级时保留 |
| `/media` | 媒体库 | 按实际路径挂载，建议 `:ro` 只读 |

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

```bash
# 精简构建
docker build --build-arg INSTALL_CHROMIUM=0 -t avm:slim .
```

`ffmpeg` 默认内置，用于封面抽帧与预览图；如需自备，可在设置页填写 `ffmpeg_path`。

## 7. 反向代理与 HTTPS

容器内服务是明文 HTTP，建议由 NAS 的反向代理（Nginx / Traefik / 群晖反代）终止 TLS：

* 代理到 `http://<容器IP或宿主IP>:8770`；
* 访问时需携带令牌（`?token=xxx` 或表单输入），由后端中间件校验；
* 通过代理访问时来源 IP 不是 `127.0.0.1`，**不享受本机豁免**，这是预期行为。

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
| 推送 `v*` 标签（如 `v1.13.0`） | `1.13.0`、`1.13`、`latest` |
| 推送 `main` 分支 | `main` |
| 手动触发（Actions → Run workflow） | 按当前分支规则 |

* 平台：`linux/amd64` + `linux/arm64`（ARM NAS 直接用 arm64 镜像）；
* 构建缓存用 GitHub Actions Cache（`type=gha`），二次构建明显加速；
* 默认 `--build-arg INSTALL_CHROMIUM=1`（含 av-wiki 的 CDP 抓取），可改为 `0` 减体积。

> `latest` 只在正式打 `v*` 标签时更新，避免被 `main` 的临时构建覆盖。

### 11.2 Windows 单文件 exe `.github/workflows/release.yml`

打 `v*` 标签时自动：构建前端 → PyInstaller 打包 → 生成 `SHA256.txt` → 创建 / 更新 GitHub Release 并附加产物（Release 正文取自 `RELEASE_NOTES.md`）。

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
