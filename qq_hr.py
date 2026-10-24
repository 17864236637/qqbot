#!/usr/bin/env python3
"""入职审核系统：陪玩 / 陪聊 / 树洞 / 代肝 / 监督员 五线入职。

定位（务必理解，别再跑偏）：
- 本模块管「人」的入职：申请 → 分线问卷 → 自动初筛 → 通过/拒绝 → 花名册。
  不管派单、不管经手资金（押金类岗位只做"知悉确认"，钱的事群主线下处理）。
- 命令拦截型：关键词命中即接管对话，不调用大模型，毫秒级、确定性强。
- 状态全部持久化到 qq_staff.json（进程会死，申请不能丢；原子写防半截文件）。

用法（群/私聊均可发起）：
  入职                列出岗位与用法
  入职<岗位>           开始该岗位问卷（如：入职陪玩）
  （私聊中逐题作答，格式错会要求重答）
  取消入职             放弃当前申请
管理命令（member_role=admin 限定）：
  入职列表 / 花名册 / 拉黑 <QQ或昵称> <原因> / 解黑 <QQ或昵称>

问卷题目在 ROLES/COMMON_QUESTIONS 里改文本即可，无需动逻辑。
自动初筛规则见 _evaluate：黑名单 → 重复在职 → 年龄门槛 → 格式（状态机已保证）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "qq_staff.json")

_lock = threading.Lock()

# ---------- 校验器：题目答案的格式关卡 ----------
VALIDATORS = {
    "nonempty": ("这项不能为空，重新答", lambda v: bool(v.strip())),
    "age": ("年龄请答 10-99 的数字", lambda v: v.isdigit() and 10 <= int(v) <= 99),
    "confirm": ("请回复「确认」或「拒绝」", lambda v: v.strip() in ("确认", "拒绝")),
    "any": ("", lambda v: True),
}

# ---------- 通用题（所有岗位都问） ----------
COMMON_QUESTIONS = [
    {"key": "age", "text": "第1题（通用）：你的年龄？", "validate": "age"},
    {"key": "contact", "text": "第2题（通用）：联系方式（QQ号或微信号）？",
     "validate": "nonempty"},
    {"key": "intro", "text": "第3题（通用）：一句话自我介绍", "validate": "nonempty"},
]

# ---------- 五条业务线：分线定制问卷 ----------
ROLES = {
    "陪玩": {
        "desc": "游戏陪玩，按局/按小时接单",
        "min_age": 18,
        "questions": [
            {"key": "game", "text": "你常玩什么游戏+区服？（如：王者荣耀/Q区）",
             "validate": "nonempty"},
            {"key": "rank", "text": "你的段位或水平？", "validate": "nonempty"},
            {"key": "hours", "text": "每天可接单时间段？（如 19:00-23:00）",
             "validate": "nonempty"},
        ],
    },
    "陪聊": {
        "desc": "文字/语音陪聊，倾听与吐槽",
        "min_age": 18,
        "questions": [
            {"key": "hours", "text": "你的在线时段？", "validate": "nonempty"},
            {"key": "topics", "text": "擅长聊什么？（倾听/吐槽/情感/游戏）",
             "validate": "nonempty"},
            {"key": "voice", "text": "文字还是语音陪聊？", "validate": "nonempty"},
        ],
    },
    "树洞": {
        "desc": "匿名树洞，倾听倾诉，内容保密",
        "min_age": 18,
        "questions": [
            {"key": "hours", "text": "你的在线时段？", "validate": "nonempty"},
            {"key": "topics", "text": "可倾听的倾诉类型？（情感/学业/职场/都行）",
             "validate": "nonempty"},
            {"key": "secret", "text": "保密承诺：树洞内容不外传、不截图、不议论，"
                                      "回复「确认」或「拒绝」", "validate": "confirm"},
        ],
    },
    "代肝": {
        "desc": "游戏代肝（每日委托/体力/活动等）",
        "min_age": 18,
        "questions": [
            {"key": "task", "text": "代肝什么游戏/什么内容？（如：原神每日委托+体力）",
             "validate": "nonempty"},
            {"key": "deposit", "text": "代肝需知悉押金与违约规则（押金金额、超时赔付"
                                      "由群主线下与你面议），回复「确认」或「拒绝」",
             "validate": "confirm"},
            {"key": "hours", "text": "每天可肝时长？", "validate": "nonempty"},
        ],
    },
    "监督员": {
        "desc": "学习监督，打卡检查+日报",
        "min_age": 16,
        "questions": [
            {"key": "identity", "text": "你的身份？（学生/家长/其他）",
             "validate": "nonempty"},
            {"key": "school", "text": "学校/年级 或 职业？", "validate": "nonempty"},
            {"key": "hours", "text": "可监督时段？（如 20:00-22:00）",
             "validate": "nonempty"},
            {"key": "style", "text": "督学方式确认：每日打卡检查+学习日报，"
                                     "回复「确认」或「拒绝」", "validate": "confirm"},
        ],
    },
}

ROLE_NAMES = list(ROLES.keys())


# ---------- 持久化 ----------
def _default_data() -> dict:
    # applications 按 openid 单槽（同时只允许一份进行中）；
    # history 是全量终态归档（approved/rejected/canceled），覆盖不丢痕。
    return {"blacklist": [], "applications": {}, "staff": {}, "history": []}


def _load() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = _default_data()
    d.setdefault("blacklist", [])
    d.setdefault("applications", {})
    d.setdefault("staff", {})
    d.setdefault("history", [])
    return d


def _save(d: dict) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)  # 原子替换，防进程死在写一半


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _questions_for(role: str) -> list:
    return COMMON_QUESTIONS + ROLES[role]["questions"]


# ---------- 关键词入口 ----------
def _norm(text: str) -> str:
    return (text or "").strip()


def parse_role(text: str) -> str | None:
    """从消息里解析岗位名：'入职陪玩' → '陪玩'；支持别名（拼音/缩写不搞，防误触）。"""
    t = _norm(text)
    m = re.match(r"^入职\s*([一-龥A-Za-z0-9]{1,6})$", t)
    if not m:
        return None
    return m.group(1) if m.group(1) in ROLES else None


def is_entry(text: str) -> bool:
    """是否入职系统的入口词（精确匹配，防日常聊天误触）。"""
    t = _norm(text)
    return t in ("入职", "入职列表", "花名册", "取消入职") \
        or parse_role(t) is not None \
        or t.startswith("拉黑") or t.startswith("解黑")


# ---------- 核心流程 ----------
def hr_handle(openid: str, name: str, text: str, is_admin: bool = False) -> str | None:
    """receiver 的接线点：命中入职系统返回文案，否则返回 None（放行给大模型）。

    openid  用户 openid（群/私聊通用主键）
    name    展示名（群名片/昵称）
    is_admin 群消息里 member_role == admin（管理命令的门禁）
    """
    t = _norm(text)
    if not t:
        return None

    # 管理命令优先
    if t in ("入职列表", "花名册") or t.startswith("拉黑") or t.startswith("解黑"):
        if not is_admin:
            return "这是管理命令，仅群主/管理员可用。"
        return _admin_cmd(t, name)

    if t == "取消入职":
        return _cancel(openid)

    # 已有进行中的申请 → 本轮消息按答卷处理；
    # 但若消息本身是入口词（想换岗位），给出切换提示而非当答案
    with _lock:
        d = _load()
        pend = d["applications"].get(openid)
    if pend is not None and pend.get("status") == "pending":
        if parse_role(t) or t in ("入职", "取消入职"):
            return (f"你有一份「{pend['role']}」的申请还没答完"
                    f"（第{pend['step'] + 1}题）。先答完，或发送「取消入职」"
                    f"放弃后再申请新岗位。")
        return _answer(openid, name, t)

    role = parse_role(t)
    if role:
        return _start(openid, name, role)

    if t == "入职":
        return _menu()
    return None


def _menu() -> str:
    lines = ["【入职中心】当前开放岗位："]
    for r in ROLES:
        lines.append(f"· {r}——{ROLES[r]['desc']}")
    lines.append("")
    lines.append("发送「入职+岗位名」开始申请，如：入职陪玩")
    lines.append("申请中发送「取消入职」可放弃；题目在私聊里逐题作答")
    return "\n".join(lines)


def _start(openid: str, name: str, role: str) -> str:
    with _lock:
        d = _load()
        # 黑名单（openid 或花名册历史联系方式都查不了太多，先按 openid+昵称）
        for b in d["blacklist"]:
            if openid == b.get("openid") or name == b.get("qq"):
                return (f"申请被拒绝：你在入职黑名单中"
                        f"（{b.get('reason', '原因未填')}）。"
                        f"有异议请联系群主。")
        # 已在职同岗位
        st = d["staff"].get(openid)
        if st and role in st.get("roles", []):
            return f"你已经是「{role}」了，不用重复入职。想换岗位直接申请新的。"
        # 已有进行中的申请
        old = d["applications"].get(openid)
        if old is not None and old.get("status") == "pending":
            return (f">你有一份「{old['role']}」的申请还没答完"
                    f"（第{old['step'] + 1}题）。继续答，或发送「取消入职」放弃。")
        d["applications"][openid] = {
            "name": name, "role": role, "qa": {}, "step": 0,
            "status": "pending", "reason": "", "ts": _now(),
        }
        _save(d)
    q = _questions_for(role)[0]
    return (f"【{role}】入职申请开始，共{_qcount(role)}题。\n"
            f"{q['text']}\n（答错格式我会提醒重答；随时可「取消入职」）")


def _qcount(role: str) -> int:
    return len(_questions_for(role))


def _answer(openid: str, name: str, text: str) -> str:
    with _lock:
        d = _load()
        app = d["applications"].get(openid)
        if app is None or app.get("status") != "pending":
            return None  # 状态已变，放行（理论上 hr_handle 已挡）
        qs = _questions_for(app["role"])
        idx = app["step"]
        q = qs[idx]
        hint, ok = VALIDATORS[q["validate"]]
        if not ok(text):
            return f"格式不对：{hint}\n{q['text']}"
        app["qa"][q["key"]] = text.strip()
        idx += 1
        app["step"] = idx
        if idx < len(qs):
            _save(d)
            nq = qs[idx]
            return f"已记录。{nq['text']}"
        # 答完 → 自动初筛
        passed, reason = _evaluate(app, d)
        app["status"] = "approved" if passed else "rejected"
        app["reason"] = reason
        app["result_ts"] = _now()
        if passed:
            st = d["staff"].setdefault(openid, {"name": name, "roles": [],
                                                "since": _now()})
            st["roles"].append(app["role"])
            st["name"] = name
        d["history"].append({"openid": openid, "name": name, "role": app["role"],
                             "status": app["status"], "reason": reason,
                             "ts": app["result_ts"]})
        _save(d)
    if passed:
        return (f"恭喜，「{app['role']}」入职通过！已记入花名册。\n"
                f"接下来群主会拉你进对应的工作安排，留意群通知。")
    return f"很遗憾，「{app['role']}」入职未通过。\n原因：{reason}\n（有异议找群主）"


def _evaluate(app: dict, d: dict) -> tuple[bool, str]:
    """自动初筛：状态机已保证必填完整，这里查硬门槛。"""
    qa = app["qa"]
    role = app["role"]
    cfg = ROLES[role]

    # 1. 黑名单（答案里的联系方式命中即拒）
    contact = qa.get("contact", "")
    for b in d["blacklist"]:
        if b.get("qq") and b["qq"] in contact:
            return False, f"联系方式命中黑名单（{b.get('reason', '原因未填')}）"

    # 2. 年龄门槛
    age_raw = qa.get("age", "")
    if age_raw.isdigit() and int(age_raw) < cfg["min_age"]:
        return False, f"「{role}」要求年满 {cfg['min_age']} 周岁（你填的 {age_raw}）"

    # 3. 确认题必须答"确认"
    for q in cfg["questions"]:
        if q["validate"] == "confirm" and qa.get(q["key"], "").strip() != "确认":
            return False, f"「{q['key']}」的规则未确认（需回复「确认」）"

    # 4. 押金类岗位的知悉确认（代肝 deposit 题）已由 confirm 校验覆盖
    return True, "审核通过"


def _cancel(openid: str) -> str:
    with _lock:
        d = _load()
        app = d["applications"].get(openid)
        if app is None or app.get("status") != "pending":
            return "你当前没有进行中的入职申请。"
        app["status"] = "canceled"
        app["result_ts"] = _now()
        d["history"].append({"openid": openid, "name": app.get("name", "?"),
                             "role": app["role"], "status": "canceled",
                             "reason": "本人取消", "ts": app["result_ts"]})
        _save(d)
    return "已取消本次入职申请。想重新开始随时发送「入职+岗位名」。"


# ---------- 管理命令 ----------
def _admin_cmd(t: str, admin_name: str) -> str:
    if t == "花名册":
        with _lock:
            d = _load()
            staff = d["staff"]
        if not staff:
            return "花名册还是空的。"
        lines = ["【花名册】"]
        for oid, st in staff.items():
            lines.append(f"· {st.get('name', '?')}：{'、'.join(st.get('roles', []))}"
                         f"（{st.get('since', '?')[:10]} 入职）")
        return "\n".join(lines)

    if t == "入职列表":
        with _lock:
            d = _load()
            apps = [a for a in d["applications"].values()
                    if a.get("status") == "pending"]
            recent = d["history"][-5:]
        lines = []
        if apps:
            lines.append("【待处理申请】")
            for a in apps:
                lines.append(f"· {a['name']} → {a['role']}（第{a['step'] + 1}题，"
                             f"{a['ts'][5:16]} 发起）")
        else:
            lines.append("当前没有待处理的入职申请。")
        if recent:
            lines.append("")
            lines.append("【最近5条终态】")
            mark = {"approved": "通过", "rejected": "拒绝", "canceled": "取消"}
            for h in reversed(recent):
                lines.append(f"· {h['name']} {h['role']} → {mark.get(h['status'], h['status'])}"
                             f"（{h['reason'][:20]}）{h['ts'][5:16]}")
        lines.append("自动初筛模式：答完即出结果，无需人工。")
        return "\n".join(lines)

    m = re.match(r"^拉黑\s*(\S+)\s*(.*)$", t)
    if m:
        target, reason = m.group(1), m.group(2).strip() or "原因未填"
        with _lock:
            d = _load()
            if any(b.get("qq") == target for b in d["blacklist"]):
                return f"「{target}」已在黑名单。"
            d["blacklist"].append({"qq": target, "reason": reason,
                                   "by": admin_name, "ts": _now()})
            _save(d)
        return f"已拉黑「{target}」（{reason}）。此后再发起入职会被自动拒绝。"

    m = re.match(r"^解黑\s*(\S+)$", t)
    if m:
        target = m.group(1)
        with _lock:
            d = _load()
            before = len(d["blacklist"])
            d["blacklist"] = [b for b in d["blacklist"] if b.get("qq") != target]
            if len(d["blacklist"]) == before:
                return f"黑名单里没有「{target}」。"
            _save(d)
        return f"已解除「{target}」的黑名单。"

    return None
