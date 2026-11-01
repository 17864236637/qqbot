#!/usr/bin/env python3
"""元命令（meta-commands）：机器人自检与按会话的设置。

涵盖：/ping /echo /time /id /uptime /whoami /reset /persona /skills /rules
      /memory /agent /owner /mode /model /总结

设计要点：
- 会话状态全部内存态（SESSIONS），按 group_openid 或 user_openid 分键；
  沙箱回收重启即清空，群里重设即可，不做持久化。
- 多主人：.env 单主人（qq_owner）作为 bootstrap，qq_owners.json 存额外授权，
  只有主人才能管理。
- 命令分发优先级最高：在 qq_hr 内置命令之前调用，避免被入群系统吞掉。
"""
from __future__ import annotations

import json
import os
import re
import time as _time

BASE = os.path.dirname(os.path.abspath(__file__))
OWNERS_FILE = os.path.join(BASE, "qq_owners.json")
DATA_STAFF = os.path.join(BASE, "qq_staff.json")
DATA_CMDS = os.path.join(BASE, "qq_cmds.json")

# 记忆滚动窗口上限
HISTORY_LIMIT = 10
# 会话状态 key 分隔符，避免群与私聊 openid 撞键（都是 32 位 hex）
_SEP = "::"


# ---------------- 会话状态 ----------------
class Session:
    def __init__(self) -> None:
        self.mode = "free"              # free | strict
        self.model = None               # 模型短名或 None（用全局）
        self.agent = None               # 人格 key 或 None（默认 supervisor）
        self.history = []               # [{role, content}]
        self.pending_summarize = False  # 等待下一条作为待总结文本

    def set(self, **kw) -> None:
        for k, v in kw.items():
            if hasattr(self, k):
                setattr(self, k, v)

    def push(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        if len(self.history) > HISTORY_LIMIT:
            self.history = self.history[-HISTORY_LIMIT:]

    def reset(self) -> None:
        self.__init__()


SESSIONS: dict[str, Session] = {}


def _skey(group_id: str | None, user_openid: str | None) -> str:
    if group_id:
        return _SEP + "g" + _SEP + group_id
    return _SEP + "u" + _SEP + (user_openid or "anon")


def get_session(group_id: str | None, user_openid: str | None) -> Session:
    return SESSIONS.setdefault(_skey(group_id, user_openid), Session())


# ---------------- 多主人 ----------------
def _load_owners() -> list:
    if not os.path.exists(OWNERS_FILE):
        return []
    try:
        return json.load(open(OWNERS_FILE, encoding="utf-8")) or []
    except (OSError, ValueError):
        return []


def _save_owners(lst: list) -> None:
    tmp = OWNERS_FILE + ".tmp"
    json.dump(lst, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, OWNERS_FILE)


def is_owner_any(openid: str | None) -> bool:
    """合并 .env 单主人 与 qq_owners.json 多主人。空值一律 False。"""
    if not openid:
        return False
    try:
        import qq_owner
        if qq_owner.is_owner(openid):
            return True
    except Exception:  # noqa: BLE001
        pass
    return any(o.get("openid") == openid for o in _load_owners())


def owner_add(openid: str, name: str = "") -> str:
    lst = _load_owners()
    if any(o.get("openid") == openid for o in lst):
        return f"{name or openid[:8]} 已经是主人了"
    lst.append({"openid": openid, "name": name,
                "ts": _time.strftime("%Y-%m-%d %H:%M:%S")})
    _save_owners(lst)
    return f"已添加主人：{name or openid[:8]}…（共 {len(lst)} 位）"


def owner_list() -> str:
    try:
        import qq_owner
        main_oid = qq_owner.owner_openid()
        main_name = qq_owner.owner_name()
    except Exception:  # noqa: BLE001
        main_oid, main_name = "", ""
    lines = ["主人列表："]
    if main_oid:
        lines.append(f"  [0] {main_name}（主·.env） {main_oid[:8]}…")
    extra = _load_owners()
    for i, o in enumerate(extra, 1):
        lines.append(f"  [{i}] {o.get('name') or o['openid'][:8]} {o['openid'][:8]}…")
    if len(lines) == 1:
        lines.append("  （暂无，用 /owner add <openid> 添加）")
    return "\n".join(lines)


def owner_remove(idx: int) -> str:
    lst = _load_owners()
    if idx < 1 or idx > len(lst):
        return f"序号无效（现有 {len(lst)} 位授权主人，编号从 1 开始）"
    removed = lst.pop(idx - 1)
    _save_owners(lst)
    return f"已移除主人：{removed.get('name') or removed['openid'][:8]}…（剩 {len(lst)} 位）"


# ---------------- 模型注册 ----------------
# 短名 → .env 键前缀。切换时从 .env 取对应三元组（base/key/model）。
MODEL_REGISTRY = {
    "hunyuan": "HUNYUAN",
    "deepseek": "DEEPSEEK",
    "glm": "GLM",
    "kimi": "KIMI",
    "minimax": "MINIMAX",
}


def current_global_model() -> str:
    return os.environ.get("QQ_BRAIN_MODEL", "1.5b") or "1.5b"


def resolve_model(name: str) -> tuple[str, str, str] | None:
    """把短名解析成 (base, key, model)；缺失返回 None。"""
    prefix = MODEL_REGISTRY.get(name.lower())
    if not prefix:
        return None
    base = os.environ.get(f"{prefix}_API_BASE", "")
    key = os.environ.get(f"{prefix}_API_KEY", "")
    model = os.environ.get(f"{prefix}_API_MODEL", "")
    if not (base and key and model):
        return None
    return base, key, model


# ---------------- 元命令分发 ----------------
def handle(cmd_text: str, ctx: dict) -> str | None:
    """命中元命令返回文案；未命中返回 None 放行下游。
    cmd_text 已去掉前导 '/'。ctx: {uid,name,group_id,user_openid,is_owner,receiver}
    """
    if not cmd_text:
        return None
    parts = cmd_text.split(None, 1)
    head = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    # echo：/echo 之后的原样回显（保留全部内容）
    if head == "echo":
        return cmd_text[len("echo"):].lstrip() or "（空）"

    if head == "ping":
        return "pong 🏓 在线"

    if head == "time":
        return "🕐 " + _time.strftime("%Y-%m-%d %H:%M:%S")

    if head == "id":
        if ctx.get("group_id"):
            return f"群会话标识：{ctx['group_id']}"
        return f"私聊会话标识：{ctx.get('user_openid') or '未知'}"

    if head == "uptime":
        rcv = ctx.get("receiver")
        started = getattr(rcv, "_started", None) or _time.time()
        return f"⏱ 已运行 {_fmt_dur(int(_time.time() - started))}"

    if head == "whoami":
        return (f"你的标识：{ctx.get('user_openid') or '未知'}\n"
                f"昵称：{ctx.get('name') or '未知'}")

    if head == "reset":
        get_session(ctx.get("group_id"), ctx.get("user_openid")).reset()
        return "♻️ 已重置本会话（模式/模型/人格/记忆已清空）"

    if head == "persona":
        try:
            import qq_personas
            s = get_session(ctx.get("group_id"), ctx.get("user_openid"))
            p = qq_personas.get(s.agent)
            return f"【当前人格：{p['name']}】\n{p['system']}"
        except Exception:  # noqa: BLE001
            return "（人设加载失败）"

    if head == "skills":
        return ("已加载功能模块：\n"
                "  • 监督员（定时催学/毒舌话术）\n"
                "  • 入群审核（陪玩/陪聊/树洞/代肝/监督员 五线）\n"
                "  • 自定义指令（/加指令 增删，支持一次性/过期/撤回）\n"
                "  • 自动清理（会话超时作废/消息撤回/日志归档）\n"
                "  • 主人绑定（人设归属/独占重罚命令）\n"
                "  • 元命令（本套 /ping 等自检命令）")

    if head == "rules":
        return ("当前规则：\n"
                "  • 群方服务费率：学员实付价的 25%（申请人实得 75%）\n"
                "  • 监督模式：free=正常闲聊，strict=只听指令\n"
                "  • 撤回策略：autodel=N 秒后自动撤回机器人消息\n"
                "  • 主人特权：人设归属 / 私聊管理 / 免疫监督 / 独占拉黑解黑\n"
                "  • 会话设置重启清空（内存态，不持久化）")

    if head == "memory":
        s = get_session(ctx.get("group_id"), ctx.get("user_openid"))
        staff_n = _count_json(DATA_STAFF, "staff")
        cmds_n = _count_json(DATA_CMDS, "cmds")
        return (f"持久数据：\n"
                f"  • qq_staff.json 在册人员：{staff_n}\n"
                f"  • qq_cmds.json 自定义指令：{cmds_n}\n"
                f"本会话记忆条数：{len(s.history)}（上限 {HISTORY_LIMIT}）")

    if head == "agent":
        try:
            import qq_personas
        except Exception:  # noqa: BLE001
            return "（人格模块加载失败）"
        if not rest:
            return qq_personas.agent_list()
        key = rest.strip().lower()
        if not qq_personas.is_valid(key):
            return f"未知人格：{key}\n" + qq_personas.agent_list()
        get_session(ctx.get("group_id"), ctx.get("user_openid")).agent = key
        return f"已切换人格为：{qq_personas.get(key)['name']}"

    if head == "mode":
        s = get_session(ctx.get("group_id"), ctx.get("user_openid"))
        if not rest:
            return f"当前模式：{s.mode}（free=正常 / strict=只听指令）"
        m = rest.strip().lower()
        if m not in ("free", "strict"):
            return "用法：/mode free 或 /mode strict"
        s.mode = m
        return f"已切换为 {m} 模式"

    if head == "model":
        s = get_session(ctx.get("group_id"), ctx.get("user_openid"))
        if not rest:
            eff = s.model or current_global_model()
            return (f"当前模型：{eff}（全局默认 {current_global_model()}）\n"
                    f"可用：hunyuan/deepseek/glm/kimi/minimax")
        name = rest.strip().lower()
        if name not in MODEL_REGISTRY:
            return f"未知模型：{name}\n可用：hunyuan/deepseek/glm/kimi/minimax"
        resolved = resolve_model(name)
        if not resolved:
            return (f"切换 {name} 需要先配置 .env：\n"
                    f"  {MODEL_REGISTRY[name]}_API_BASE=\n"
                    f"  {MODEL_REGISTRY[name]}_API_KEY=\n"
                    f"  {MODEL_REGISTRY[name]}_API_MODEL=")
        s.model = name
        return f"已切换本会话模型为：{name}"

    if head == "owner":
        if not ctx.get("is_owner"):
            return "只有主人能管理主人。"
        sub = rest.strip()
        sub_l = sub.lower()
        if sub == "" or sub_l == "list":
            return owner_list()
        if sub_l.startswith("add "):
            target = sub[4:].strip()
            m = re.search(r"[0-9A-Fa-f]{32}", target)
            oid = m.group(0) if m else target
            return owner_add(oid)
        if sub_l.startswith("remove "):
            try:
                idx = int(sub.split()[1])
            except (ValueError, IndexError):
                return "用法：/owner remove <序号>（序号看 /owner list）"
            return owner_remove(idx)
        return "用法：/owner [list | add <openid> | remove <序号>]"

    if head == "总结":
        s = get_session(ctx.get("group_id"), ctx.get("user_openid"))
        if rest:
            s.pending_summarize = False
            return _summarize(rest)
        if s.pending_summarize:          # 再发一次 /总结 = 取消
            s.pending_summarize = False
            return "已取消总结。"
        s.pending_summarize = True
        return "把要总结的内容发给我，我来提炼要点清单。（再发 /总结 取消）"

    # 处理 pending 总结：上一条是 /总结 无内容，本条作为待总结文本
    s = get_session(ctx.get("group_id"), ctx.get("user_openid"))
    if s.pending_summarize:
        s.pending_summarize = False
        if head == "总结":
            return "已取消总结。"
        return _summarize(cmd_text)

    return None


def _summarize(text: str) -> str:
    try:
        from qq_brain import generate
        return generate(
            text,
            system=("你是一个专业的文本总结助手。把用户给的内容提炼成"
                    "清晰的要点清单（markdown 无序列表），保留关键数字、"
                    "结论与行动项。不要复述、不要评价。如内容为空或无效，"
                    "直接说『没看出要总结啥』。"),
            max_tokens=400, temperature=0.3,
        ) or "（总结失败）"
    except Exception as exc:  # noqa: BLE001
        return f"总结失败：{exc}"


def _fmt_dur(secs: int) -> str:
    d, s = divmod(secs, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}天")
    if h:
        parts.append(f"{h}时")
    if m:
        parts.append(f"{m}分")
    parts.append(f"{s}秒")
    return "".join(parts)


def _count_json(path: str, key: str) -> int:
    if not os.path.exists(path):
        return 0
    try:
        d = json.load(open(path, encoding="utf-8"))
        v = d.get(key)
        return len(v) if isinstance(v, (list, dict)) else 0
    except (OSError, ValueError):
        return 0
