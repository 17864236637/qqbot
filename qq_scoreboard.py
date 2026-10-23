"""积分制督学 - 积分板（状态持久化 + 违纪扣分 + 每月重置 + 群存档）。

数据持久化在 qq_scoreboard.json：
{
  "groups": {
    "<group_openid>": {
      "last_reset": "2026-10",
      "members": {
        "<user_openid>": {
          "name": "昵称",
          "score": 10,
          "violations": [ {"ts":"2026-10-02 11:00","reason":"刷屏","delta":-1} ]
        }
      }
    }
  }
}

说明：本模块运行在单进程内（receiver 主线程 + cron 后台线程），
因此用 threading.Lock 保证并发安全即可，无需跨进程文件锁。
"""
from __future__ import annotations

import json
import os
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "qq_scoreboard.json")
INIT_SCORE = 10
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


def ensure_member(group: str, user: str, name: str = "") -> dict:
    """确保成员存在；返回成员 dict。"""

    def op(d):
        g = d["groups"].setdefault(group, {"members": {}, "last_reset": ""})
        m = g["members"].setdefault(
            user, {"name": name or user, "score": INIT_SCORE, "violations": []}
        )
        if name:
            m["name"] = name
        return m

    return _with_lock(op)


def deduct(group: str, user: str, delta: int, reason: str, name: str = "") -> tuple:
    """扣分（delta 为负值）。返回 (最新分数, 违规记录条数)。"""

    def op(d):
        g = d["groups"].setdefault(group, {"members": {}, "last_reset": ""})
        m = g["members"].setdefault(
            user, {"name": name or user, "score": INIT_SCORE, "violations": []}
        )
        if name:
            m["name"] = name
        m["score"] = max(0, m["score"] + delta)
        m["violations"].append(
            {
                "ts": time.strftime("%Y-%m-%d %H:%M"),
                "reason": reason,
                "delta": delta,
            }
        )
        return m["score"], len(m["violations"])

    return _with_lock(op)


def get_score(group: str, user: str):
    def op(d):
        return d["groups"].get(group, {}).get("members", {}).get(user, {}).get("score")

    return _with_lock(op)


def monthly_reset_if_needed(month: str | None = None) -> list:
    """若当月未重置则把所有成员分数重置为 INIT_SCORE，并记录 last_reset。
    返回被重置的群列表。"""
    month = month or time.strftime("%Y-%m")

    def op(d):
        reset = []
        for gid, g in d["groups"].items():
            if g.get("last_reset") != month:
                for m in g["members"].values():
                    m["score"] = INIT_SCORE
                    m["violations"].append(
                        {
                            "ts": time.strftime("%Y-%m-%d %H:%M"),
                            "reason": "每月重置",
                            "delta": 0,
                        }
                    )
                g["last_reset"] = month
                reset.append(gid)
        return reset

    return _with_lock(op)


def snapshot(group: str):
    """返回该群积分榜（分数降序）与最近账目。无此群返回 None。"""
    def op(d):
        g = d["groups"].get(group)
        if not g:
            return None
        members = sorted(
            g["members"].items(), key=lambda kv: kv[1]["score"], reverse=True
        )
        return [
            {"name": m["name"], "score": m["score"], "recent": m["violations"][-5:]}
            for _, m in members
        ]

    return _with_lock(op)


def all_groups() -> list:
    """返回所有有成员的群 openid 列表（cron 用来推送）。"""
    def op(d):
        return [gid for gid, g in d["groups"].items() if g.get("members")]

    return _with_lock(op)


def weekly_report(group: str, days: int = 7) -> dict | None:
    """聚合近 days 天数据用于周报。无此群返回 None。

    返回 {members: [{name, score, week_delta, items, clean}]}：
    - week_delta: 本周净增减分
    - items: [(原因, 分值), ...] 只含分值非 0 的真实账目
      （"每月重置""报备写作业"等 0 分记录是状态噪音，剔除）
    - clean: 本周无任何扣分
    members 按当前分数降序。
    """
    cutoff = time.time() - days * 86400
    # ts 格式 "%Y-%m-%d %H:%M" 定长，字符串字典序即时间序，可直接比较
    cutoff_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(cutoff))

    def op(d):
        g = d["groups"].get(group)
        if not g:
            return None
        members = []
        for m in g["members"].values():
            week = [v for v in m["violations"] if v["ts"] >= cutoff_str]
            members.append({
                "name": m["name"],
                "score": m["score"],
                "week_delta": sum(v["delta"] for v in week),
                "items": [(v["reason"], v["delta"]) for v in week if v["delta"] != 0],
                "clean": not any(v["delta"] < 0 for v in week),
            })
        members.sort(key=lambda x: x["score"], reverse=True)
        return {"members": members}

    return _with_lock(op)
