# 安卓手机 Termux 常驻部署（7×24 版）

目标：让 QQ 机器人在安卓手机上长期在线。手机只用 **API 大脑模式**
（`QQ_BRAIN_MODEL=api`）——本地 GGUF 模型在手机上发热、慢、吃内存，不推荐。

## 0. 装 Termux

- 从 **F-Droid** 安装 Termux（应用商店版已停更，别装错）
- 可选：再装一个 Termux:Boot（F-Droid 同名）用于开机自启

## 1. 一条龙：装环境 + 解压代码

把 `qqbot-upload.zip` 保存到手机（通常在「下载」目录），然后在 Termux 里粘贴：

```bash
termux-setup-storage
pkg install python unzip nano -y
pip install websocket-client
mkdir -p ~/qqbot
unzip -o ~/storage/downloads/qqbot-upload.zip -d ~/qqbot
cd ~/qqbot
cp .env.example .env
```

## 2. 填配置

```bash
nano .env
```

按方向键移动，填这些（值去 QQ 开放平台 bot.qq.com → 你的应用 → 开发设置拿）：

| 字段 | 填什么 |
|---|---|
| `APP_ID` | 机器人 appId |
| `APP_SECRET` | 机器人 clientSecret |
| `GROUP_OPENID` | 目标群 openid（只影响定时推送，聊天不收影响） |
| `BOT_NAME` | 展示名，随意 |

大脑（方案 A，四行去掉注释填值）：

```
QQ_BRAIN_MODEL=api
QQ_BRAIN_API_BASE=https://api.siliconflow.cn/v1
QQ_BRAIN_API_KEY=你的key
QQ_BRAIN_API_MODEL=Qwen/Qwen2.5-7B-Instruct
```

免费 key 渠道：硅基流动注册送额度；GitHub Models 免费但限额严。
填完 `Ctrl+O` 回车保存，`Ctrl+X` 退出。

## 3. 启动

```bash
cd ~/qqbot
termux-wake-lock
nohup python3 qq_receiver.py > receiver.log 2>&1 &
tail -f receiver.log
```

看到 `会话就绪 bot_id=...` 就是连上了。`Ctrl+C` 停止看日志（不影响后台进程）。

## 4. 防杀设置（不做这个，半夜准掉）

安卓系统默认会杀后台，逐个关：

1. **设置 → 应用 → Termux → 电池 → 无限制**（或"允许后台运行"）
2. **最近任务卡片，下拉/长按 Termux 加锁**（🔒图标）
3. **允许自启动**（小米/华为/OV/vivo 都有这个开关，藏在各自的权限管理里）
4. 系统省电模式尽量不要开"深度睡眠/超级省电"

## 5. QQ 后台 IP 白名单（换网络必看）

机器人连 QQ 走 WebSocket 长连接，**出口 IP 必须在 QQ 开放平台的 IP 白名单里**：

- 查当前出口 IP：Termux 里 `curl ifconfig.me`
- 加到 bot.qq.com → 开发设置 → IP 白名单
- **WiFi 和移动数据是两个不同 IP**，切换网络后 IP 会变，不变就连不上（报 11298）
- 家用宽带重启光猫后 IP 也可能变

## 6. 开机自启（可选）

装了 Termux:Boot 后：

```bash
mkdir -p ~/.termux/boot
nano ~/.termux/boot/start-bot.sh
```

写入：

```bash
cd ~/qqbot
termux-wake-lock
nohup python3 qq_receiver.py > receiver.log 2>&1 &
```

```bash
chmod +x ~/.termux/boot/start-bot.sh
```

手机重启后会自动拉起。

## 故障对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| 日志报 11298 | 出口 IP 不在白名单 | `curl ifconfig.me`，把新 IP 加进 QQ 后台 |
| 日志报 100017 | 连接被限流 | 等几分钟重跑启动命令 |
| 进程莫名没了 | 被系统杀了 | 补第 4 节设置，重启 Termux 重跑启动命令 |
| 一直"收到消息"不回 | 大脑配置错/欠费 | 看 receiver.log 里大脑报错，检查 `.env` 的 API 三行 |
| 换地方旅游没网 | — | 手机到家连上 WiFi 自动恢复（IP 没变的话） |

## 已知限制

- **WebSocket 断线暂不会自动重连**：网络抖动后要重跑启动命令（后续版本会加看门狗）
- 没有 systemd 那种崩溃自拉起，靠 Termux 防杀 + Boot 兜底
- 手机睡眠/没电时机器人就是离线，这是手机方案的固有代价
