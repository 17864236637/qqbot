"""违纪检测规则：刷屏 / 脏话 / 宵禁 / 惹事 / 写作业报备状态机。

所有函数均为纯规则判定，不调用大模型，毫秒级执行。
状态保存在进程内（与 receiver 同进程），无需持久化。
"""
from __future__ import annotations

import time

# 脏话/辱骂词表（可自行扩充）
BAD_WORDS = ["傻逼", "傻叉", "滚蛋", "废物", "垃圾", "草泥马", "cnm", "sb", "mdzz"]
# 惹事触发词
PROVOKE_WORDS = ["打架", "惹事", "干架", "去死", "弄死"]
# 写作业报备指令
HW_START = ["开始写作业", "报备写作业", "写作业了", "开始学习", "我要学习了"]
HW_END = ["写完作业", "写完啦", "提交作业", "写完", "作业提交", "学习结束"]
# 宵禁时段（小时，含起点不含终点）：0:00 - 8:00
CURFEW_START, CURFEW_END = 0, 8
# 刷屏判定：窗口内消息数
SPAM_WINDOW = 10  # 秒
SPAM_COUNT = 5  # 10 秒内 5 条算刷屏
# 不按时提交阈值（分钟）
HW_TIMEOUT_MIN = 60

# 状态存储（进程内）
_spam = {}  # {group: {user: [ts,...]}}
_homework = {}  # {group: {user: {"start": ts}}}
_hw_msgs = {}  # {group: {user: [ts,...]}}  写作业期间的发言


def check_spam(group: str, user: str) -> bool:
    now = time.time()
    g = _spam.setdefault(group, {})
    lst = [t for t in g.get(user, []) if now - t < SPAM_WINDOW]
    lst.append(now)
    g[user] = lst
    if len(lst) >= SPAM_COUNT:
        g[user] = []  # 重置，避免连续狂扣
        return True
    return False


def check_badword(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in BAD_WORDS)


def check_curfew() -> bool:
    h = time.localtime().tm_hour
    return CURFEW_START <= h < CURFEW_END


def check_provoke(text: str) -> bool:
    t = text or ""
    return any(w in t for w in PROVOKE_WORDS)


def start_homework(group: str, user: str) -> bool:
    """识别到开始写作业指令。返回是否成功开始（避免重复）。"""
    g = _homework.setdefault(group, {})
    if user in g:
        return False
    g[user] = {"start": time.time()}
    _hw_msgs.setdefault(group, {})[user] = []
    return True


def end_homework(group: str, user: str):
    """结束写作业，返回 (玩手机扣1?, 不按时扣2?, 时长分钟)。"""
    g = _homework.get(group, {})
    if user not in g:
        return False, False, 0
    start = g.pop(user)["start"]
    msgs = _hw_msgs.get(group, {}).pop(user, [])
    dur = (time.time() - start) / 60.0
    play = bool(msgs)  # 期间发过消息 = 玩手机
    overdue = dur > HW_TIMEOUT_MIN
    return play, overdue, int(dur)


def note_homework_msg(group: str, user: str) -> None:
    """记录写作业期间的发言（用于判定玩手机）。"""
    if group in _homework and user in _homework.get(group, {}):
        _hw_msgs.setdefault(group, {}).setdefault(user, []).append(time.time())


def is_hw_start(text: str) -> bool:
    return any(k in (text or "") for k in HW_START)


def is_hw_end(text: str) -> bool:
    return any(k in (text or "") for k in HW_END)
