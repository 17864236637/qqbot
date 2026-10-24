#!/usr/bin/env python3
"""入群审核系统：陪玩 / 陪聊 / 树洞 / 代肝 / 监督员 五线入群。

定位（务必理解，别再跑偏）：
- 本模块管「人」的入群：申请 → 分线问卷 → 自动初筛 → 通过/拒绝 → 花名册上架。
  经手资金只做"规则知悉确认"（学员实付价、群方收 25% 服务费），钱的事群主线下处理。
- 学员点单：通过者按岗位分类上架（业务范围+定价），学员发「点单」看板选人下单，
  被点者私聊收到通知，接单/拒单/完结全流程留痕。
- 命令拦截型：关键词命中即接管对话，不调用大模型，毫秒级、确定性强。
- 状态全部持久化到 qq_staff.json（进程会死，申请不能丢；原子写防半截文件）。

用法（群/私聊均可发起；「入职」是「入群」的别名，两个词都认）：
  入群 / 入职           列出岗位与用法
  入群<岗位>            开始该岗位问卷（如：入群陪玩）
  （私聊中逐题作答，格式错会要求重答）
  取消入群              放弃当前申请
  点单                  学员看板：分类在册人员+业务范围+定价
  点单 <编号>           给该编号的人下单（私聊通知对方）
  接单 / 拒单           被点者处理最新一单
  完结 <编号>           订单完成后归档（下单人或被点人皆可）
  我的订单              查自己发起/收到的订单
管理命令（member_role=admin/owner 限定）：
  入群列表 / 花名册 / 订单列表 / 拉黑 <QQ或昵称> <原因> / 解黑 <QQ或昵称>

问卷题目在 ROLES/COMMON_QUESTIONS 里改文本即可，无需动逻辑。
自动初筛规则见 _evaluate：黑名单 → 重复在职 → 年龄门槛 → 确认题（全部题，含抽成规则）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "qq_staff.json")
ADMIN_FILE = os.path.join(BASE, "qq_admins.json")

# 注意：RLock（可重入）——_place_order/_respond_order/_finish_order 持锁期间
# 会调用 _queue_notif 排队私聊通知，普通 Lock 会同线程二次获取直接死锁。
_lock = threading.RLock()

# 群方服务费率：学员实付价里群内收取的比例（申请人实得 1-0.25）。
FEE_RATE = 0.25

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
    {"key": "scope", "text": "第4题（通用）：你的业务范围？（具体服务内容/科目/游戏/"
                             "可接类型，写清楚学员能得到什么）", "validate": "nonempty"},
    {"key": "price", "text": "第5题（通用）：你的定价？自定义——填学员实付价"
                             "（如：30元/小时、50元/局）", "validate": "nonempty"},
    {"key": "fee_ack", "text": "第6题（通用）：收费规则确认——你报的是学员实付价，"
                               "群内收取 25% 服务费，你实得 75%。"
                               "回复「确认」或「拒绝」", "validate": "confirm"},
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
    # history 是全量终态归档（approved/rejected/canceled），覆盖不丢痕；
    # orders 是点单全量记录（pending/accepted/done/rejected/canceled）。
    return {"blacklist": [], "applications": {}, "staff": {}, "history": [],
            "orders": []}


def _migrate_staff(d: dict) -> None:
    """旧结构兼容：roles 从 list[str] 升级为 list[dict]（带业务范围/定价/上架时间）。"""
    for st in d.get("staff", {}).values():
        roles = st.get("roles", [])
        new_roles = []
        for r in roles:
            if isinstance(r, str):
                new_roles.append({"role": r, "scope": "未提供", "price": "未提供",
                                  "since": st.get("since", "?")})
            else:
                new_roles.append(r)
        st["roles"] = new_roles


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
    d.setdefault("orders", [])
    _migrate_staff(d)
    return d


def _save(d: dict) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)  # 原子替换，防进程死在写一半


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------- 管理员身份缓存 ----------
# 群消息带 member_role（群主是 owner、管理员是 admin），私聊不带这个字段。
# 群主在私聊里发管理命令时，靠这个缓存认人——只认在群里被官方标记过
# 管理身份的人，安全边界不变（陌生人私聊发"拉黑"照样被挡）。
def note_admin(openid: str, member_role: str) -> None:
    if member_role in ("admin", "owner") and openid:
        with _lock:
            try:
                with open(ADMIN_FILE, encoding="utf-8") as f:
                    known = set(json.load(f))
            except (OSError, ValueError):
                known = set()
            if openid not in known:
                known.add(openid)
                tmp = ADMIN_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(sorted(known), f, ensure_ascii=False)
                os.replace(tmp, ADMIN_FILE)


def is_known_admin(openid: str) -> bool:
    try:
        with open(ADMIN_FILE, encoding="utf-8") as f:
            return openid in set(json.load(f))
    except (OSError, ValueError):
        return False


# ---------- 待发私聊通知（receiver 消费后发送） ----------
_NOTIFS: list[tuple[str, str]] = []


def _queue_notif(to_openid: str, text: str) -> None:
    with _lock:
        _NOTIFS.append((to_openid, text))


def pop_notifs() -> list[tuple[str, str]]:
    with _lock:
        out, _NOTIFS[:] = list(_NOTIFS), []
    return out


def _questions_for(role: str) -> list:
    return COMMON_QUESTIONS + ROLES[role]["questions"]


# ---------- 关键词入口 ----------
def _norm(text: str) -> str:
    return (text or "").strip()


# 入口词别名：入群/入职 等价；列表/取消同样双认
_ENTRY_WORDS = ("入群", "入职")
_LIST_WORDS = ("入群列表", "入职列表")
_CANCEL_WORDS = ("取消入群", "取消入职")


def parse_role(text: str) -> str | None:
    """从消息里解析岗位名：'入群陪玩'/'入职陪玩' → '陪玩'（拼音/缩写不搞，防误触）。"""
    t = _norm(text)
    m = re.match(r"^(?:入群|入职)\s*([一-龥A-Za-z0-9]{1,6})$", t)
    if not m:
        return None
    return m.group(1) if m.group(1) in ROLES else None


def is_entry(text: str) -> bool:
    """是否入群系统的入口词（精确匹配，防日常聊天误触）。"""
    t = _norm(text)
    return t in _ENTRY_WORDS + _LIST_WORDS + ("花名册", "订单列表") + _CANCEL_WORDS \
        or parse_role(t) is not None \
        or t in ("点单", "接单", "拒单", "我的订单") \
        or re.match(r"^点单\s*\d{1,2}$", t) \
        or re.match(r"^完结\s*\d{1,2}$", t) \
        or t.startswith("拉黑") or t.startswith("解黑")


# ---------- 核心流程 ----------
def hr_handle(openid: str, name: str, text: str, is_admin: bool = False) -> str | None:
    """receiver 的接线点：命中入群系统返回文案，否则返回 None（放行给大模型）。

    openid  用户 openid（群/私聊通用主键）
    name    展示名（群名片/昵称）
    is_admin 群消息里 member_role in (admin, owner)，或私聊缓存命中的群主/管理员
    """
    t = _norm(text)
    if not t:
        return None

    # 管理命令优先
    if t in _LIST_WORDS + ("花名册", "订单列表") \
            or t.startswith("拉黑") or t.startswith("解黑"):
        if not is_admin:
            return "这是管理命令，仅群主/管理员可用。"
        return _admin_cmd(t, name)

    if t in _CANCEL_WORDS:
        return _cancel(openid)

    # 学员点单
    if t == "点单":
        return _order_menu()
    m = re.match(r"^点单\s*(\d{1,2})$", t)
    if m:
        return _place_order(openid, name, int(m.group(1)))
    m = re.match(r"^完结\s*(\d{1,2})$", t)
    if m:
        return _finish_order(openid, name, int(m.group(1)))
    if t == "接单":
        return _respond_order(openid, name, True)
    if t == "拒单":
        return _respond_order(openid, name, False)
    if t == "我的订单":
        return _my_orders(openid)

    # 已有进行中的申请 → 本轮消息按答卷处理；
    # 但若消息本身是入口词（想换岗位），给出切换提示而非当答案
    with _lock:
        d = _load()
        pend = d["applications"].get(openid)
    if pend is not None and pend.get("status") == "pending":
        if parse_role(t) or t in _ENTRY_WORDS + _CANCEL_WORDS:
            return (f"你有一份「{pend['role']}」的申请还没答完"
                    f"（第{pend['step'] + 1}题）。先答完，或发送「取消入群」"
                    f"放弃后再申请新岗位。")
        return _answer(openid, name, t)

    role = parse_role(t)
    if role:
        return _start(openid, name, role)

    if t in _ENTRY_WORDS:
        return _menu()
    return None


def _menu() -> str:
    lines = ["【入群中心】当前开放岗位："]
    for r in ROLES:
        lines.append(f"· {r}——{ROLES[r]['desc']}")
    lines.append("")
    lines.append("发送「入群+岗位名」（「入职+岗位名」也行）开始申请，如：入群陪玩")
    lines.append("通过后你的业务范围和定价上架看板，学员可点单；"
                 "定价填学员实付价，群内收 25% 服务费")
    lines.append("申请中发送「取消入群」可放弃；题目在私聊里逐题作答")
    lines.append("学员点单发「点单」看板")
    return "\n".join(lines)


def _start(openid: str, name: str, role: str) -> str:
    with _lock:
        d = _load()
        # 黑名单（openid 或花名册历史联系方式都查不了太多，先按 openid+昵称）
        for b in d["blacklist"]:
            if openid == b.get("openid") or name == b.get("qq"):
                return (f"申请被拒绝：你在入群黑名单中"
                        f"（{b.get('reason', '原因未填')}）。"
                        f"有异议请联系群主。")
        # 已在职同岗位（scope 还是"未提供"的旧数据允许重新申请补资料）
        st = d["staff"].get(openid)
        if st:
            for r in st.get("roles", []):
                if r.get("role") == role and r.get("scope") != "未提供":
                    return f"你已经是「{role}」了，不用重复入群。想换岗位直接申请新的。"
        # 已有进行中的申请
        old = d["applications"].get(openid)
        if old is not None and old.get("status") == "pending":
            return (f">你有一份「{old['role']}」的申请还没答完"
                    f"（第{old['step'] + 1}题）。继续答，或发送「取消入群」放弃。")
        d["applications"][openid] = {
            "name": name, "role": role, "qa": {}, "step": 0,
            "status": "pending", "reason": "", "ts": _now(),
        }
        _save(d)
    q = _questions_for(role)[0]
    return (f"【{role}】入群申请开始，共{_qcount(role)}题。\n"
            f"{q['text']}\n（答错格式我会提醒重答；随时可「取消入群」）")


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
            # 同岗位覆盖（补资料通道：旧数据 scope 未提供时可重报，不重复追加）
            st["roles"] = [r for r in st["roles"] if r.get("role") != app["role"]]
            st["roles"].append({"role": app["role"], "scope": app["qa"].get("scope", "未提供"),
                                "price": app["qa"].get("price", "未提供"),
                                "since": _now()})
            st["name"] = name
        d["history"].append({"openid": openid, "name": name, "role": app["role"],
                             "status": app["status"], "reason": reason,
                             "ts": app["result_ts"]})
        _save(d)
    if passed:
        return (f"恭喜，「{app['role']}」入群通过！已记入花名册并上架点单看板。\n"
                f"业务范围：{app['qa'].get('scope', '未提供')}\n"
                f"定价：{app['qa'].get('price', '未提供')}（学员实付，群方收 25%）\n"
                f"接下来群主会拉你进对应的工作安排，留意群通知。")
    return f"很遗憾，「{app['role']}」入群未通过。\n原因：{reason}\n（有异议找群主）"


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

    # 3. 确认题必须答"确认"（全部题，含通用题里的收费规则 fee_ack）
    for q in _questions_for(role):
        if q["validate"] == "confirm" and qa.get(q["key"], "").strip() != "确认":
            return False, f"「{q['key']}」的规则未确认（需回复「确认」）"

    # 4. 押金类岗位的知悉确认（代肝 deposit 题）已由 confirm 校验覆盖
    return True, "审核通过"


def _cancel(openid: str) -> str:
    with _lock:
        d = _load()
        app = d["applications"].get(openid)
        if app is None or app.get("status") != "pending":
            return "你当前没有进行中的入群申请。"
        app["status"] = "canceled"
        app["result_ts"] = _now()
        d["history"].append({"openid": openid, "name": app.get("name", "?"),
                             "role": app["role"], "status": "canceled",
                             "reason": "本人取消", "ts": app["result_ts"]})
        _save(d)
    return "已取消本次入群申请。想重新开始随时发送「入群+岗位名」。"


# ---------- 学员点单 ----------
def _offerings(d: dict) -> list[dict]:
    """展开花名册为可点单条目：按岗位分组顺序，每人每岗一条。"""
    items = []
    for oid, st in d.get("staff", {}).items():
        for r in st.get("roles", []):
            items.append({"openid": oid, "name": st.get("name", "?"),
                          "role": r.get("role", "?"),
                          "scope": r.get("scope", "未提供"),
                          "price": r.get("price", "未提供"),
                          "since": r.get("since", "?")})
    return items


def _order_menu() -> str:
    with _lock:
        d = _load()
        items = _offerings(d)
    if not items:
        return ("点单看板还是空的——还没有人通过入群审核。\n"
                "想接单？发送「入群」看看开放岗位。")
    lines = ["【点单看板】按岗位分类（编号即点单号）："]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. [{it['role']}] {it['name']}｜{it['scope']}"
                     f"｜定价：{it['price']}")
    lines.append("")
    lines.append(f"发送「点单 编号」下单，如：点单 1")
    lines.append("定价均为学员实付价，群内收取 25% 服务费；"
                 "被点者会收到私聊通知，回复「接单」后开始")
    return "\n".join(lines)


def _place_order(openid: str, name: str, idx: int) -> str:
    with _lock:
        d = _load()
        items = _offerings(d)
        if not (1 <= idx <= len(items)):
            return f"编号 {idx} 不在看板上。发送「点单」重新看板。"
        it = items[idx - 1]
        if it["openid"] == openid:
            return "不能点自己的单。"
        # 同一学员对同一被点者的同岗位进行中订单去重
        for o in d["orders"]:
            if o["status"] == "pending" and o["from_openid"] == openid \
                    and o["to_openid"] == it["openid"] and o["role"] == it["role"]:
                return (f"你对「{it['name']}」的「{it['role']}」已有一单在进行中"
                        f"（单号 {o['id']}），等对方处理后再说。")
        oid_new = max([o.get("id", 0) for o in d["orders"]], default=0) + 1
        order = {"id": oid_new, "from_openid": openid, "from_name": name,
                 "to_openid": it["openid"], "to_name": it["name"],
                 "role": it["role"], "scope": it["scope"], "price": it["price"],
                 "status": "pending", "ts": _now(), "result_ts": ""}
        d["orders"].append(order)
        _save(d)
        _queue_notif(it["openid"],
                     f"有人点你！学员：{name}\n岗位：{it['role']}\n"
                     f"业务范围：{it['scope']}\n定价：{it['price']}"
                     f"（学员实付，群方收 25%，你实得 75%）\n"
                     f"回复「接单」或「拒单」；单号 {oid_new}")
    return (f"已下单（单号 {oid_new}）：{it['name']} 的「{it['role']}」，"
            f"定价 {it['price']}。\n对方私聊已收到通知，等 TA 回复「接单」；"
            f"随时发「我的订单」查进度。")


def _respond_order(openid: str, name: str, accept: bool) -> str:
    with _lock:
        d = _load()
        mine = [o for o in d["orders"]
                if o["to_openid"] == openid and o["status"] == "pending"]
        if not mine:
            return "你现在没有被点的待处理订单。"
        o = mine[-1]  # 处理最新一单
        o["status"] = "accepted" if accept else "rejected"
        o["result_ts"] = _now()
        _save(d)
        _queue_notif(o["from_openid"],
                     f"{'好消息' if accept else '很遗憾'}：「{o['to_name']}」"
                     f"{'已接' if accept else '拒了'}你的「{o['role']}」单"
                     f"（单号 {o['id']}，{o['price']}）。"
                     + ("完成后发送「完结 %d」。" % o["id"] if accept
                        else "可以发「点单」看其他人。"))
    if accept:
        return (f"已接单（单号 {o['id']}）：{o['from_name']} 的「{o['role']}」。\n"
                f"记得线上履约；对方确认后发送「完结 {o['id']}」归档。")
    return f"已拒单（单号 {o['id']}）。学员已收到通知。"


def _finish_order(openid: str, name: str, oid: int) -> str:
    with _lock:
        d = _load()
        for o in d["orders"]:
            if o.get("id") == oid and o["status"] == "accepted" \
                    and openid in (o["from_openid"], o["to_openid"]):
                o["status"] = "done"
                o["result_ts"] = _now()
                _save(d)
                other = o["to_openid"] if openid == o["from_openid"] else o["from_openid"]
                _queue_notif(other,
                             f"单号 {oid}（{o['role']}，{o['price']}）已完结归档。"
                             f"辛苦了。")
                return f"单号 {oid} 已完结，订单已存档。"
        return f"没找到你可完结的进行中订单 {oid}。发「我的订单」看看。"


def _my_orders(openid: str) -> str:
    with _lock:
        d = _load()
        mine = [o for o in d["orders"]
                if openid in (o["from_openid"], o["to_openid"])]
    if not mine:
        return "你还没有订单。学员发「点单」看板下单；在册人员等人点即可。"
    mark = {"pending": "待处理", "accepted": "进行中", "done": "已完成",
            "rejected": "被拒", "canceled": "已取消"}
    lines = ["【我的订单】"]
    for o in mine[-10:]:
        way = "我发起" if o["from_openid"] == openid else "点我的"
        lines.append(f"· #{o['id']} {way}｜{o['to_name'] if way == '我发起' else o['from_name']}"
                     f"｜{o['role']}｜{o['price']}｜{mark.get(o['status'], o['status'])}"
                     f"｜{o['ts'][5:16]}")
    return "\n".join(lines)


# ---------- 管理命令 ----------
def _admin_cmd(t: str, admin_name: str) -> str:
    if t == "花名册":
        with _lock:
            d = _load()
            items = _offerings(d)
        if not items:
            return "花名册还是空的。"
        lines = ["【花名册】按岗位分类（含业务范围/定价）："]
        cur = None
        for it in items:
            if it["role"] != cur:
                cur = it["role"]
                lines.append(f"— {cur} —")
            lines.append(f"· {it['name']}｜{it['scope']}｜{it['price']}"
                         f"（{it['since'][:10]} 入群）")
        return "\n".join(lines)

    if t in _LIST_WORDS:
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
            lines.append("当前没有待处理的入群申请。")
        if recent:
            lines.append("")
            lines.append("【最近5条终态】")
            mark = {"approved": "通过", "rejected": "拒绝", "canceled": "取消"}
            for h in reversed(recent):
                lines.append(f"· {h['name']} {h['role']} → {mark.get(h['status'], h['status'])}"
                             f"（{h['reason'][:20]}）{h['ts'][5:16]}")
        lines.append("自动初筛模式：答完即出结果，无需人工。")
        return "\n".join(lines)

    if t == "订单列表":
        with _lock:
            d = _load()
            live = [o for o in d["orders"] if o["status"] in ("pending", "accepted")]
            done = [o for o in d["orders"] if o["status"] not in ("pending", "accepted")]
        lines = []
        if live:
            lines.append("【进行中订单】")
            for o in live:
                lines.append(f"· #{o['id']} {o['from_name']} → {o['to_name']}"
                             f"｜{o['role']}｜{o['price']}｜"
                             f"{'待处理' if o['status'] == 'pending' else '已接单'}")
        else:
            lines.append("没有进行中的订单。")
        if done:
            lines.append("")
            lines.append("【最近5条终态】")
            for o in done[-5:]:
                lines.append(f"· #{o['id']} {o['from_name']} → {o['to_name']}"
                             f"｜{o['role']}｜{o['status']}")
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
        return f"已拉黑「{target}」（{reason}）。此后再发起入群会被自动拒绝。"

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
