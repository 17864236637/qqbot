#!/usr/bin/env python3
"""自动清理：卡住的会话状态 / 机器人发的消息 / 历史数据。

三件事分开放是因为失效时机完全不同：
1. 会话状态——用户答到一半跑了，pending 会永久挂着。超时自动作废。
2. 消息撤回——指令配了 autodel 才撤，发完 N 秒后台线程执行。
3. 历史数据——chat_log / 订单 / history 只增不减，定期截断。

一律惰性触发（有消息进来顺手做 + 限频），不起常驻定时器：沙箱随时
被回收，任何"每分钟跑一次"的线程都活不过一次回收。
"""
from __future__ import annotations

import json
import os
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "qq_staff.json")
CHAT_LOG = os.path.join(BASE, "chat_log.jsonl")
PUSH_LOG = os.path.join(BASE, "push_log.jsonl")

# 默认时效：问卷答一半跑掉，30 分钟作废；点单没人接，24 小时作废
APP_TTL_MIN = 30
ORDER_PENDING_TTL_H = 24
ORDER_ACCEPTED_TTL_H = 72      # 已接单在进行中，给 3 天

# 归档保留策略
KEEP_HISTORY = 500             # history 终态记录保留条数
KEEP_ORDERS = 300              # 订单保留条数
KEEP_LOG_LINES = 20000         # jsonl 日志保留行数

_SWEEP_GAP = 120               # 会话状态扫描间隔（秒），别每条消息都扫文件
_last_sweep = 0.0
_lock = threading.Lock()


def _now_s() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _age_min(ts: str) -> float:
    """时间串距今多少分钟；解析不出返回 0（当新数据看待，绝不误删）。"""
    try:
        return (time.time() - time.mktime(time.strptime(str(ts),
                "%Y-%m-%d %H:%M:%S"))) / 60.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _save(d: dict) -> None:
    """落盘。

    坑：别写成 `def _save(d, path=DATA_FILE)`——默认参数在**定义时**就绑定了，
    测试里 monkeypatch 掉 DATA_FILE 也改不动它，结果读写两个文件：读的是
    临时目标、写的是生产真数据（惨案已发生过一次）。这里运行时才取值。
    """
    path = DATA_FILE
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------- 1. 卡住的会话状态 ----------
def sweep_sessions(force: bool = False) -> dict:
    """把超时的 pending 会话作废并归档。返回各类型清理计数。

    applications 是单槽字典，超时直接摘掉——用户才能重新申请，
    否则那条 openid 会被永久占住，再发"入群"永远是"你还有问卷没答完"。
    """
    global _last_sweep
    now = time.time()
    if not force and now - _last_sweep < _SWEEP_GAP:
        return {}
    _last_sweep = now

    out = {"apps": 0, "orders": 0}
    if not os.path.exists(DATA_FILE):
        return out
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return out

    changed = False
    apps = d.setdefault("applications", {})
    for oid in list(apps):
        app = apps[oid] or {}
        if app.get("status") != "pending":
            continue
        if _age_min(app.get("ts", "")) >= APP_TTL_MIN:
            rec = dict(app)
            rec["status"] = "expired"
            rec["openid"] = oid
            rec["result_ts"] = _now_s()
            d.setdefault("history", []).append(rec)
            del apps[oid]
            out["apps"] += 1
            changed = True

    for o in d.get("orders", []):
        st = o.get("status")
        if st == "pending" and _age_min(o.get("ts", "")) >= ORDER_PENDING_TTL_H * 60:
            o["status"] = "expired"
            o["result_ts"] = _now_s()
            out["orders"] += 1
            changed = True
        elif st == "accepted" and _age_min(o.get("accepted_ts") or o.get("ts", "")) \
                >= ORDER_ACCEPTED_TTL_H * 60:
            o["status"] = "expired"
            o["result_ts"] = _now_s()
            out["orders"] += 1
            changed = True

    if changed:
        _save(d)
    return out


# ---------- 2. 机器人发的消息 ----------
def recall_later(bot, kind: str, target: str, message_id: str,
                 delay: int, hidetip: bool = True) -> None:
    """延迟撤回机器人自己发的消息（群聊实测可用，别被空群号坑了）。

    daemon=True：沙箱回收时不等这个线程。撤回失败无所谓，静默吞掉——
    最坏结果是那条消息留着，绝不能因为撤不回就报错打扰人。
    """
    if not (target and message_id) or delay <= 0:
        return

    def _job():
        try:
            if kind == "group":
                bot.recall_group_message(target, message_id, hidetip)
            else:
                bot.recall_c2c_message(target, message_id, hidetip)
        except Exception:
            pass

    t = threading.Timer(delay, _job)
    t.daemon = True
    t.start()


# ---------- 3. 历史数据归档 ----------
def prune_logs(keep_lines: int = KEEP_LOG_LINES) -> dict:
    """把 jsonl 日志截断到最近 N 行。返回 {文件: 清理行数}。"""
    out = {}
    for path in (CHAT_LOG, PUSH_LOG):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            if len(lines) <= keep_lines:
                continue
            keep = lines[-keep_lines:]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(tmp, path)
            out[os.path.basename(path)] = len(lines) - len(keep)
        except Exception:
            pass        # 日志清理失败不影响主流程
    return out


def prune_data(keep_history: int = KEEP_HISTORY,
               keep_orders: int = KEEP_ORDERS) -> dict:
    """限制 history / orders 的条数，保留最近的部分。"""
    out = {}
    if not os.path.exists(DATA_FILE):
        return out
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return out
    changed = False
    for key, keep in (("history", keep_history), ("orders", keep_orders)):
        rows = d.get(key) or []
        if len(rows) > keep:
            out[key] = len(rows) - keep
            d[key] = rows[-keep:]
            changed = True
    if changed:
        _save(d)
    return out


_PRUNE_GAP = 6 * 3600          # 归档 6 小时一次足够，扫几万行日志不便宜
_last_prune = 0.0


def daily_prune(force: bool = False) -> dict:
    """日志截断 + 数据归档，内部 6 小时限频。"""
    global _last_prune
    now = time.time()
    if not force and now - _last_prune < _PRUNE_GAP:
        return {}
    _last_prune = now
    out = {}
    try:
        out["logs"] = prune_logs()
    except Exception:
        pass
    try:
        out["data"] = prune_data()
    except Exception:
        pass
    return out
