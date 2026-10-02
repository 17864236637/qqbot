# QQ 监督员机器人（积分督学版）

一个能听能回、带「积分制督学」的 QQ 机器人。支持私聊 + 群聊双向对话，主动定时提醒，
以及基于规则自动扣分、群内存档的积分系统。

## 功能

- **双向对话**：私聊（C2C）与群 @消息，用本地大模型生成回复（不依赖外部 LLM API）。
- **积分制督学**：每人初始 10 分、每月重置；违纪自动扣分（刷屏/骂人/宵禁/惹事/写作业摸鱼），
  违规记录与惩罚账目群内/本地存档；分数 ≤3 触发「开会」通知。
- **定时提醒**：每天 8:00 / 21:00 提醒报备手机时长（由 receiver 后台线程驱动，不加载模型）。
- **多模型后端**：1.5B（默认，快）、0.5B（最小最快、抽风、适合恶搞）、7B-GGUF（质量最好、最慢）。

## 架构

```
qq_watchdog.sh   看门狗：30s 保活 receiver（+SUPERVISE=on 时拉起 superviser）
qq_receiver.py   接收端：WebSocket 长连接，收到消息→违纪检测→扣分→7B 播报；含积分定时线程
qq_superviser.py 监管端：主动定时推送（SUPERVISE=on 时启用）
qq_brain.py      模型推理：GGUF(7B) → 1.5B → 0.5B → 7B(transformers) 优先级，由 QQ_BRAIN_MODEL 切换
qq_scoreboard.py 积分账簿：JSON 持久化、扣分、每月重置、快照
qq_rules.py      违纪规则：刷屏/脏话/宵禁/惹事检测 + 写作业报备状态机（纯规则，不调模型）
qq_bot.py        QQ 开放平台 HTTP API 客户端（获取 access_token、发群消息）
```

> 沙箱 8GB 内存限制下，7B 只能单进程运行（会占满内存）。若要 7B 对话，请在 .env 设
> `SUPERVISE=off`（关闭定时推送）以腾出内存。详见「配置」。

## 配置（.env）

参考 `.env.example` 创建 `.env`：

```
APP_ID=你的_appid
APP_SECRET=你的_client_secret
BOT_NAME=QQ监督员
# 大脑模型：1.5b(默认) / 0.5b(最小最快) / 7b(质量最好最慢)
QQ_BRAIN_MODEL=1.5b
# 定时推送：on(开启) / off(仅对话，省内存给 7B 用)
SUPERVISE=on
```

## 部署步骤

```bash
bash deploy.sh          # 安装依赖 + 启动看门狗
# 或手动：
pip3 install -r requirements.txt
bash qq_watchdog.sh
```

## ⚠️ 关键坑：IP 白名单（必读）

QQ 开放平台要求调用方服务器 IP 在 **IP 白名单** 内，否则返回 `401 / code:11298`。
**如果你的部署环境出口 IP 不固定（如某些沙箱、弹性容器），请把所有会出现的出口 IP 全部加入白名单。**
若平台允许，直接「不限制 IP / 填 0.0.0.0/0」最省事。

> 经验：动态 IP 环境每次会话 IP 可能变化，必须加全所有历史 IP，否则一飘就 401 重连死循环。

## 模型权重

仓库**不含**模型权重（过大）。部署时需自行下载到对应目录：

| 模型 | 目录 | 获取 |
|------|------|------|
| Qwen2.5-1.5B-Instruct | `qwen15_model/` | ModelScope / HuggingFace |
| Qwen2.5-0.5B-Instruct | `qwen_model/` | ModelScope / HuggingFace |
| Qwen2.5-7B-Instruct (GGUF Q4_K_M) | `qwen7b_gguf/` | 自行转换或用社区 GGUF |

## 积分系统说明

- 状态存于 `qq_scoreboard.json`（不入库，属运行数据）。
- 触发扣分：在群里 @机器人 发言即触发违纪检测；问「我还有几分？」可查分。
- 写作业流程：发「开始写作业」→ 期间发言算摸鱼（扣 1）→ 发「写完作业」结束（超时未提交扣 2）。
- 每月 1 日自动重置；任意成员 ≤3 分时自动发「开会」通知。
