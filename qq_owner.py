#!/usr/bin/env python3
"""主人绑定：单一主人的身份判定与称谓。

QQ 开放平台不下发真实 QQ 号，只给 openid（32 位十六进制字符串），
所以主人只能按 openid 绑定，填 QQ 号无效。抓 openid 的办法：让对方在
群里说一句话，然后从 receiver.log 的事件里捞 author.member_openid。

配置（.env）：
    QQ_OWNER_OPENID=B5614B1C7B56CA04056D266F3E3C2F55
    QQ_OWNER_NAME=桉柠

主人享有的四项特权：
    1. 人设归属——提示词里写明「你是 XX 的专属监督搭子」，换主人只改 .env
    2. 私聊管理命令——主人私聊也能发管理命令，不依赖群角色缓存
    3. 免疫监督推送——定时监督跳过主人，只催别人
    4. 独占控制台——拉黑/解黑这类重罚命令只认主人，普通管理员挡掉

安全边界不变：陌生人私聊发任何管理命令照样被挡。
"""
from __future__ import annotations

import os

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE, ".env")

# 未配置主人时的人设兜底名（与历史硬编码一致，保证不配也能跑）
DEFAULT_OWNER_NAME = "桉柠"


def load_env() -> None:
    """把 .env 灌进 os.environ。用 setdefault：进程已有同名环境变量时不覆盖。"""
    if not os.path.exists(ENV_FILE):
        return
    for line in open(ENV_FILE, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


# 模块级读一次：任何地方 import qq_owner 都能直接拿到配置，不依赖调用方
# 记得先 load_env。setdefault 保证幂等，重复调用无害。
load_env()


def owner_openid() -> str:
    """主人的 openid；未配置返回空串（此时主人机制整体不生效）。"""
    return (os.environ.get("QQ_OWNER_OPENID") or "").strip()


def owner_name() -> str:
    """主人的称呼；未配置回退到 DEFAULT_OWNER_NAME，行为与改动前一致。"""
    return (os.environ.get("QQ_OWNER_NAME") or "").strip() or DEFAULT_OWNER_NAME


def is_owner(openid: str | None) -> bool:
    """判定 openid 是否为主人。空值一律不算——避免匿名事件误判成主人。"""
    oid = owner_openid()
    return bool(oid) and bool(openid) and openid.strip() == oid


def describe() -> str:
    """给查询命令用的一句话绑定状态。"""
    oid = owner_openid()
    if not oid:
        return "还没绑定主人（.env 里缺 QQ_OWNER_OPENID）"
    return f"我的主人是「{owner_name()}」（{oid[:8]}…）"


if __name__ == "__main__":
    print(describe())
