#!/usr/bin/env bash
# QQ 监督员一键部署脚本
# 用法： bash deploy.sh
set -e
cd "$(dirname "$0")"

echo "[deploy] 安装 Python 依赖..."
pip3 install -r requirements.txt

if [ ! -f .env ]; then
  echo "[deploy] 未发现 .env，已从 .env.example 复制，请编辑填入你的 APP_ID / APP_SECRET"
  cp .env.example .env
fi

echo "[deploy] 请在 .env 中确认以下配置："
echo "        APP_ID / APP_SECRET   —— QQ 开放平台应用凭证"
echo "        QQ_BRAIN_MODEL        —— 1.5b(默认) / 0.5b / 7b"
echo "        SUPERVISE             —— on(开启定时推送) / off(仅对话)"
echo ""
echo "[deploy] 启动看门狗（自动保活 receiver，SUPERVISE=on 时同时拉起 superviser）..."
bash qq_watchdog.sh
