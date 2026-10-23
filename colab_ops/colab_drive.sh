#!/usr/bin/env bash
# colab_drive.sh —— Colab VM 侧 Google Drive 接入（rclone，不依赖 Colab 前端）
#
# 背景：纯 SSH 会话里 `google.colab.drive.mount()` 必然失败（缺 TBE_EPHEM_CREDS_ADDR，
#       凭据只由浏览器前端投递）。本脚本用 rclone 自己的 OAuth 令牌，SSH 下完全可用。
#
# 用法（在 VM 里跑）：
#   bash colab_drive.sh install                 装 rclone（幂等）
#   bash colab_drive.sh config "<base64>"       写入 rclone.conf（从本机 mint_token.ps1 拿）
#   bash colab_drive.sh config-check            校验配置 + 配额 + 列目录（不写文件）
#   bash colab_drive.sh mount [--read-only]     挂载：/content/gdrive + 兼容软链 /content/drive/MyDrive
#   bash colab_drive.sh umount
#   bash colab_drive.sh status                  挂载点/日志/配额一览
#   bash colab_drive.sh verify                  写+读一个测试文件（端到端）
#   bash colab_drive.sh push <本地文件> <远端路径>     例：push /content/out/ckpt.pt backup/ckpt.pt
#   bash colab_drive.sh pull <远端路径> <本地目录>     例：pull backup/data.zip /content
#   bash colab_drive.sh ls [远端路径]
#
# 远端根 = 你的「我的云端硬盘」内容（rclone 语义：根就是 My Drive 里那一层，没有 MyDrive/ 一级；
# 兼容软链已把 /content/drive/MyDrive 指过去，老脚本里的路径照旧能用）。
#
# 环境变量：RCLONE_REMOTE（默认 gdrive）、DRIVE_MOUNT（默认 /content/gdrive）
set -euo pipefail

REMOTE="${RCLONE_REMOTE:-gdrive}"
MOUNT="${DRIVE_MOUNT:-/content/gdrive}"
COMPAT_LINK="/content/drive/MyDrive"
LOG="/content/rclone_drive.log"
CONF="$HOME/.config/rclone/rclone.conf"
RCLONE="${RCLONE_BIN:-rclone}"

say() { echo "[drive] $*"; }
die() { echo "[drive][FAIL] $*" >&2; exit 1; }

need_rclone() {
  command -v "$RCLONE" >/dev/null 2>&1 || die "没装 rclone，先跑: bash $0 install"
}

# 不依赖 mountpoint 命令（util-linux 缺失时也能判断）；注意 set -e 下要配合 if 使用
is_mounted() { grep -qs " $MOUNT " /proc/mounts; }

# ---------------------------------------------------------------- install
do_install() {
  if command -v rclone >/dev/null 2>&1; then
    say "rclone 已在: $(rclone version | head -1)"
  else
    say "下载 rclone（静态二进制，不走 apt）..."
    command -v unzip >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq unzip; }
    curl -fsSL https://downloads.rclone.org/rclone-current-linux-amd64.zip -o /tmp/rclone.zip
    rm -rf /tmp/rclone_x && mkdir -p /tmp/rclone_x
    unzip -q -o /tmp/rclone.zip -d /tmp/rclone_x
    install -m 0755 /tmp/rclone_x/rclone-*/rclone /usr/local/bin/rclone
    rm -rf /tmp/rclone.zip /tmp/rclone_x
    say "装好: $(rclone version | head -1)"
  fi

  # FUSE：mount 子命令需要 fuse3 的 fusermount3。
  # 实测坑（2026-10-06 Colab CPU 会话）：镜像里只有 fuse2 的 /usr/bin/fusermount，
  # rclone 1.75 只认 fusermount3 → 必须先装 fuse3，否则 mount 报
  # "failed to mount FUSE fs: fusermount: exec: \"fusermount3\": executable file not found in $PATH"。
  if [ -e /dev/fuse ]; then
    say "/dev/fuse 存在"
  else
    say "警告：/dev/fuse 不存在 → 只能 push/pull，不能 mount"
  fi
  if ! command -v fusermount3 >/dev/null 2>&1; then
    say "缺 fusermount3（fuse3），apt 安装 ..."
    apt-get update -qq && apt-get install -y -qq fuse3 || say "fuse3 安装失败"
  fi
  if command -v fusermount3 >/dev/null 2>&1; then
    say "fusermount3: $(command -v fusermount3)"
  else
    say "警告：仍无 fusermount3 → mount 不可用，改用 push/pull"
  fi
  mkdir -p "$(dirname "$CONF")"
}

