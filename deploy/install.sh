#!/usr/bin/env bash
# QQ 机器人一键部署到 Linux 服务器（systemd 常驻，7x24）
# 用法： sudo bash deploy/install.sh
# 适用：Ubuntu 22.04 / 24.04（其它发行科把 useradd/pip 两处适配一下）
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST=/opt/qqbot
PY="$(command -v python3)"

echo "==> [1/6] 创建运行用户 qqbot（系统用户，无登录 shell）"
if ! getent group qqbot >/dev/null; then
  groupadd --system qqbot
fi
if ! id -u qqbot >/dev/null 2>&1; then
  useradd --system --gid qqbot --home-dir "$DST" --shell /usr/sbin/nologin qqbot
fi

echo "==> [2/6] 拷贝代码到 $DST（白名单：qq_*.py + 配置文件）"
mkdir -p "$DST"
# 白名单而非排除名单：模型目录/日志/数据 json/脚本 deploy.sh、qq_watchdog.sh
# （systemd 模式下不应跑）一律不带；新增 qq_*.py 会自动带上。
# 想迁移沙箱聊天数据，部署完后手动 scp 这两个文件再 chown qqbot:qqbot：
#   qq_profiles.json  chat_log.jsonl
(cd "$SRC" && tar -cf - qq_*.py .env.example requirements.txt README.md .gitignore) \
  | tar -C "$DST" -xf -

echo "==> [3/6] 安装最小依赖（只要 websocket-client；API 模式不需要 torch）"
# Ubuntu 22.04+ 有 PEP 668 外部管理保护，需 --break-system-packages
$PY -m pip install --quiet --break-system-packages websocket-client 2>/dev/null \
  || $PY -m pip install --quiet websocket-client

echo "==> [4/6] 检查 .env"
if [ ! -f "$DST/.env" ]; then
  if [ -f "$SRC/.env" ]; then
    install -m 600 "$SRC/.env" "$DST/.env"
    echo "    已从源码目录复制 .env"
  else
    cp "$DST/.env.example" "$DST/.env"
    echo "    !! 已生成空白 .env —— 启动前必须编辑 $DST/.env 填入 APP_ID/APP_SECRET"
  fi
fi
chown -R qqbot:qqbot "$DST"

echo "==> [5/6] 安装 systemd unit"
install -m 644 "$SRC/deploy/qqbot.service" /etc/systemd/system/qqbot.service
# unit 里写死 /usr/bin/python3；若本机 python3 不在该路径则替换
if [ "$PY" != "/usr/bin/python3" ]; then
  sed -i "s|/usr/bin/python3|$PY|g" /etc/systemd/system/qqbot.service
fi
systemctl daemon-reload

echo "==> [6/6] 启用并启动"
systemctl enable --now qqbot
sleep 2
systemctl status qqbot --no-pager || true
echo
echo "部署完成。常用命令："
echo "  systemctl status qqbot        查看状态"
echo "  journalctl -u qqbot -f        实时日志（systemd 侧）"
echo "  tail -f $DST/receiver.log     应用日志（同内容，追加写）"
echo "  systemctl restart qqbot       改完代码/配置后重启"
echo "  systemctl disable --now qqbot 停机"
