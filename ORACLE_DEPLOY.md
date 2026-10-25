# Oracle Cloud 永久免费 VPS 部署指南（7×24 常驻）

> 定位：把机器人从沙箱搬到 Oracle Cloud Always Free 实例，固定 IP、永不休眠、
> systemd 保活。全程手机浏览器可完成（走 OCI 控制台自带的 Cloud Shell，
> 不需要在手机上生成 SSH 密钥）。
>
> **三个硬前置**（缺一会卡住）：
> 1. **DeepSeek（或其它 OpenAI 兼容）API Key**——服务器不跑本地模型，大脑走云 API
> 2. **一张能验证的信用卡**（Oracle 注册用，虚拟卡/预付卡常被拒；只验证不扣费）
> 3. **代码在 GitHub 上有仓库**（公开私有皆可；本沙箱仓库没有配置远程，需先推上去）

## 0. 先更正一个数字

Always Free 的 ARM 额度现行政策是 **2 OCPU + 12GB 内存**（旧政策的 4C/24GB 已下调，
很多老教程数字是过期的）。AMD micro 是 **1/8 OCPU + 1GB × 2 台**。
对本项目：**API 模式下机器人内存占用约 100-150MB，1 台 AMD micro 就够**；
ARM 留给以后想跑本地模型或加别的服务时再说（且 ARM 有容量抽签，见下）。

## 1. 注册（约 15 分钟，手机浏览器）

1. 打开 `signup.oraclecloud.com`，填邮箱/手机（SMS 验证）
2. **Home Region（家乡区）一旦选定永久不能改**，且 Always Free 资源只能建在家乡区。
   国内用户建议：**Japan East (Tokyo)** 或 **Japan Central (Osaka)**（离 QQ 网关和
   DeepSeek 都近，延迟 50-100ms，websocket 无感）
3. 绑卡验证。通过后进入 30 天试用（送额度）——**试用期内不要建付费规格的资源**，
   试用结束后付费资源会被停，Always Free 资源不受影响
4. 试用结束账号自动转为 Always Free 模式，免费资源**永久可用**

## 2. 创建实例（约 5 分钟）

控制台菜单：**Compute → Instances → Create instance**

| 配置项 | 选法 |
|------|------|
| Image | **Ubuntu 24.04**（install.sh 验证过 22.04/24.04） |
| Shape | 先试 **AMD Micro (VM.Standard.E2.1.Micro)**——1/8 OCPU/1GB，免费，一般有容量 |
| SSH key | 见下方 Cloud Shell 路径（**不要在手机上管理密钥文件**） |
| Public IP |  Assign a public IPv4 address ✔（分配后别再释放，重分配会变 IP） |
| Boot volume | 默认 47GB 起，够用（Always Free 总共 200GB 块存储） |

**ARM 彩蛋（可选）**：想拿 2C/12GB 就把 Shape 改成 Ampere A1 Flex（OCPU=2, RAM=12GB）。
但 ARM 有"容量抽签"——报 `out of host capacity` 就换可用区 (AD) 多重试几次，
抢不到就先用 AMD micro，机器人跑得动。

**SSH 密钥的手机最优路径**（不用 Termius、不传 .key 文件）：
1. 控制台右上角打开 **Cloud Shell**（浏览器里的终端，已登录你的租户）
2. 在 Cloud Shell 里执行：`ssh-keygen -t ed25519 -f ~/.ssh/oci_key -N ""`
3. `cat ~/.ssh/oci_key.pub` 复制公钥，粘到创建实例页面的 SSH key 框里
4. 实例创建好后，以后随时在 Cloud Shell 里：`ssh -i ~/.ssh/oci_key ubuntu@<公网IP>`

## 3. 实例基础设置（SSH 进实例后）

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip curl
pip3 install --user websocket-client   # 唯一第三方依赖（install.sh 也会兜底装）
```

防火墙说明（**两个地方，都默认只放行 SSH，正合意**）：
- 云侧 VCN 安全列表：默认放行 22 端口。想更稳可在安全列表把 22 限定为你自己的 IP
  （手机网络 IP 会变，嫌烦就保持 0.0.0.0/0 + 密钥认证，Ubuntu 镜像默认禁密码登录）
- 实例内 iptables：Oracle 的 Ubuntu 镜像自带规则只放行 22。**机器人只需出站 443，
  不需要开任何额外入站端口，不要动它**

## 4. 拉代码

在实例上（Cloud Shell 或 SSH 会话）：

```bash
cd ~
# 公开仓库：
git clone https://github.com/<你的用户名>/<仓库名>.git
# 私有仓库：GitHub → Settings → Developer settings → Fine-grained token
#（只给这个仓库 read 权限），然后：
git clone https://<token>@github.com/<你的用户名>/<仓库名>.git
```

> 本沙箱仓库尚未配置 GitHub 远程。**下一步行动：先在 GitHub 建仓库并把代码推上去**
> （沙箱里已备好备份 bundle，可直接 push）。推之前确认 `.env`、`qq_*.json` 在
> .gitignore 里（仓库已配置好，不会误传凭证）。

## 5. 配置 .env（API 大脑模式）

```bash
cd ~/<仓库名>
cp .env.example .env
nano .env
```

必须填/改的五项：

```
APP_ID=<QQ开放平台的机器人AppID>
APP_SECRET=<对应Secret>
QQ_BRAIN_MODEL=api
QQ_BRAIN_API_BASE=https://api.deepseek.com/v1
QQ_BRAIN_API_KEY=sk-<你的DeepSeek key>
QQ_BRAIN_API_MODEL=deepseek-chat
```

> 免费大脑备选：智谱 `glm-4-flash`（base `https://open.bigmodel.cn/api/paas/v4`，真有免费额度）。

