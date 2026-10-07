# syntax=docker/dockerfile:1
#
# 片匣（AVM）容器镜像
# - 多阶段构建：前端在 node 阶段构建，最终镜像只带运行期产物
# - 非 root 运行，支持 PUID/PGID 对齐 NAS 宿主机文件属主
# - tini 作为 init，正确处理信号与僵尸进程
# - 默认内置 ffmpeg（抽帧/预览）与 chromium（av-wiki 的 CDP 抓取）
#   不需要 chromium 可用 --build-arg INSTALL_CHROMIUM=0 减体积
#
# 构建：docker build -t avm:latest .
# 运行：docker compose up -d   （见 docker-compose.yml）

# ---------------------------------------------------------------- 1) 前端构建
FROM node:20-alpine AS web
WORKDIR /build
COPY web_src/package*.json ./
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; \
    else npm install --no-audit --no-fund; fi
COPY web_src/ ./
# 直接调用 vite，避免 package.json 里已含 --outDir 造成参数重复
RUN npx vite build --outDir /out --emptyOutDir

# ---------------------------------------------------------------- 2) 运行时
FROM python:3.11-slim-bookworm

# 是否内置 Chromium（av-wiki 数据源走 CDP 需要；纯精简镜像可设 0）
ARG INSTALL_CHROMIUM=1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    AVM_DATA_DIR=/data \
    AVM_HOST=0.0.0.0 \
    AVM_PORT=8770 \
    AVM_CONTAINER=1 \
    AVM_CHROME_ARGS="--no-sandbox --disable-dev-shm-usage" \
    PUID=1000 \
    PGID=1000 \
    UMASK=022

# 系统依赖：ffmpeg(抽帧) chromium(CDP) tini(init) gosu(降权) tzdata(时区) curl(健康检查)
RUN set -eux; \
    apt-get update; \
    pkgs="ffmpeg tini gosu tzdata ca-certificates curl"; \
    if [ "$INSTALL_CHROMIUM" = "1" ]; then pkgs="$pkgs chromium"; fi; \
    apt-get install -y --no-install-recommends $pkgs; \
    if [ "$INSTALL_CHROMIUM" = "1" ]; then ln -sf /usr/bin/chromium /usr/bin/google-chrome; fi; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py ./
COPY --from=web /out ./web_dist
COPY docker/entrypoint.sh /entrypoint.sh
# sed 去掉可能的 CRLF，保证在 Linux 内可执行
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh && mkdir -p /data /media

VOLUME ["/data", "/media"]
EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${AVM_PORT}/api/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["python", "run.py", "serve", "--host", "0.0.0.0", "--port", "8770", "--no-browser"]
