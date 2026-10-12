# 片匣 v1.13.1 版本说明

> 相比 v1.13.0 的变更。本版新增 **Docker / NAS 一键部署** 与 **网页播放兼容层**（浏览器不支持的容器会自动快速转封装），并修复社区反馈的远程访问令牌 401、首页搜索框无响应等问题。

## 🐳 Docker / NAS 部署（新增）

提供官方多架构镜像（`linux/amd64` + `linux/arm64`），群晖 / 威联通 / Unraid / TrueNAS 上**无需编译**：

```bash
docker run -d --name avm \
  -p 8770:8770 \
  -e TZ=Asia/Shanghai \
  -e PUID=1000 -e PGID=1000 \
  -v /path/to/data:/data \
  -v /path/to/media:/media:ro \
  --shm-size=256m \
  --restart unless-stopped \
  ghcr.io/dengji85/avm:latest
```

* 镜像内置 `ffmpeg`（抽帧/预览）与 `chromium`（av-wiki 抓取），开箱可用；
* 数据（数据库 / 配置 / 封面 / 头像 / 预览）全在 `/data` 卷，升级只换镜像、库自动迁移；
* Windows 单文件 exe 同样由 Actions 自动构建并附加到 Release。

**镜像标签怎么选**：`latest` = 最近一次正式发布（稳定，推荐）；`main` = main 分支最新构建（含尚未发布的修复，想尝鲜可用）；`1.13.1` = 固定版本。

> 容器不会继承宿主机的代理设置。刮削需要走代理时，请在 compose 里显式声明 `HTTP_PROXY` / `HTTPS_PROXY`（建议同时设 `NO_PROXY=localhost,127.0.0.1`），详见 [docs/Docker部署.md](docs/Docker部署.md)。

## ▶ 网页播放兼容层：MKV 等容器自动转封装

以前 MKV、AC3 / DTS 音轨的影片网页播不了，只能换系统播放器。现在播放失败时会**自动快速重封装为 MP4**（`-c:v copy` 只复制视频流、不重新编码，几乎不耗 CPU）：

* 首次播放这类片子会显示「正在准备播放」与进度条，转好后**同一部片再播直接命中缓存**；
* HEVC / 10bit 等需要真转码的**行为不变**，仍提示用系统播放器；
* 缓存位于 `data/remux`，可在 **设置 → 网页播放** 查看 / 清理（默认上限 4 GB，超限自动清理最久未访问的）；
* 不需要的话，可在同一面板关闭「播放失败时自动转封装」。

## 🔧 社区反馈修复

* **远程访问令牌 401（#1）**：令牌此前只随接口请求头发送，而封面、头像、播放流是浏览器**直接请求的 URL**，无法携带自定义请求头 —— 于是远程访问时出现「设置能改能保存，封面与播放却全部 401」。现在令牌同时写入 Cookie，打开页面会自动补写，**无需重新填写**。
* **首页搜索框回车无响应（#1）**：此前回车只更新关键词、不切换视图，停在首页看不出任何变化；现在回车会自动跳到影片库展示结果。

## 📄 文档

* README 新增 Docker / NAS 部署章节与镜像标签说明（中英日繁四语言）；
* `docs/Docker部署.md` 补充容器代理配置（`HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`）。

## 升级方式

下载 `AVM-1.13.1.exe` 覆盖旧版即可，沿用原 `data/` 目录（数据库会自动迁移，无需重新建库）。
Docker 用户执行 `docker compose pull && docker compose up -d`。

> 提示：网页播放首次遇到 MKV 类影片时需要等待转封装（只复制视频流），属正常现象；缓存可在「设置 → 网页播放」中清理。