## 6. 迁移数据（花名册/申请/订单/管理员缓存）

数据文件小（几 KB），**base64 粘贴即可，不用 scp**。打开我给你的
`qqbot-oracle-data-b64.txt`，在实例的 SSH 会话里**逐段粘贴执行**（共 3 段）。
粘贴完 `qq_staff.json` / `qq_admins.json` 就落在 `/opt/qqbot/` 下。

## 7. systemd 部署

```bash
sudo bash deploy/install.sh
```

脚本自动完成：建 `qqbot` 系统用户 → 白名单拷代码到 `/opt/qqbot`（只带 `qq_*.py`，
模型/日志/数据一概不带）→ 装 websocket-client → 检查 .env → 装 unit →
`systemctl enable --now qqbot`。

> 注意：install.sh 不动已存在的 .env 和数据 json，重复执行安全（幂等）。
> 但它**不会**帮你填 .env——第 5 步填的是仓库目录的 .env，会被拷过去；
> 如果已经先跑了 install.sh，就直接编辑 `/opt/qqbot/.env` 再 `systemctl restart qqbot`。

## 8. 【关键】防空闲回收 cron

**这是 Oracle 免费档独有的坑，不防可能整台机器被收走。** 官方政策：7 天窗口内
CPU p95、网络、内存（仅 A1）全部低于 20% 的 Always Free 实例可能被回收。
机器人平时正是"什么都不干"的形态，必须制造最低限度的活体活动：

```bash
# 推荐：healthchecks.io 免费注册一个 check，拿到 UUID（顺带获得掉线告警能力）
sudo tee /etc/cron.d/qqbot-keepalive >/dev/null <<'EOF'
*/5 * * * * root curl -fsS -m 10 -o /dev/null https://hc-ping.com/把这里换成你的UUID; python3 -c "sum(i*i for i in range(200000))" >/dev/null 2>&1
EOF
```

不想注册就用任意稳定外网端点代替 hc-ping（如 `https://www.baidu.com`），
效果一样（产生网络+CPU 活动），只是没有告警。

## 9. QQ 开放平台加 IP 白名单

```bash
curl -s ifconfig.me    # 在实例上跑，拿固定出口 IP
```

把 IP 填到 `bot.qq.com` → 你的机器人 → 开发设置 → IP 白名单。
**VPS 是固定 IP，加一次就够**——这是 VPS 路线相对手机部署的最大优势。

## 10. 切换上线（先验证，后断旧）

1. 实例上 `systemctl status qqbot` 显示 `active (running)`
2. `tail -f /opt/qqbot/receiver.log` 看到 WebSocket 连接成功
3. QQ 私聊机器人发条普通消息，确认秒回；再试 `入职陪玩` 确认审核系统在拦题
4. **全部通过后**，才停沙箱实例（同一 APP_ID 两个接收器会互抢会话）。
   沙箱停法：在沙箱终端 `pkill -f qq_receiver.py`
5. 沙箱实例保留为灾备：VPS 任何时刻出问题，停 VPS 起沙箱即可回切

## 11. 日常运维

```bash
systemctl status qqbot          # 状态
systemctl restart qqbot         # 改代码/.env 后重启
journalctl -u qqbot -f          # 实时日志
tail -f /opt/qqbot/receiver.log # 应用日志（同内容）
```

**更新代码**：`cd ~/<仓库名> && git pull && sudo bash deploy/install.sh`（.env 和数据不动）。

**备份数据**（重要——实例可能被回收/误删，数据只存在于本地磁盘）：
```bash
# 最简单：每周把两个 json 拉回本地（在你自己的电脑上跑）
scp -i <密钥> ubuntu@<VPS-IP>:/opt/qqbot/qq_staff.json ./backup/
scp -i <密钥> ubuntu@<VPS-IP>:/opt/qqbot/qq_admins.json ./backup/
```
进阶：Oracle Object Storage 免费 20GB，可配 rclone 定时同步。

## 12. 故障对照表

| 现象 | 原因 / 处理 |
|------|-------------|
| `activating (auto-restart)` 循环 | 九成是 .env 没填/APP_ID 错：`journalctl -u qqbot -n 50` |
| 日志 `Invalid Session` / op=9 | 机器人还在沙箱模式，去开放平台发布上架 |
| 401 / 鉴权失败 | Secret 错，或出口 IP 没加白名单（第 9 步） |
| 能收消息但不回 | API 三件套没配或欠费，日志里有 `api:` 错误链 |
| 连不上 SSH | 云侧安全列表和实例内 iptables 两个地方都只放行 22；确认用 ubuntu 用户 + 私钥 |
| 创建实例报 `out of host capacity` | ARM 容量抽签未中；换可用区重试，或改用 AMD micro |
| 收到 Oracle 回收通知邮件 | 第 8 步 cron 没生效；检查 `/etc/cron.d/qqbot-keepalive` 是否存在 |

## 13. 费用边界（怎么保证不花一分钱）

- Always Free 资源**只在家乡区免费**——创建时确认资源带 Always Free-eligible 标签
- 不要升级 Pay As You Go（除非要绕 ARM 容量抽签；免费额度不变，但超额会计费）
- 试用期建的付费资源，试用结束会被停——一开始就只用 Always Free 规格
- 出站流量 10TB/月免费，本项目的量可以忽略
- 设一个预算告警双保险：控制台 → Billing → Budgets，阈值 $1
