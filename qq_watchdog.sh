#!/usr/bin/env bash
# QQ 机器人看门狗：保活 监管进程 + 接收端（崩溃自动拉起，重新搭建版）
set -u

QQBOT_DIR="/root/qqbot"
ENV_FILE="$QQBOT_DIR/.env"
CHECK_INTERVAL=30

cd "$QQBOT_DIR" || exit 1

# 载入配置（若存在）
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

echo "[watchdog] 启动，监控 superviser + receiver"

while true; do
  if [ "${SUPERVISE:-on}" != "off" ]; then
    if ! pgrep -f "qq_superviser.py" > /dev/null 2>&1; then
      echo "[watchdog] 拉起 superviser @ $(date '+%H:%M:%S')" >> "$QQBOT_DIR/superviser.log"
      nohup python3 "$QQBOT_DIR/qq_superviser.py" >> "$QQBOT_DIR/superviser.log" 2>&1 &
    fi
  fi
  if ! pgrep -f "qq_receiver.py" > /dev/null 2>&1; then
    echo "[watchdog] 拉起 receiver @ $(date '+%H:%M:%S')" >> "$QQBOT_DIR/receiver.log"
    nohup python3 "$QQBOT_DIR/qq_receiver.py" >> "$QQBOT_DIR/receiver.log" 2>&1 &
  fi
  sleep "$CHECK_INTERVAL"
done
