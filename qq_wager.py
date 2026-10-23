"""对赌契约 - 群学习赌约的状态机（积分制的继任者）。

设计哲学（与积分制的根本区别）：
- 积分制：系统记账、系统扣分、系统发奖 -> 分数没有后果，规则变成恐吓
- 契约制：周一当众认赌、周日当众验收、输了的按群规兑现 -> 社交压力是真的
机器人只当"公证人"：记下赌约、到期追问、张贴战报。
它不动手惩罚——惩罚是群里自己的人情债，那才疼。

数据持久化在 qq_wager.json：
{
  "groups": {
    "<group_openid>": {
      "week": "2026-W43",
      "bets": {"<user_openid>": {"name": "张三", "goal": "数学作业全写完",
                                  "status": "pending"}}
    }
  }
}
status: pending(待验收) / done(兑现) / lost(赖账) / overdue(逾期未认)
"""
from __future__ import annotations

import json
import os
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "qq_wager.json")
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"groups": {}}


def _save(data: dict) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def _with_lock(fn):
    with _LOCK:
        data = _load()
        res = fn(data)
        _save(data)
        return res


def week_key(ts: float | None = None) -> str:
    """ISO 周标，如 '2026-W43'。跨周自动开新周期。"""
    import datetime
    d = datetime.date.fromtimestamp(ts) if ts else datetime.date.today()
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _ensure_week(g: dict) -> None:
    """周期过期则归档上周（pending->overdue 挪进 last_week），开新周。

    教训：第一版直接清空 bets，上周逾期者永久消失——周一点名时
    "上周没认账的"永远查不到，契约的账就这么烂了。必须留档一周。
    """
    cur = week_key()
    if g.get("week") != cur:
        if g.get("bets"):
            archived = {}
            for uid, b in g["bets"].items():
                st = b.get("status")
                if st == "pending":
                    st = "overdue"
                archived[uid] = {"name": b["name"], "goal": b["goal"], "status": st}
            g["last_week"] = archived
        g["week"] = cur
        g["bets"] = {}


def join_bet(group: str, user: str, name: str, goal: str) -> str:
    """加入/更新本周赌约。返回给用户的确认文案。"""
    goal = (goal or "").strip()
    if not goal:
        return "赌什么呢？带上目标，比如：我赌数学作业全写完"

    def op(d):
        g = d["groups"].setdefault(group, {"week": "", "bets": {}})
        _ensure_week(g)
        old = g["bets"].get(user)
        g["bets"][user] = {"name": name or user, "goal": goal, "status": "pending"}
        return "已改约" if old else "已入局"

    act = _with_lock(op)
    return (f"✅ {act}：本周赌「{goal}」。周日晚8点验收，"
            f"做到了回'完成'，赖账回'输了'，全员见证。")


def settle(group: str, user: str, done: bool) -> str:
    """了结赌约。done=True 兑现，False 赖账。仅 pending 可结。"""

    def op(d):
        g = d["groups"].get(group)
        if not g:
            return None
        _ensure_week(g)
        b = g["bets"].get(user)
        if not b:
            return None
        if b["status"] != "pending":
            return ("dup", b)
        b["status"] = "done" if done else "lost"
        return ("ok", b)

    res = _with_lock(op)
    if res is None:
        return "你本周没入局。想玩的话：我赌+目标"
    tag, b = res
    if tag == "dup":
        state = {"done": "早已兑现", "lost": "早已认栽",
                 "overdue": "上周逾期未认"}.get(b["status"], "已了结")
        return f"本周赌约{state}过了，下周再来。"
    if done:
        return f"🎉 {b['name']}兑现承诺：「{b['goal']}」——说话算数，记你一笔。"
    return f"😅 {b['name']}认栽：「{b['goal']}」没做到。认账态度满分，下次赢回来。"


def bets(group: str) -> list:
    """本周契约列表 [{name, goal, status}]，按入局顺序。"""

    def op(d):
        g = d["groups"].get(group)
        if not g:
            return []
        _ensure_week(g)
        return [
            {"name": b["name"], "goal": b["goal"], "status": b["status"]}
            for b in g["bets"].values()
        ]

    return _with_lock(op)


def pending(group: str) -> list:
    """待验收名单。"""
    return [b for b in bets(group) if b["status"] == "pending"]


def overdue_last_week(group: str) -> list:
    """上周逾期未认的名单（周一开新局时点名用）。"""

    def op(d):
        g = d["groups"].get(group)
        if not g:
            return []
        return [
            {"name": b["name"], "goal": b["goal"], "status": b["status"]}
            for b in (g.get("last_week") or {}).values()
            if b["status"] == "overdue"
        ]

    return _with_lock(op)


def note_group(group: str) -> None:
    """登记出现过的群（cron 的推送目标列表，自生长，免手工配置）。

    教训：原来 cron 靠积分数据的群列表推消息——积分制一停，列表就空了，
    定时全员静默。现在凡是见过群消息的群都登记，不依赖任何玩法是否开启。
    """
    def op(d):
        known = d.setdefault("known", [])
        if group not in known:
            known.append(group)

    _with_lock(op)


def known_groups() -> list:
    """所有登记过的群 openid。"""

    def op(d):
        return list(d.get("known", []))

    return _with_lock(op)
