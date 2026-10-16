#!/usr/bin/env python3
"""时间校准与时段识别：让监督消息"看时间说话"。

两部分：
1. 自动校准：用 QQ 服务器回执里的权威时间戳（带 +08:00）校正本地时钟，
   避免因机器时钟漂移或时区配置错误导致"凌晨三点祝你早上好"。
2. 时段识别：按当前时间给出时段名与话术指引，并决定此时是否该推送
   （深夜安静时段不打扰）。
"""
from __future__ import annotations

import datetime
import os
import time

TZ = "Asia/Shanghai"
os.environ.setdefault("TZ", TZ)
try:
    time.tzset()  # 让上面的 TZ 立即生效（Linux）
except Exception:  # noqa: BLE001
    pass

_offset = 0.0        # 本地时钟相对权威时间的偏移（秒）
_synced_at = 0.0     # 上次校准时间


def sync_from_api(api_ts: str) -> float | None:
    """用 QQ API 返回的时间戳校准本地时钟。

    api_ts 形如 "2026-09-28T22:21:11+08:00"。返回偏差秒数（本地 - 权威），
    解析失败返回 None。
    """
    global _offset, _synced_at
    if not api_ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(api_ts)
        if dt.tzinfo is None:
            return None
        delta = dt.timestamp() - time.time()
        _offset = delta
        _synced_at = time.time()
        return delta
    except Exception:  # noqa: BLE001
        return None


def offset() -> float:
    """当前校准偏移（秒）。"""
    return _offset


def now() -> datetime.datetime:
    """校准后的当前本地时间。"""
    return datetime.datetime.fromtimestamp(time.time() + _offset)


# 时段表：(起始小时, 结束小时, 时段名, 监督任务, 话术指引, 是否推送)
# 安静时段 01:00-06:00 不推送，避免半夜打扰。
# 每个时段自带"监督任务"：避免用同一句话套所有时间（否则深夜会出现
# "该起床了…好好休息"这类前后矛盾的胡话）。
_PERIODS = [
    (0, 1, "深夜", "该放下手机去睡觉了，别再熬夜",
     "已过零点，对方还在熬夜。语气心疼但不容商量：立刻去睡。", True),
    (1, 6, "凌晨", "现在应该正在睡觉，别熬夜",
     "现在是凌晨，正常人该在睡觉。此阶段不推送，避免打扰。", False),
    (6, 9, "清晨", "该起床洗漱、开启新的一天了，别赖床",
     "现在是清晨，对方可能刚醒或赖床。督促起床、洗漱、开启一天。", True),
    (9, 12, "上午", "这是上午黄金时间，该专注做正事了，别摸鱼",
     "现在是上午黄金时间，督促专注学习/工作，别摸鱼、别刷手机。", True),
    (12, 14, "午间", "该吃饭、短暂午休一下了，别一直盯着屏幕",
     "现在是中午，提醒吃饭、短暂午休，别一直盯屏幕。", True),
    (14, 18, "下午", "下午容易犯困，该提提神把任务推进下去，别走神",
     "现在是下午，容易犯困走神，督促提神、把任务推进下去。", True),
    (18, 20, "傍晚", "该吃晚饭、放松一下了，顺便回顾今天的进度",
     "现在是傍晚，提醒吃晚饭、放松一下，顺便回顾今天进度。", True),
    (20, 23, "晚间", "该收尾今天的任务、做个小复盘了，别荒废",
     "现在是晚间，督促收尾今天的任务、做个小复盘，别荒废。", True),
    (23, 24, "深夜", "该放下手机准备睡觉了，别熬夜",
     "已经深夜，督促放下手机、准备睡觉，别熬夜。", True),
]


def period(dt: datetime.datetime | None = None) -> tuple[str, str, bool]:
    """返回 (时段名, 话术指引, 是否应推送)。"""
    dt = dt or now()
    h = dt.hour
    for start, end, name, _task, hint, push in _PERIODS:
        if start <= h < end:
            return name, hint, push
    return "白天", "督促对方保持专注，别摸鱼。", True


def task(dt: datetime.datetime | None = None) -> str:
    """返回该时段对应的监督任务（用作生成时的主题，而非固定话术）。"""
    dt = dt or now()
    h = dt.hour
    for start, end, _name, task_text, _hint, _push in _PERIODS:
        if start <= h < end:
            return task_text
    return "该专注做正事了，别摸鱼"


def should_push(dt: datetime.datetime | None = None) -> bool:
    """此刻是否应该推送（安静时段返回 False）。"""
    return period(dt)[2]


def weekday_hint(dt: datetime.datetime | None = None) -> str:
    """周末/工作日补充语气。"""
    dt = dt or now()
    if dt.weekday() >= 5:
        return "今天是周末，语气可以稍松一点，但仍要提醒别彻底摆烂。"
    return "今天是工作日，语气利落一点，督促把正事推进下去。"


def describe(dt: datetime.datetime | None = None) -> str:
    """一行时间描述，便于日志排查。"""
    dt = dt or now()
    name, _, push = period(dt)
    return (f"{dt:%Y-%m-%d %H:%M:%S} {dt:%A} | 时段={name} | "
            f"推送={'是' if push else '否(安静)'} | 校准偏移={_offset:+.1f}s")


if __name__ == "__main__":
    print(describe())
    print("话术指引:", period()[1])
    print("周末提示:", weekday_hint())
