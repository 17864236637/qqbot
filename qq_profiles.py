#!/usr/bin/env python3
"""用户身份记忆：自动识别并持久化用户名/昵称，供督学对话时 call 名字。

名字来源：
- 来源1：QQ 下发的 author.nickname（群消息通常有）
- 来源2：对话内的自我介绍（"我是X" / "叫我X" / "我的名字是X" ...）
存储：qq_profiles.json，按 openid 索引，线程安全读写。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
PROFILE_FILE = os.path.join(BASE, "qq_profiles.json")
_lock = threading.Lock()

# 出现在提取结果里说明这不是真名（而是动作/描述/疑问词/代词）
_NAME_BAD = set(
    "的来去了在要想到学做看玩吃睡说听写读上把给对很太最正想该应可以这那什么怎为被让叫"
    "啊呢吧哦嘛呀哇哎哟啦咯呐喂滴个着过儿们谁他她它你我是怎哪本"
    "就不还有几分没更也才都会能别再将又已再"
)
_TAIL = re.compile(r"[吧啊呢哦嘛呀哇哎哟啦咯呐喂滴个着过儿们~]+$")

_PATTERNS = [
    # 叫我X / 你可以叫我X / 大家叫我X / 你们叫我X（"叫"必选，禁止裸"我X"误匹配）
    re.compile(r"(?:叫我|你(?:可|能)?以?叫?我|大家叫?我|你们叫?我|都叫?我)[是:：\s]*([\u4e00-\u9fffA-Za-z0-9_\-]{1,12})"),
    # 我的名字(是|叫)X / 名字叫X / 昵称是X / 本名X
    re.compile(r"(?:我(?:的)?名字|昵称|本名)[是叫:：\s]+([\u4e00-\u9fffA-Za-z0-9_\-]{1,12})"),
    # 我是X（句中也可："啊呸，我是稜秎"）
    re.compile(r"我是([\u4e00-\u9fffA-Za-z0-9_\-]{1,12})"),
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean(raw: str):
    """清洗提取到的名字片段，无效返回 None。"""
    if not raw:
        return None
    name = raw.strip(" ，,。！？!?~、；;\t　")
    name = _TAIL.sub("", name)
    name = name.strip(" ，,。！？!?~、；;\t　")
    if not (1 <= len(name) <= 12):
        return None
    if name.isdigit():
        return None
    if any(c in _NAME_BAD for c in name):
        return None
    return name


def extract_self_intro(text: str):
    """从一句话里识别'我是X/叫我X/名字是X'，返回名字或 None。"""
    if not text:
        return None
    for pat in _PATTERNS:
        m = pat.search(text)
        if m:
            nm = _clean(m.group(1))
            if nm:
                return nm
    return None


def load() -> dict:
    try:
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(data: dict) -> None:
    with _lock:
        tmp = PROFILE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PROFILE_FILE)


def remember(openid, name, scope=None, note=None):
    """记录/更新某 openid 的名字，返回最终使用的名字（无效则 None）。"""
    if not openid or not name:
        return None
    name = _clean(name)
    if not name:
        return None
    data = load()
    rec = data.get(openid, {})
    rec["name"] = name  # 以最新自称/昵称为准
    rec["last_seen"] = _now()
    if scope:
        rec["scope"] = scope
    if note:
        rec.setdefault("note", note)
    if "first_seen" not in rec:
        rec["first_seen"] = _now()
    data[openid] = rec
    save(data)
    return name


def get_name(openid):
    return load().get(openid, {}).get("name")


def apply_author(openid, author, scope=None):
    """用 QQ 下发的昵称兜底（群消息常有）。无则 None。"""
    if not openid or not author:
        return None
    nick = author.get("nickname") or author.get("user_name")
    if nick:
        return remember(openid, nick, scope)
    return None
