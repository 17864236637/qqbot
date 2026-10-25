#!/usr/bin/env bash
# QQ 机器人看门狗（保活 qq_receiver.py）
#
# 适用场景：没有 systemd 的环境（沙箱 / 安卓 Termux）。
# systemd 部署（deploy/install.sh）不要用本脚本——Restart=always 就是保活，
# 两层保活会重复拉起。
#
# 用法：
#   ./qq_watchdog.sh [监控目录]     # 默认监控脚本所在目录
#   setsid nohup ./qq_watchdog.sh >/dev/null 2>&1 &   # 后台常驻
#   停：kill $(cat 监控目录/.watchdog.lock/pid)
#
# 设计要点（都是踩过的坑，别改掉）：
# 1. pidfile + kill -0 认进程，不用 pgrep -f 裸匹配——历史上裸匹配误捕
#    后台子 shell，导致"先杀旧再起新"的清理整段失败，双实例互抢会话。
# 2. mkdir 原子锁防双看门狗（flock 在 Termux 不保证有，mkdir 全平台可用）。
# 3. 双模探活：
#    a. 进程级：pidfile 失效即重启；
#    b. 活体级：进程在但 receiver.log 超 WATCHDOG_STALE_SEC 秒没更新
#       → 判定卡死（WS 静默断开、进程挂起型故障，进程级探活管不到），
#       kill -9 强杀后走重启流程。心跳 ACK 每秒级写日志，日志 mtime
#       就是可靠活体信号。
# 4. 崩溃退避：60 秒内死了再退避加倍（5→10→20→40→60 封顶），
#    跑过 60 秒才死则重置——避免无限热循环刷日志。
# 5. 重启时若 receiver.log 超 10MB 才轮转（进程按追加模式持握旧 fd，
#    运行中 mv 没用，只在重启点轮换）。
# 6. 会"领养"已在运行但没登记 pidfile 的接收器（手动 nohup 起的），
#    不会重复拉起。领养判定：pgrep 候选里 /proc/PID/comm 必须是 python*，
#    排除 bash 包装进程——防止把 bash -c 命令行误当成接收器。
#
# 可调参数（环境变量）：
#   WATCHDOG_CHECK_INTERVAL  检测间隔秒，默认 20
#   WATCHDOG_STALE_SEC       日志多久不更新算卡死，默认 300
set -u

DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PIDFILE="$DIR/receiver.pid"
LOCKDIR="$DIR/.watchdog.lock"
LOG="$DIR/watchdog.log"
CHECK_INTERVAL="${WATCHDOG_CHECK_INTERVAL:-20}"
STALE_SEC="${WATCHDOG_STALE_SEC:-300}"
MAX_LOG_BYTES=$((10 * 1024 * 1024))
BACKOFF_MAX=60

log() { echo "[watchdog] $* @ $(date '+%F %H:%M:%S')" >> "$LOG"; }

# 可被信号即时中断的 sleep。
# 直接用 `sleep N` 时，SIGTERM 要等 sleep 跑完才被 bash 处理（trap 延迟），
# 停看门狗最多要等一个检测周期；`sleep & wait` 让 wait 收到信号立即返回。
snooze() { sleep "$1" & wait $! 2>/dev/null; }

# ---- 单实例锁 ----
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  old_pid="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "已有看门狗在运行 (pid=$old_pid)，本实例退出" >&2
    exit 0
  fi
  rm -rf "$LOCKDIR"
  mkdir "$LOCKDIR" 2>/dev/null || { echo "无法创建锁目录" >&2; exit 1; }
fi
echo $$ > "$LOCKDIR/pid"
# 【必须带 exit】trap 只清理不 exit 的话，TERM 处理完脚本会继续循环，
# 看门狗永远退不掉（实测踩坑）。EXIT 分支保证任何退出路径都清锁。
trap 'rm -rf "$LOCKDIR"; exit 0' EXIT INT TERM