# ---------------------------------------------------------------- config
do_config() {
  local b64="${1:-}"
  [ -n "$b64" ] || die "用法: bash $0 config \"<base64 of rclone.conf>\""
  need_rclone
  mkdir -p "$(dirname "$CONF")"
  printf '%s' "$b64" | base64 -d > "$CONF"
  chmod 600 "$CONF"
  say "写入 $CONF"
  grep -c '^\[.*\]$' "$CONF" >/dev/null || die "配置看起来不对（没有 [remote] 段）"
  say "自检 ..."
  rclone about "$REMOTE:" || die "rclone 连不上 $REMOTE（令牌/配置问题）"
  rclone lsd "$REMOTE:" --max-depth 1 | head -15 || true
}

# ---------------------------------------------------------------- config-check
do_config_check() {
  need_rclone
  [ -f "$CONF" ] || die "没有 $CONF，先跑 config"
  say "配置: $CONF ($(stat -c%s "$CONF") bytes)"
  grep -E '^\[' "$CONF" | sed 's/^/  remote: /' || true
  say "连通性 + 配额 ..."
  rclone about "$REMOTE:" || die "连不上 $REMOTE（令牌过期/配置错）"
  rclone lsd "$REMOTE:" --max-depth 1 | head -15 || true
}

# ---------------------------------------------------------------- mount
do_mount() {
  local ro=""
  # 注意：set -e 下不能写 `[ ... ] && ro=...`（条件为假时整行返回非零 → 脚本直接退出）
  if [ "${1:-}" = "--read-only" ]; then ro="--read-only"; fi
  need_rclone
  [ -f "$CONF" ] || die "没有 $CONF，先跑 config"
  [ -e /dev/fuse ] || die "/dev/fuse 不存在，mount 不可用 → 用 push/pull 子命令"

  mkdir -p "$MOUNT"
  if is_mounted; then
    say "$MOUNT 已挂载，先 umount"
    do_umount
  fi

  command -v fusermount3 >/dev/null 2>&1 || say "注意：没有 fusermount3，mount 大概率失败（先跑 install 装 fuse3）"

  say "挂载 $REMOTE: → $MOUNT $ro"
  # set -e 下必须用 if 包住：--daemon 在挂载失败时父进程返回非零，
  # 直接裸调会被 set -e 静默打断（实测踩过：只看到 exit 1，真正原因在 log 里）。
  # </dev/null >>"$LOG" 2>&1 是必须的：否则 daemon 会继承调用方的 stdout/stderr，
  # 调用方若用管道捕获输出（python subprocess capture_output=True / $() / ssh）就会
  # 永远等不到 EOF 而挂死（实测踩过：setup_on_vm.ps1 卡住 10 分钟）。
  if ! rclone mount "$REMOTE:" "$MOUNT" \
    $ro \
    --vfs-cache-mode writes \
    --vfs-cache-max-size 8G \
    --vfs-cache-max-age 1h \
    --vfs-cache-poll-interval 30s \
    --dir-cache-time 24h \
    --poll-interval 0 \
    --buffer-size 64M \
    --drive-chunk-size 64M \
    --transfers 8 \
    --checkers 16 \
    --uid 0 --gid 0 --umask 022 \
    --log-file "$LOG" --log-level INFO \
    --daemon </dev/null >>"$LOG" 2>&1
  then
    say "rclone mount 返回非零，日志尾部："
    tail -n 15 "$LOG" 2>/dev/null || true
    die "挂载失败（完整日志 $LOG）"
  fi

  # 等挂载就绪（最多 30s）；ls 一律套 timeout，防止 FUSE 卡住时无限等
  for i in $(seq 1 30); do
    if is_mounted && timeout 10 ls "$MOUNT" >/dev/null 2>&1; then break; fi
    sleep 1
  done
  if ! is_mounted; then tail -30 "$LOG" 2>/dev/null || true; die "挂载没起来，看 $LOG"; fi

  # 路径兼容：老脚本用 /content/drive/MyDrive/...
  mkdir -p /content/drive
  if [ ! -e "$COMPAT_LINK" ] || [ -L "$COMPAT_LINK" ]; then
    ln -sfn "$MOUNT" "$COMPAT_LINK"
    say "兼容软链: $COMPAT_LINK → $MOUNT"
  fi

  say "挂载成功。顶层目录："
  timeout 20 ls -1 "$MOUNT" | head -20 || true
  say "老路径可用性检查: ls /content/drive/MyDrive"
  if timeout 20 ls -1 "$COMPAT_LINK" >/dev/null 2>&1; then say "  OK"; else say "  失败（软链没生效）"; fi
}

