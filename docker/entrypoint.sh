#!/usr/bin/env bash
# 片匣容器入口：
# 1) 按 PUID/PGID 准备运行用户（NAS 场景对齐宿主机文件属主）
# 2) 确保数据目录存在并归属正确
# 3) 首次启动写入容器友好的默认配置（容器内回收站通常不可用 → 默认关闭）
# 4) 以非 root 身份执行主进程（tini 负责信号转发）
set -euo pipefail

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-022}"
DATA_DIR="${AVM_DATA_DIR:-/data}"

umask "$UMASK"
mkdir -p "$DATA_DIR"

# ---- 运行用户 ----
if ! getent group "$PGID" >/dev/null 2>&1; then
  groupadd -o -g "$PGID" avm
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
  useradd -o -u "$PUID" -g "$PGID" -M -d /app -s /usr/sbin/nologin avm
fi
RUN_USER="$(getent passwd "$PUID" | cut -d: -f1)"

# 只调整数据目录顶层属主，避免对大库递归遍历
chown "$PUID:$PGID" "$DATA_DIR" 2>/dev/null || true

# ---- 首次启动的默认配置 ----
# 容器内 send2trash 通常不可用：默认「移出媒体库」（保留文件），需要时可自行在设置页开启。
CFG="$DATA_DIR/config.json"
if [ ! -f "$CFG" ]; then
  printf '%s\n' '{"library":{"delete_to_recycle_bin":false}}' > "$CFG"
  chown "$PUID:$PGID" "$CFG" 2>/dev/null || true
  echo "[avm] 已生成初始配置：$CFG（容器内默认不删文件，仅移出媒体库）"
fi

echo "[avm] data=$DATA_DIR uid=$PUID gid=$PGID umask=$UMASK tz=${TZ:-UTC}"

exec gosu "$RUN_USER" "$@"