# ---- 进程存活判定：pidfile 优先，找不到再领养 ----
# 【安全红线】两种判定都必须验证 /proc/PID/cwd 指向本目录。
# 历史上 pgrep -f 是全机器范围搜索：另一个目录的看门狗会把本目录的
# 接收器当"自己家孩子"领养并强杀，两个看门狗互相抢杀（实测复现过）。
# cwd 绑定后，各看门狗只认自己目录里跑着的进程，物理隔离。
DIR_REAL="$(readlink -f "$DIR")"
receiver_pid() {
  local p comm cwd
  if [ -f "$PIDFILE" ]; then
    p="$(cat "$PIDFILE" 2>/dev/null)"
    if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
      cwd="$(readlink -f "/proc/$p/cwd" 2>/dev/null || true)"
      if [ "$cwd" = "$DIR_REAL" ]; then
        echo "$p"; return 0
      fi
    fi
  fi
  # 领养：pgrep 候选里找 comm 为 python* 且 cwd 在本目录的
  # （排除 bash 包装进程——防止把 bash -c 命令行误当成接收器）
  for p in $(pgrep -f "qq_receiver.py" 2>/dev/null); do
    [ "$p" = "$$" ] && continue
    comm="$(cat "/proc/$p/comm" 2>/dev/null || true)"
    case "$comm" in python*) ;; *) continue;; esac
    cwd="$(readlink -f "/proc/$p/cwd" 2>/dev/null || true)"
    if [ "$cwd" = "$DIR_REAL" ]; then
      echo "$p"; return 0
    fi
  done
  return 1
}

log "看门狗启动，监控目录 $DIR（检测间隔 ${CHECK_INTERVAL}s）"

# 启动时先领养一次（手动 nohup 起的接收器）
if p="$(receiver_pid)"; then
  echo "$p" > "$PIDFILE"
  log "领养已在运行的接收器 pid=$p"
fi

LAST_START=0
BACKOFF=5
while true; do
  if p="$(receiver_pid)"; then
    now="$(date +%s)"
    # 活体探活：日志超期未更新 = 进程挂着但连接已死（WS 静默断开型故障）
    # 保护：刚重启 STALE_SEC 之内的不杀——旧日志的 mtime 是继承来的，
    # 不设这道保护会把刚拉起的进程误杀，陷入重启-误杀死循环。
    if [ $((now - LAST_START)) -gt "$STALE_SEC" ] && [ -f "$DIR/receiver.log" ]; then
      mtime="$(stat -c%Y "$DIR/receiver.log" 2>/dev/null || echo 0)"
      if [ "$mtime" -gt 0 ] && [ $((now - mtime)) -gt "$STALE_SEC" ]; then
        log "接收器 pid=$p 存活但日志 $((now - mtime))s 无更新，判定卡死，强杀"
        kill -9 "$p" 2>/dev/null
        rm -f "$PIDFILE"
        snooze 2
        continue
      fi
    fi
    # 跑过 60 秒没死，退避重置
    if [ "$LAST_START" -gt 0 ] && [ $((now - LAST_START)) -ge 60 ]; then
      BACKOFF=5
    fi
    snooze "$CHECK_INTERVAL"
    continue
  fi

  # ---- 接收器不在：先算退避，再清理，再重启 ----
  now="$(date +%s)"
  if [ "$LAST_START" -gt 0 ] && [ $((now - LAST_START)) -lt 60 ]; then
    BACKOFF=$((BACKOFF * 2))
    [ "$BACKOFF" -gt "$BACKOFF_MAX" ] && BACKOFF=$BACKOFF_MAX
  else
    BACKOFF=5
  fi

  rm -f "$PIDFILE"
  log "接收器不在运行，${BACKOFF}s 后重启"
  snooze "$BACKOFF"

  # 重启点轮转日志（运行中 mv 无效，进程持握旧 fd）
  if [ -f "$DIR/receiver.log" ]; then
    size="$(stat -c%s "$DIR/receiver.log" 2>/dev/null || echo 0)"
    if [ "$size" -gt "$MAX_LOG_BYTES" ]; then
      mv -f "$DIR/receiver.log" "$DIR/receiver.log.1"
      log "receiver.log 超 ${MAX_LOG_BYTES} 字节，已轮转为 .1"
    fi
  fi

  cd "$DIR" || { log "无法进入 $DIR，看门狗退出"; exit 1; }
  nohup python3 qq_receiver.py >> receiver.log 2>&1 &
  new_pid=$!
  echo "$new_pid" > "$PIDFILE"
  LAST_START="$(date +%s)"
  log "已拉起接收器 pid=$new_pid"
done
