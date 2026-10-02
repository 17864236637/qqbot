#!/usr/bin/env python3
"""QQ 监督员（watchlist 私聊版 + ollama 大脑）。

基于 QQ 开放平台官方客户端 qq_bot.py：
- 读取 qq_watchlist.json（QQ号 -> {name, openid}）
- 用本地 ollama 大模型为每个对象生成"有灵魂"的个性化监督话术（非固定文本）
- 逐个发私聊（C2C）；ollama 不可用时自动回退模板

配置从 .env / 环境变量读取：APP_ID / APP_SECRET / BOT_NAME / INTERVAL_SEC
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from qq_bot import QQBot, QQBotError
import qq_time  # 时间校准 + 时段识别（导入即锁定 TZ=Asia/Shanghai）

BASE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(BASE, "qq_watchlist.json")
ENV_FILE = os.path.join(BASE, ".env")
SYSTEM_PROMPT = (
    "你是「QQ监督员」，桉柠的专属监督搭子。任务：用最短的话把人从摸鱼/躺平里拽起来——该学学、该睡睡。"
    "风格：毒舌但暖、有梗、带点中二、爱call对方名字、偶尔甩网络热词，绝不爹味说教，最多两句话。"
    "示例：「桉柠你又摸鱼！起来搞学习，不然今晚别想睡。」"
    "用中文输出，只给消息正文本身，不要引号、不要解释、不要『好的』这类废话、不要重复对方名字超过一次。"
)


def load_env() -> None:
    if not os.path.exists(ENV_FILE):
        return
    for line in open(ENV_FILE, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def load_watchlist() -> dict:
    if not os.path.exists(WATCHLIST):
        return {}
    with open(WATCHLIST, encoding="utf-8") as f:
        return json.load(f)


def build_prompt(name: str, text: str, hint: str = "") -> str:
    base = f"给「{name}」发一条监督提醒：{text}"
    if hint:
        base += f"\n（时间背景：{hint}）"
    return base


def craft_message(bot_name: str, name: str, base_text: str,
                  period_name: str = "", hint: str = "") -> str:
    """优先用本地大脑生成（带时间背景），失败回退模板。"""
    ts = qq_time.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f"{bot_name}·{period_name}" if period_name else bot_name
    try:
        from qq_brain import generate
        out = generate(build_prompt(name, base_text, hint), system=SYSTEM_PROMPT)
        if out:
            return f"## {title}\n> {ts}\n\n{out}"
    except Exception as exc:  # noqa: BLE001
        print(f"  [大脑不可用，回退模板] {exc}", flush=True)
    return f"## {title}\n> {ts}\n\n{base_text}"


MAX_SEND_TRIES = 8


def _send_with_retry(bot: QQBot, oid: str, payload: dict) -> tuple[bool, object]:
    """发送，并针对『IP 不在白名单』自动重试。

    沙箱出口 IP 在多个地址间轮换（每次请求可能不同），重试数次可显著提高
    送达率。非 IP 类错误（如未扫码授权 40012004）重试无益，直接返回失败。
    """
    last = None
    for attempt in range(MAX_SEND_TRIES):
        try:
            res = bot._post(f"/v2/users/{oid}/messages", payload)
            return True, res
        except QQBotError as exc:
            last = exc
            if "白名单" not in str(exc):
                return False, exc
            if attempt < MAX_SEND_TRIES - 1:
                time.sleep(2)
    return False, last


PUSH_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "push_log.jsonl")


def log_push(name: str, oid: str, body: str, period_name: str, ok: bool,
             err: str = "") -> None:
    """把主动推送的话术正文落盘到 push_log.jsonl。

    与 qq_receiver.log_chat 同理：之前只 print 了「✅ 名字」，推送正文完全没留痕，
    事后无法复盘机器人说过什么。记录失败绝不能影响主流程。
    """
    try:
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "name": name, "openid": oid, "period": period_name,
            "out": body, "ok": ok, "err": err,
        }
        with open(PUSH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def push_once(bot: QQBot, wl: dict, bot_name: str, text: str) -> tuple[list, list]:
    sent, failed = [], []
    dt = qq_time.now()
    period_name, hint, _ = qq_time.period(dt)
    hint = f"{hint}{qq_time.weekday_hint(dt)}"
    text = text or qq_time.task(dt)  # 未指定主题时，用当前时段的监督任务
    print(f"  [时间] {qq_time.describe(dt)}", flush=True)
    for qq, info in wl.items():
        oid = info.get("openid")
        name = info.get("name", qq)
        if not oid:
            print(f"  ⏭  {name}: openid 为空，跳过", flush=True)
            continue
        body = craft_message(bot_name, name, text, period_name, hint)
        ok, res = _send_with_retry(
            bot, oid, {"msg_type": 2, "markdown": {"content": body}})
        if ok:
            # 用服务器回执里的权威时间戳校准本地时钟
            ts = res.get("timestamp") if isinstance(res, dict) else None
            delta = qq_time.sync_from_api(ts) if ts else None
            if delta is not None and abs(delta) > 5:
                print(f"  [时钟校准] 本地偏差 {delta:+.1f}s，已校正", flush=True)
            print(f"  ✅ {name}", flush=True)
            print(f"     └ 推送: {body!r}", flush=True)
            log_push(name, oid, body, period_name, True)
            sent.append(name)
        else:
            msg = str(res)
            if "40012004" in msg or "沙箱环境" in msg:
                print(f"  ❌ {name}: 未扫码授权", flush=True)
            else:
                print(f"  ❌ {name}: HTTP {getattr(res, 'status', '?')}", flush=True)
            log_push(name, oid, body, period_name, False, str(res)[:120])
            failed.append(name)
    return sent, failed


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description="QQ 监督员（watchlist 私聊版 + 大脑）")
    parser.add_argument("--once", action="store_true", help="只推一轮后退出")
    parser.add_argument("--text", default=None,
                        help="监督主题；不填则按当前时段自动选择")
    args = parser.parse_args()

    app_id = os.environ.get("APP_ID", "")
    app_secret = os.environ.get("APP_SECRET", "")
    bot_name = os.environ.get("BOT_NAME", "QQ监督员")
    interval = int(os.environ.get("INTERVAL_SEC", "300"))

    if not (app_id and app_secret):
        print("缺少 APP_ID / APP_SECRET，请检查 .env", flush=True)
        sys.exit(1)

    bot = QQBot(app_id, app_secret)
    wl = load_watchlist()
    if not wl:
        print("watchlist 为空，请检查 qq_watchlist.json", flush=True)
        sys.exit(1)

    if args.once:
        print(f"[{bot_name}] 单轮推送 {len(wl)} 个目标（含大脑生成）", flush=True)
        push_once(bot, wl, bot_name, args.text)
        return

    print(f"[{bot_name}] 监管循环启动，间隔 {interval}s，目标 {len(wl)} 个", flush=True)
    while True:
        if not qq_time.should_push():
            print(f"[{bot_name}] 安静时段，跳过本轮 | {qq_time.describe()}", flush=True)
        else:
            push_once(bot, wl, bot_name, args.text)
        time.sleep(interval)


if __name__ == "__main__":
    main()
