#!/usr/bin/env python3
"""自定义指令：关键词触发的自定义回复，支持一次性、有效期、自动撤回。

设计的几条规矩：
1. 内置命令优先——自定义指令只做兜底，不许覆盖"入群/花名册/点单"这些，
   否则管理员改坏一条就把核心流程堵死了。
2. 群聊必须 @机器人 或带 / 前缀才触发；私聊必须 / 前缀。
   裸关键词触发会让机器人变成话痨，人聊到"打卡"两个字就被插嘴。
3. 权限分层：管理员可增，删除（含改动）收归主人。
4. 清理走惰性扫描——有消息进来时顺手做，不额外起定时器。沙箱随时被回收，
   常驻定时器活不过一次回收，惰性是最稳的。

数据落在 qq_cmds.json（含创建者 openid，属隐私，已在 .gitignore 里排除）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CMD_FILE = os.path.join(BASE, "qq_cmds.json")

_lock = threading.Lock()

# 一次性/过期指令的扫描频率上限：每条消息都扫一遍文件太浪费
_SWEEP_INTERVAL = 60
_last_sweep = 0.0

_MAX_KEYWORD = 20      # 关键词长度上限，防手滑塞进一整句话
_MAX_REPLY = 800       # 回复体长度上限
_MAX_CMDS = 200        # 指令总数上限，防灌爆文件


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load() -> dict:
    try:
        with open(CMD_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {"version": 1, "cmds": {}}
    if not isinstance(d, dict) or "cmds" not in d:
        return {"version": 1, "cmds": {}}
    return d


def _save(d: dict) -> None:
    tmp = CMD_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CMD_FILE)   # 原子替换：写一半崩了不会留半个文件


def _norm_key(k: str) -> str:
    """关键词归一化：去空白、去首尾斜杠，"打卡"和"/打卡"算同一条。"""
    return (k or "").strip().lstrip("/").strip()


# ---------- 查询 ----------
def get(keyword: str) -> dict | None:
    return _load()["cmds"].get(_norm_key(keyword))


def all_cmds() -> dict:
    return _load()["cmds"]


def match(text: str) -> tuple[str, dict] | None:
    """文本 -> (关键词, 指令) 命中则返回，否则 None。整条消息精确匹配。"""
    key = _norm_key(text)
    if not key:
        return None
    cmd = _load()["cmds"].get(key)
    return (key, cmd) if cmd else None


# ---------- 增 / 删 ----------
def _future(minutes: int) -> str:
    """当前时间 + minutes 分钟的时间串。"""
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() + minutes * 60))


def add(keyword: str, reply: str, by: str = "", by_name: str = "",
        once: bool = False, ttl_min: int = 0, autodel: int = 0,
        scope: str = "any") -> tuple[bool, str]:
    """新增指令。返回 (是否成功, 提示文案)。"""
    key = _norm_key(keyword)
    if not key:
        return False, "关键词不能为空"
    if len(key) > _MAX_KEYWORD:
        return False, f"关键词太长（上限 {_MAX_KEYWORD} 字），别把整句话塞进去"
    if not (reply or "").strip():
        return False, "回复内容不能为空"
    if len(reply) > _MAX_REPLY:
        return False, f"回复内容太长（上限 {_MAX_REPLY} 字）"
    if autodel and not (1 <= autodel <= 120):
        return False, "自动撤回延迟要在 1~120 秒之间"
    with _lock:
        d = _load()
        if key in d["cmds"]:
            return False, f"指令「{key}」已存在，要改的话先删掉再加"
        if len(d["cmds"]) >= _MAX_CMDS:
            return False, f"指令数量到上限了（{_MAX_CMDS} 条），先清理一些"
        d["cmds"][key] = {
            "reply": reply.strip(),
            "once": once,
            "expires": (_future(ttl_min) if ttl_min else ""),
            "autodel": autodel or 0,
            "scope": scope if scope in ("any", "admin") else "any",
            "by": by or "",
            "by_name": by_name or "",
            "ts": _now(),
            "hits": 0,
        }
        _save(d)
    extra = []
    if once:
        extra.append("用一次自动销毁")
    if ttl_min:
        extra.append(f"{ttl_min} 分钟后过期")
    if autodel:
        extra.append(f"{autodel} 秒后自动撤回")
    tail = f"（{'，'.join(extra)}）" if extra else ""
    return True, f"已添加指令「{key}」{tail}"


def remove(keyword: str) -> tuple[bool, str]:
    key = _norm_key(keyword)
    with _lock:
        d = _load()
        if key not in d["cmds"]:
            return False, f"没有叫「{key}」的指令"
        del d["cmds"][key]
        _save(d)
    return True, f"已删除指令「{key}」"


def hit(key: str) -> None:
    """记一次命中；一次性指令用完即焚。"""
    with _lock:
        d = _load()
        cmd = d["cmds"].get(key)
        if not cmd:
            return
        cmd["hits"] = int(cmd.get("hits", 0)) + 1
        if cmd.get("once"):
            del d["cmds"][key]
        _save(d)


# ---------- 惰性清理 ----------
def sweep(force: bool = False) -> int:
    """清掉已过期的指令，返回清理条数。

    每条消息都扫太浪费，默认 60 秒一次。不用常驻定时器：沙箱随时被回收，
    线程活不过一次回收。
    """
    global _last_sweep
    now = time.time()
    if not force and now - _last_sweep < _SWEEP_INTERVAL:
        return 0
    _last_sweep = now
    with _lock:
        d = _load()
        now_s = _now()
        dead = [k for k, c in d["cmds"].items()
                if c.get("expires") and str(c["expires"]) <= now_s]
        if not dead:
            return 0
        for k in dead:
            del d["cmds"][k]
        _save(d)
    return len(dead)


def list_text() -> str:
    d = _load()["cmds"]
    if not d:
        return "还没有自定义指令。管理员用「/加指令 关键词 | 回复内容」添加。"
    lines = [f"共 {len(d)} 条自定义指令："]
    for k, c in sorted(d.items()):
        tags = []
        if c.get("once"):
            tags.append("一次性")
        if c.get("expires"):
            tags.append(f"过期于 {c['expires']}")
        if c.get("autodel"):
            tags.append(f"{c['autodel']}s 撤回")
        if c.get("scope") == "admin":
            tags.append("仅管理员")
        tail = f"（{'，'.join(tags)}）" if tags else ""
        preview = c.get("reply", "")
        preview = preview if len(preview) <= 24 else preview[:24] + "…"
        lines.append(f"• /{k} → {preview}{tail}")
    return "\n".join(lines)


# ---------- 管理命令解析 ----------
_ADD_RE = re.compile(r"^加指令\s+(.+?)\s*\|\s*(.+)$", re.S)
_DEL_RE = re.compile(r"^删指令\s+(\S+)\s*$")
_LIST_WORDS = ("指令列表", "自定义指令", "指令")


def parse_manage(text: str) -> tuple[str, dict] | None:
    """解析管理类命令，返回 (动作, 参数)。动作：add/del/list。"""
    t = (text or "").strip().lstrip("/").strip()
    if t in _LIST_WORDS:
        return ("list", {})
    m = _DEL_RE.match(t)
    if m:
        return ("del", {"keyword": m.group(1)})
    m = _ADD_RE.match(t)
    if m:
        return ("add", {"keyword": m.group(1).strip(), "rest": m.group(2)})
    return None


_OPT_RE = re.compile(
    r"\s+(once|一次性|admin|仅管理|仅管理员|ttl=\S+|autodel=\S+)$", re.I)


def parse_add_opts(keyword: str, rest: str) -> tuple[bool, str, dict]:
    """把"打卡 | 内容 once ttl=60 autodel=5"拆成参数。

    整段一律先当正文，只从**尾部**反复剥离开关。早先版本按 | 切段再取末段
    当开关，结果正文里的管道符被吃掉（"明早交 | 别忘了" 变成 "明早交|别忘了"，
    空格也丢了）。明确写了 ttl=/autodel= 但值不是正整数时直接报错——
    静默当成正文会让人以为设了过期，其实把话写进了回复里。
    """
    body = (rest or "").strip()
    once, ttl, autodel, scope = False, 0, 0, "any"
    touched = False
    while True:
        m = _OPT_RE.search(body)
        if not m:
            break
        touched = True
        tok = m.group(1).lower()
        body = body[:m.start()].rstrip()
        if tok in ("once", "一次性"):
            once = True
        elif tok.startswith("ttl="):
            v = tok[4:]
            if not (v.isdigit() and int(v) > 0):
                return False, "ttl 要填正整数（分钟）", {}
            ttl = int(v)
        elif tok.startswith("autodel="):
            v = tok[8:]
            if not (v.isdigit() and int(v) > 0):
                return False, "autodel 要填正整数（秒）", {}
            autodel = int(v)
        elif tok in ("admin", "仅管理", "仅管理员"):
            scope = "admin"
    if touched:
        # 剥掉开关后可能留下语法性的悬空分隔符："闹钟 | once" -> "闹钟 |"
        body = body.rstrip().rstrip("|").rstrip()
    if not body:
        return False, "回复内容不能为空", {}
    return True, "", {"keyword": keyword, "reply": body, "once": once,
                      "ttl_min": ttl, "autodel": autodel, "scope": scope}