do_umount() {
  if is_mounted; then
    fusermount3 -u "$MOUNT" 2>/dev/null || fusermount -u "$MOUNT" 2>/dev/null || umount "$MOUNT" || true
    say "已卸载 $MOUNT"
  else
    say "$MOUNT 没在挂载状态"
  fi
  pkill -f "rclone mount $REMOTE:" 2>/dev/null || true
}

# ---------------------------------------------------------------- status
do_status() {
  echo "--- rclone 进程 ---"; pgrep -af "rclone mount" || echo "(无)"
  echo "--- 挂载点 ---"; if is_mounted; then echo "$MOUNT 已挂载"; else echo "$MOUNT 未挂载"; fi
  if [ -L "$COMPAT_LINK" ]; then echo "软链 $COMPAT_LINK → $(readlink "$COMPAT_LINK")"; fi
  if command -v rclone >/dev/null 2>&1 && [ -f "$CONF" ]; then
    echo "--- 配额 ---"; rclone about "$REMOTE:" 2>&1 | head -6 || true
    echo "--- VFS 缓存 ---"; du -sh ~/.cache/rclone 2>/dev/null || true
  fi
  echo "--- 日志尾 ---"; tail -8 "$LOG" 2>/dev/null || echo "(无日志)"
}

# ---------------------------------------------------------------- verify
do_verify() {
  need_rclone
  local f=".colab_drive_write_test_$(date +%s).txt"
  say "写测试文件 → $REMOTE:"
  echo "hello from $(hostname) at $(date -Is)" > "/tmp/$f"
  rclone copyto "/tmp/$f" "$REMOTE:$f" --drive-chunk-size 8M
  say "读回对比 ..."
  rclone cat "$REMOTE:$f" | head -2 || true
  rclone delete "$REMOTE:$f" || true
  rm -f "/tmp/$f"
  say "端到端 OK（写/读/删都通）"
}

# ---------------------------------------------------------------- push/pull
do_push() {
  local src="${1:?用法: push <本地文件> <远端路径>}" dst="${2:?缺远端路径}"
  need_rclone
  local sz; sz=$(stat -c%s "$src" 2>/dev/null || echo 0)
  say "上传 $(basename "$src") ($((sz/1048576))MB) → $REMOTE:$dst"
  rclone copyto "$src" "$REMOTE:$dst" \
    --drive-chunk-size 64M --transfers 4 --checkers 8 \
    --multi-thread-streams 4 --stats 30s --stats-one-line -v
  say "完成。远端确认："
  rclone lsjson "$REMOTE:$dst" | head -5 || true
}

do_pull() {
  local src="${1:?用法: pull <远端路径> <本地目录>}" dst="${2:?缺本地目录}"
  need_rclone
  mkdir -p "$dst"
  say "下载 $REMOTE:$src → $dst"
  rclone copy "$REMOTE:$src" "$dst" \
    --drive-chunk-size 64M --transfers 4 --checkers 8 \
    --multi-thread-streams 8 --multi-thread-cutoff 64M \
    --stats 30s --stats-one-line -v
  say "完成："; ls -lh "$dst" | head -10
}

do_ls() { need_rclone; rclone lsd "$REMOTE:${1:-}" --max-depth 1 | head -30 || true; }

# ---------------------------------------------------------------- dispatch
cmd="${1:-status}"; shift || true
case "$cmd" in
  install) do_install ;;
  config)  do_config "$@" ;;
  config-check) do_config_check ;;
  mount)   do_mount "$@" ;;
  umount)  do_umount ;;
  status)  do_status ;;
  verify)  do_verify ;;
  push)    do_push "$@" ;;
  pull)    do_pull "$@" ;;
  ls)      do_ls "$@" ;;
  *) sed -n '2,30p' "$0"; exit 1 ;;
esac
