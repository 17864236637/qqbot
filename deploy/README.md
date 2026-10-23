# QQ 机器人 7×24 部署（systemd 版）

把机器人从沙箱/本机搬到一台 Linux 服务器上常驻。崩溃自动拉起、开机自启、
日志落盘。整套就是"拷代码 → 装依赖 → 装 unit → enable"四步，已打包成
`install.sh` 一键完成。

## 前置条件

1. 一台 Linux 服务器（VPS 即可，1核1G 都够——API 模式下不跑本地模型），
   Ubuntu 22.04 / 24.04 验证过路径。
2. 机器人已在 QQ 开放平台**发布/上架**。沙箱模式下 WebSocket 会被服务端以
   `op=9 Invalid Session` 拒绝，这是平台限制，代码侧绕不过。
3. 有 root 或 sudo 权限。

## 一键部署

在**源码目录**（本仓库根目录，含 `qq_receiver.py` 的目录）执行：

```bash
sudo bash deploy/install.sh
```

脚本依次做六件事：建 `qqbot` 系统用户 → 白名单拷贝代码到 `/opt/qqbot`
（只带 `qq_*.py` + `.env.example`/`requirements.txt`/`README.md`，模型和
数据 json 一概不带）→ `pip install websocket-client` → 检查 `.env` →
安装 `/etc/systemd/system/qqbot.service` → `systemctl enable --now qqbot`。

跑完后按提示看 `systemctl status qqbot`，出现 `active (running)` 即成功。

## .env 要填什么

`/opt/qqbot/.env`（KEY=VALUE 无引号格式，注释用 `#`）：

| 键 | 说明 |
|---|---|
| `APP_ID` / `APP_SECRET` | QQ 开放平台 → 你的机器人 → 开发设置 |
| `GROUP_OPENID` | 目标群 openid（机器人先加群，从回调日志里拿） |
| `QQ_BRAIN_MODEL` | 填 `api`（服务器方案：大脑走云 API） |
| `QQ_BRAIN_API_BASE` | 如 `https://api.deepseek.com/v1` |
| `QQ_BRAIN_API_KEY` | `sk-xxx` |
| `QQ_BRAIN_API_MODEL` | 如 `deepseek-chat` |

免费档推荐：智谱 `glm-4-flash`（base `https://open.bigmodel.cn/api/paas/v4`）
或硅基流动的 Qwen2.5-7B-Instruct。改完 `.env` 后 `systemctl restart qqbot` 生效。

> 不配 API 三件套也能启动，但服务器上没有本地模型文件，回复会是兜底文案。

## 关键一步：IP 白名单

QQ 开放平台要对机器人服务器出口 IP 加白名单。VPS 是固定 IP，只需加一个：

```bash
curl -s ifconfig.me   # 在服务器上跑，拿到出口 IP
```

把该 IP 填到开放平台 → 机器人 → 开发设置 → IP 白名单。
（沙箱/本机模式下出口 IP 会在几个地址间滚动，那是家庭宽动的锅；服务器没这问题。）

## 日常运维

```bash
systemctl status qqbot          # 状态
systemctl restart qqbot         # 重启（改代码/.env 后）
systemctl stop qqbot            # 停
systemctl disable --now qqbot   # 停机 + 取消开机自启
journalctl -u qqbot -f          # 实时看日志
tail -f /opt/qqbot/receiver.log # 应用日志（同一份内容，追加写）
```

**改代码**：直接编辑 `/opt/qqbot/qq_receiver.py` 等文件后 `systemctl restart qqbot`；
想从源码目录重新同步，重跑 `sudo bash deploy/install.sh`（不动 .env）。

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `status` 显示 `activating (auto-restart)` 循环 | 看 `journalctl -u qqbot -n 50`。九成是 .env 没填或 APP_ID/APP_SECRET 错 |
| 日志出现 `Invalid Session` / op=9 | 机器人还在沙箱模式，去开放平台发布上架 |
| 401 / 鉴权失败 | APP_SECRET 错了；或出口 IP 没加白名单 |
| 能收消息但不回 | API 三件套没配或欠费，日志里会留 `api:` 错误链 |
| 回复是兜底文案 | 大脑全部后端失败，查 receiver.log 里的错误链 |
| `useradd: group qqbot exists` | 重跑过脚本，正常，脚本幂等 |

## 没有服务器？旧手机 termux 替代路径

Android 旧机 + Termux 也能 7×24（没 systemd，用 termux-boot 开机自启）：

```bash
pkg install python termux-api    # termux-api 可选
pkg install termux-boot
# 装 termux-boot 的 Autostart 插件（F-Droid），然后：
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-qqbot.sh <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
cd ~/qqbot
while true; do
  python3 qq_receiver.py >> receiver.log 2>&1
  sleep 5
done
EOF
chmod +x ~/.termux/boot/start-qqbot.sh
```

手机路径注意：① 系统设置里给 Termux 加电池白名单，否则会被 LMK 杀；
② 出口 IP 是移动网络动态 IP，白名单要跟着换，不如 VPS 省心。
termux 下 `while true` 循环即保活，**不要**再跑 `qq_watchdog.sh`（重复拉起）。

## 设计说明

- **为什么不用 qq_watchdog.sh**：watchdog 是 nohup 时代的产物，systemd 的
  `Restart=always` 就是保活。两层保活会重复拉起进程，日志刷屏。
- **为什么排除 *.json**：`qq_profiles.json`（用户记忆）/`chat_log.jsonl`（聊天记录）
  是沙箱资产，不带 server 上从零攒。要迁移：scp 过去后
  `chown qqbot:qqbot`，再 `systemctl restart qqbot`。
- **为什么只装 websocket-client**：`qq_brain.py` 的 torch/transformers 全是函数内
  延迟导入，API 模式下服务器无需 2GB 依赖。
