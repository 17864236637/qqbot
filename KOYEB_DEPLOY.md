# Koyeb 部署清单（2026-10 修订：免费档已不适用，慎读）

> **重大变更（2026-02-17）**：Koyeb 已与 Mistral AI 签署最终收购协议，团队并入
> Mistral Compute。官方公告明确：
> - **新用户必须绑定支付方式并订阅付费档才能开通**（Starter 起，无保底消费但需验证卡）
> - 平台将「聚焦 Pro 及以上客户」，未来数月逐步转型为 Mistral Compute 的核心组件，
>   重心转向 Inference / Sandboxes / MCP Servers
>
> **对本项目的致命一点**：Koyeb 免费实例（512MB/0.1vCPU/2GB，仅 Frankfurt/DC）
> **空闲 1 小时即缩容到零，冷启动唤醒**。而 QQ 机器人是**主动向 QQ 网关建连的
> WebSocket 客户端，没有任何入站流量**——缩容到零后没有任何请求能唤醒它，
> 等于永久下线直到手动重启。**免费实例对常驻机器人不可用，这不是钱的问题。**
>
> **结论：7×24 常驻不要选 Koyeb。** 推荐顺序：
> 1. **Oracle Cloud 永久免费 VPS**（ARM 4核24GB / AMD 1GB×2，永不休眠，
>     本仓库 `deploy/install.sh` + `deploy/qqbot.service` 就是为它准备的）
> 2. **安卓 Termux 本地部署**（零成本，见 `PHONE_DEPLOY.md`，物料已备齐）
> 3. 愿意付费且接受平台转型风险：Koyeb Starter + eco 实例（$1.61/月起，
>     **必须设置 min instances=1 关闭 scale-to-zero**，否则同样休眠）
>
> 以下内容保留供付费路线参考。

---

## 1. API Token 获取（仅编程管理服务才需要）

控制台点鼠标部署**不需要任何 token**。API Token 路径：
**Settings → API 标签页 → Create API token**（token 只显示一次，立即保存）。

## 2. 付费路线：控制台建服务

1. 登录 [app.koyeb.com](https://app.koyeb.com) → Create Service → GitHub 授权 → 选仓库
2. Builder 选 **Dockerfile**（仓库根目录已备好，实测构建通过，镜像约 140MB）
3. Instance 选 **eco**（$1.61/月，共享 CPU）；**Scaling 设置 min instances=1**
   （默认 scale-to-zero 会让机器人休眠后永久掉线，见顶部警告）
4. **Ports 留空**——WebSocket 客户端，无入站端口
5. 注册时注意：官方会做 **$29 预授权冻结**并**立即按比例收取所选套餐费用**，
   选 Starter（无保底）可立即降级；支付验证有时需数个工作日

## 3. 环境变量（Koyeb 控制台注入）

| Key | Value | 说明 |
|-----|-------|------|
| `APP_ID` | 机器人 AppID | QQ 开放平台 |
| `APP_SECRET` | 机器人 Secret | 标 Secret |
| `BOT_NAME` | `QQ监督员` | 可选 |
| `QQ_BRAIN_MODEL` | `api` | 强制 API 大脑 |
| `QQ_BRAIN_API_BASE` | `https://api.deepseek.com/v1` | API 厂商 |
| `QQ_BRAIN_API_KEY` | `sk-...` | 标 Secret |
| `QQ_BRAIN_API_MODEL` | `deepseek-chat` | 模型名 |

## 4. 风险清单

| # | 风险 | 说明 |
|---|------|------|
| 1 | **平台转型** | 收购交割中，重心转向 Mistral Compute（推理/沙箱/MCP），通用 App 托管长期政策未知，随时可能再变 |
| 2 | **scale-to-zero 陷阱** | eco 实例默认也缩容；必须显式 min instances=1，改一次部署后要复查 |
| 3 | **磁盘临时** | 重启/重部署后 `qq_staff.json`（花名册/申请/订单）清零，当前在册仅 1 人损失可接受 |
| 4 | **出口 IP 不固定** | QQ 开放平台若开 IP 白名单会反复失效；建议关白名单（Secret 本身就是凭证） |
| 5 | **双实例互踢** | 同 APP_ID 两个接收器互抢会话；Koyeb 上线=沙箱/手机下线，永远只跑一个 |

## 5. 为什么 Oracle Cloud 永久免费是更优解

- **真·永久免费**（非试用）：2× AMD VM（1 OCPU/1GB）或 4× ARM Ampere 核 + 24GB 内存
- **永不休眠**：没有 scale-to-zero 这种坑，机器人长连接稳定
- **完整 root**：`deploy/install.sh` 一键装 systemd 常驻服务，日志用 `journalctl` 看
- 24GB ARM 甚至能塞个小本地模型（可选，非必须）
- 代价：注册需信用卡验证（仅验证不扣费）；ARM 机型热门区可能缺容量需重试；
  系统安全自己负责（机器人用非 root 用户跑 + 只开必要端口）
