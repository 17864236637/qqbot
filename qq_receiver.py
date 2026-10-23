#!/usr/bin/env python3
"""QQ 机器人接收端（WebSocket 长连接）。

连接 QQ开放平台网关，接收单聊(C2C)与群@消息，用 qq_brain 生成回复并回发，
实现"能听能回"的双向监督。

依赖：websocket-client（pip install websocket-client）

说明：
- 意图位 GROUP_AND_C2C_EVENT = 1 << 25 已同时覆盖：
    C2C_MESSAGE_CREATE（用户单聊发消息给机器人）
    GROUP_AT_MESSAGE_CREATE（用户在群里 @机器人）
  因此私聊对话无需叠加其他位，发布后即可双向对话。
- 群消息事件类型分两种情况（2025-10 实测踩坑）：
    后台只开"@消息"    -> 来的是 GROUP_AT_MESSAGE_CREATE
    后台开"接收所有消息" -> 每句话都来 GROUP_MESSAGE_CREATE（@ 机器人的也是它）
  只写前者不写后者，全量模式下群 @ 会被当成不存在，永远不回话。
- 督学玩法是「对赌契约」（qq_wager.py）：周一发起、周日验收、全员见证。
  积分制已于 2026-10 停用（分数没有后果，规则沦为恐吓）——
  数据与代码留坟于 qq_scoreboard.py / qq_rules.py + git 历史，想复活随时。
- 沙箱模式下 WebSocket 会被服务器以 op=9 拒绝（Invalid Session），
  这是开放平台限制，发布/上架机器人后自动解除，代码侧无法绕过。
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
import time

import websocket  # websocket-client
from qq_bot import QQBot, QQBotError
from qq_profiles import remember, get_name, extract_self_intro, apply_author

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
ENV_FILE = os.path.join(BASE, ".env")
import qq_scoreboard as sb
import qq_wager as wg
GATEWAY = "https://api.bot.qq.com/gateway/bot"

# 意图位：GROUP_AND_C2C_EVENT = 1 << 25
# 官方文档确认该位同时覆盖单聊与群@消息，私聊对话无需额外位。
INTENTS = 1 << 25


def load_env() -> None:
    if not os.path.exists(ENV_FILE):
        return
    for line in open(ENV_FILE, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


CHAT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_log.jsonl")


def log_chat(scope: str, peer: str, user_openid: str, inbound: str,
             outbound: str, ok: bool) -> None:
    """把一次完整对话（用户说了什么 + 机器人回了什么）追加到 chat_log.jsonl。

    教训（务必保留）：之前只 print 了「收到消息」，机器人回复内容完全没落盘，
    导致事后既无法整理聊天记录，也无法复盘大脑话术质量。这里补齐落盘。
    记录失败绝不能影响主流程，所以全程吞异常。
    """
    try:
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scope": scope,            # private / group
            "peer": peer,              # 私聊 openid 或群 openid
            "user_openid": user_openid,
            "in": inbound,             # 用户说的话
            "out": outbound,           # 机器人回的话
            "ok": ok,                  # 是否发送成功
        }
        with open(CHAT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


class Receiver:
    def __init__(self) -> None:
        self.app_id = os.environ.get("APP_ID", "")
        self.app_secret = os.environ.get("APP_SECRET", "")
        self.bot_name = os.environ.get("BOT_NAME", "QQ监督员")
        if not (self.app_id and self.app_secret):
            raise SystemExit("缺少 APP_ID / APP_SECRET")
        self.bot = QQBot(self.app_id, self.app_secret)
        self.ws = None
        self.seq = None
        self.hb_interval = 30
        self.alive = True
        self.seen_msg_ids = set()  # 去重：防止沙箱/网络重推导致重复回复
        self._last_recv = time.monotonic()  # 最近一次收到 WS 帧的时间（僵尸会话检测）
        self._hb_ws = None                  # 当前心跳线程服务的 ws（防旧线程泄漏）
        self.bot_id = None                  # 机器人自身 ID（READY 下发），全量模式判 @ 用
        self._cron_start()  # 启动积分定时线程（8/21点提醒、每月重置、≤3分会会议）

    def _token(self) -> str:
        return "QQBot " + self.bot.get_access_token()

    def _gateway_url(self) -> str:
        import urllib.error
        import urllib.request
        r = urllib.request.Request(GATEWAY, headers={"Authorization": self._token()})
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return json.loads(resp.read().decode())["url"]
        except urllib.error.HTTPError as exc:
            # 必须读出响应体：QQ 的真实错误码在里面（11298=IP不在白名单等）。
            # 丢弃 body 只看到裸 "HTTP Error 401"，等于放弃诊断，历代排查都吃过这个亏。
            body = exc.read().decode("utf-8", errors="ignore")[:200]
            raise QQBotError(f"gateway: HTTP {exc.code} {body}", status=exc.code) from exc

    # ----- websocket 回调 -----
    def on_open(self, ws) -> None:
        print(f"[{self.bot_name}] WS 已连接，等待 Hello", flush=True)

    def on_message(self, ws, raw: str) -> None:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return
        self._last_recv = time.monotonic()
        op = obj.get("op")
        if op == 10:  # Hello
            self.hb_interval = max(1, obj["d"]["heartbeat_interval"] / 1000)
            self._identify(ws)
            self._heartbeat_loop(ws)
        elif op == 11:  # Heartbeat ACK —— 会话存活的铁证，留痕便于诊断"连着却不回话"
            print(f"[{self.bot_name}] 心跳 ACK op=11", flush=True)
        elif op == 1:  # 服务端要求心跳
            self._heartbeat(ws)
        elif op == 0:  # Dispatch
            self.seq = obj.get("s")
            self._handle_event(obj)
        elif op in (7, 9):  # Reconnect / Invalid Session
            print(f"[{self.bot_name}] 收到重连指令 op={op}", flush=True)
            try:
                ws.close()
            except Exception:
                pass

    def on_error(self, ws, err) -> None:
        print(f"[{self.bot_name}] WS 错误: {err}", flush=True)

    def on_close(self, ws, *a) -> None:
        print(f"[{self.bot_name}] WS 关闭，稍后重连", flush=True)

    # ----- 协议动作 -----
    def _identify(self, ws) -> None:
        ws.send(json.dumps({
            "op": 2,
            "d": {"token": self._token(), "intents": INTENTS, "shard": [0, 1]},
        }))
        print(f"[{self.bot_name}] 已发送 Identify", flush=True)

    def _heartbeat(self, ws) -> None:
        try:
            ws.send(json.dumps({"op": 1, "d": self.seq}))
        except Exception:
            pass

    def _heartbeat_loop(self, ws) -> None:
        self._hb_ws = ws

        def _loop() -> None:
            while self.alive and self._hb_ws is ws:
                self._heartbeat(ws)
                # 僵尸会话检测：超过阈值没收到任何帧（含心跳 ACK）-> 强制关闭触发外层重连。
                # 教训：TCP 可能假活——服务端应用层已踢掉会话、底层连接却还在，
                # 此时 recv 永久阻塞、on_close 永不触发，机器人呈"连着却不回话"的假死态，
                # 日志一片空白，极难排查。只能靠"多久没收到帧"主动戳破。
                if time.monotonic() - self._last_recv > max(120.0, 3 * self.hb_interval):
                    print(f"[{self.bot_name}] 会话无响应，强制重连", flush=True)
                    try:
                        ws.close()
                    except Exception:
                        pass
                    return
                time.sleep(self.hb_interval)

        threading.Thread(target=_loop, daemon=True).start()

    # ----- 业务 -----
    def _handle_event(self, obj) -> None:
        t = obj.get("t")
        d = obj.get("d", {})
        # 诊断留痕：任何 dispatch 事件的原始体都截断落盘。
        # 教训：平台事件字段会变迁（如群标识 group_openid），不留原始体等于盲猜；
        # 且截断必须够长——曾经 [:500] 恰好把 mentions 数组切掉，
        # 事后根本没法复盘"这条到底是不是在 @ 机器人"。
        print(f"[{self.bot_name}] 事件 t={t} | {json.dumps(d, ensure_ascii=False)[:900]}",
              flush=True)
        if t == "READY":
            # 会话就绪：服务端下发机器人自身 ID，全量模式下判"是不是在@我"就靠它。
            # 教训：曾经不存 bot_id，导致全量消息来了也只能干瞪眼。
            self.bot_id = (d.get("user") or {}).get("id")
            print(f"[{self.bot_name}] 会话就绪 bot_id={self.bot_id}", flush=True)
            return
        if t in ("AT_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
            self._reply(d)
        elif t == "GROUP_MESSAGE_CREATE":
            # 登记群：cron 的推送目标自生长，不依赖任何玩法是否开启
            # （积分制停用后 sb.all_groups() 就是空的，吃过这个亏）
            gid = d.get("group_openid") or d.get("group_id")
            if gid:
                wg.note_group(gid)
            # 根因（2025-10 实测）：后台开了"接收所有消息"（全量模式）后，
            # 群里每句话都推这个事件类型——@ 机器人的消息也是它。
            # 旧代码只认 GROUP_AT_MESSAGE_CREATE，于是群里的一切都被当成不存在，
            # 表现为"@ 它永远不回"。全量模式不能来一句回一句（API 限额+骚扰），
            # 只有被 @ 到才回。
            if self._is_bot_mentioned(d):
                self._reply(d)

    def _is_bot_mentioned(self, d: dict) -> bool:
        """判断一条群消息是不是在 @ 机器人。

        mentions 数组的字段名历史上变过（id / user_openid / member_openid），
        有的版本带 is_you 标记；content 里 @ 标签也有 <@id> 和 <@!id> 两种写法。
        全部兼容——宁可多判不可漏判，漏判的代价是用户以为机器人死了。
        """
        if not self.bot_id:
            return False
        for m in d.get("mentions") or []:
            if not isinstance(m, dict):
                continue
            if m.get("is_you"):
                return True
            if self.bot_id in (m.get("id"), m.get("user_openid"), m.get("member_openid")):
                return True
            user = m.get("user")
            if isinstance(user, dict) and user.get("id") == self.bot_id:
                return True
        return bool(re.search(r"<@!?" + re.escape(self.bot_id) + r">",
                              d.get("content") or ""))

    def _post_retry(self, path: str, payload: dict, tries: int = 8) -> tuple[bool, object]:
        """发送并针对『IP 不在白名单』重试。

        沙箱出口 IP 每次请求可能不同，重试可显著提高送达率（与监管端同理）。
        """
        last = None
        for i in range(tries):
            try:
                self.bot._post(path, payload)
                return True, None
            except QQBotError as exc:
                last = exc
                if "白名单" not in str(exc):
                    return False, exc
                if i < tries - 1:
                    time.sleep(2)
        return False, last

    def _reply(self, msg: dict) -> None:
        mid = msg.get("id")
        if mid and mid in self.seen_msg_ids:
            return  # 同一条消息被重复推送，跳过
        if mid:
            self.seen_msg_ids.add(mid)
        # QQ 群@消息 content 形如 "<@!bot_openid>你好"；私聊/群文本也可能带 "@昵称"。
        # 仅 replace("@","") 会残留 "<!bot_openid>" 垃圾前缀，必须把 @标签整体剥掉，
        # 否则模型拿到的正文是脏的，回复易跑偏。
        raw = msg.get("content") or ""
        content = re.sub(r"<@!?[A-Za-z0-9_]+>", "", raw)   # 去掉 <@botid> / <!botid>
        content = re.sub(r"^@\S+\s*", "", content).strip()   # 去掉开头 "@昵称 "
        if not content:
            content = "（对方只@了你，没说话）"
        # 官方事件体字段是 group_openid（群 OpenID），老字段名 group_id 兜底兼容
        group_id = msg.get("group_openid") or msg.get("group_id")
        author = msg.get("author", {})
        user_openid = author.get("user_openid") or author.get("member_openid")
        msg_id = msg.get("id")
        print(f"[{self.bot_name}] 收到消息: {content!r}", flush=True)
        # ---- 自动识别并记住用户名/昵称 ----
        scope = "group" if group_id else "private"
        apply_author(user_openid, author, scope)            # QQ 下发昵称兜底
        intro = extract_self_intro(content)                 # 对话内"我是X/叫我X"
        if intro:
            remember(user_openid, intro, scope)
            print(f"[{self.bot_name}] 已记住 {user_openid[:8]}… 叫 {intro}", flush=True)
        name = get_name(user_openid)
        # ---- 命令直达：契约/战报是纯规则，绝不劳烦 7B 模型 ----
        # 教训：这类结构化状态让模型生成，它会把 10 分说成 8 分还一脸自信，
        # 且模型冷启动要占 6.5G 内存、等一两分钟，记个赌约不值得。
        if self._try_wager(group_id, content, msg_id, user_openid, name):
            return
        try:
            from qq_brain import generate
            reply = generate(
                f"对方说：{content}" + (f"（记住：对方叫 {name}，直接称呼其名）"
                                       if name else "（你还不知道对方名字，不要编造，用'你'称呼即可）"),
                system=(
                    (f"你是「{self.bot_name}」——群里同学的学习搭子，嘴毒但真希望他们好。"
                     f"你的正职是'赌约公证人'：每周一早上9点发起本周学习赌约"
                     f"（同学@我发'我赌+目标'即可入局，如'我赌数学作业全写完'），"
                     f"周日晚上8点主持验收（同学回'完成'或'输了'了结赌约）。"
                     f"【铁律】赌约名单和结果由系统记录，你只能转述系统给的名单，"
                     f"绝不编造别人的赌约、进度或结果；群里没有'扣分''积分'这回事，"
                     f"严禁说出任何扣分/记账相关字眼；别人问榜就说'本周战报'让系统来。"
                     f"风格：毒舌但暖、有梗、带中二、爱call名字，不爹味。最多两句话。"
                     f"只给消息正文，不要引号、不要'好的'之类废话。")
                    if group_id else
                    (f"你是「{self.bot_name}」——专门监督好好学习的毒舌小助手，核心算法是'监督学习'（监督的是人的学习）。"
                     f"风格：毒舌但暖、有梗、带中二、爱call对方名字、偶尔甩热词，绝不爹味说教，最多两句话。"
                     f"用最短的话把人从摸鱼/躺平里拽起来。不涉及扣分与记账，只催学。只给消息正文，不要引号、不要'好的'之类废话。")
                ),
                max_tokens=60, temperature=0.7,
            )
        except Exception as exc:
            print(f"[{self.bot_name}] 大脑失败: {exc}", flush=True)
            reply = "我在的，有什么事？"
        if not reply:
            reply = "我在的。"
        if group_id:
            payload = {"msg_type": 2, "markdown": {"content": reply}}
            if msg_id:
                payload["msg_id"] = msg_id  # 被动回复，绕开主动消息限制
            ok, err = self._post_retry(f"/v2/groups/{group_id}/messages", payload)
            print(f"[{self.bot_name}] {'已回群' if ok else '群回复失败'} "
                  f"{group_id}{'' if ok else ': ' + str(err)[:80]}", flush=True)
            print(f"[{self.bot_name}] 回复内容: {reply!r}", flush=True)
            log_chat("group", group_id, user_openid or "", content, reply, ok)
        elif user_openid:
            payload = {"msg_type": 2, "markdown": {"content": reply}}
            if msg_id:
                payload["msg_id"] = msg_id
            ok, err = self._post_retry(f"/v2/users/{user_openid}/messages", payload)
            print(f"[{self.bot_name}] {'已回私聊' if ok else '私聊回复失败'} "
                  f"{user_openid}{'' if ok else ': ' + str(err)[:80]}", flush=True)
            print(f"[{self.bot_name}] 回复内容: {reply!r}", flush=True)
            log_chat("private", user_openid, user_openid, content, reply, ok)
        else:
            # 教训：曾经这里两不管——回复生成了却静默丢弃，日志无任何痕迹，
            # 表现为"机器人在生成（内存暴涨）但永远不吭声"。必须留痕。
            print(f"[{self.bot_name}] ⚠️ 回复无处可发：事件既无群标识也无用户标识。"
                  f"author={json.dumps(author, ensure_ascii=False)[:200]} "
                  f"msg_keys={sorted(msg.keys())}", flush=True)

    # ---- 赌约命令（纯规则，不过模型）----
    # 验收词刻意用全等匹配而非子串：日常聊天里"完成了""没做完"遍地都是，
    # 用子串会把闲聊全判成认账，赌约账本就废了。
    DONE_WORDS = ("完成", "我完成了", "做到了", "我做到了", "兑现", "已兑现")
    LOST_WORDS = ("输了", "我输了", "赖账", "没做到", "没完成", "翻车", "翻车了")

    def _try_wager(self, group_id, content: str, msg_id, user_openid, name) -> bool:
        """契约命令：我赌+目标 / 完成 / 输了 / 战报。命中则被动回复，返回 True。"""
        if not group_id or not content or not user_openid:
            return False
        stripped = content.strip()
        # 入局：我赌 xxx / 赌 xxx
        m = re.match(r"^(?:我)?赌\s*(.+)$", stripped)
        if m:
            text = wg.join_bet(group_id, user_openid, name or "", m.group(1))
            ok, err = self._send_text(group_id, text, msg_id)
            print(f"[{self.bot_name}] 契约入局 {'已回' if ok else '失败: ' + str(err)[:80]}",
                  flush=True)
            log_chat("group", group_id, user_openid, content, text, ok)
            return True
        if stripped in self.DONE_WORDS:
            text = wg.settle(group_id, user_openid, done=True)
            ok, err = self._send_text(group_id, text, msg_id)
            print(f"[{self.bot_name}] 契约兑现 {'已回' if ok else '失败: ' + str(err)[:80]}",
                  flush=True)
            log_chat("group", group_id, user_openid, content, text, ok)
            return True
        if stripped in self.LOST_WORDS:
            text = wg.settle(group_id, user_openid, done=False)
            ok, err = self._send_text(group_id, text, msg_id)
            print(f"[{self.bot_name}] 契约认栽 {'已回' if ok else '失败: ' + str(err)[:80]}",
                  flush=True)
            log_chat("group", group_id, user_openid, content, text, ok)
            return True
        if any(k in content for k in ("战报", "赌约", "契约")):
            text = self._wager_report_text(group_id)
            ok, err = self._send_text(group_id, text, msg_id)
            print(f"[{self.bot_name}] 战报查询 {'已回' if ok else '失败: ' + str(err)[:80]}",
                  flush=True)
            log_chat("group", group_id, user_openid, content, text, ok)
            return True
        return False

    def _send_text(self, group_id: str, text: str, msg_id) -> tuple[bool, object]:
        """群内被动回复纯文本。

        刻意用 msg_type 0 而不是 2(markdown)：markdown 消息需要机器人有
        "原生 markdown"权限，没开的话发送直接失败——榜单这种纯文本没必要赌。
        """
        payload = {"msg_type": 0, "content": text}
        if msg_id:
            payload["msg_id"] = msg_id  # 被动回复，绕开主动消息限额
        return self._post_retry(f"/v2/groups/{group_id}/messages", payload)

    def _wager_report_text(self, group_id: str) -> str:
        """本周赌约战报。"""
        bs = wg.bets(group_id)
        if not bs:
            return ("本周还没人入局。参与方式：@我 + 「我赌+你的目标」\n"
                    "例：我赌数学作业全写完。周日晚8点验收，全员见证。")
        state = {"pending": "⏳待验收", "done": "✅兑现",
                 "lost": "❌认栽", "overdue": "⚠️逾期"}
        lines = [f"⚔️ 本周赌约战报（{wg.week_key()}）"]
        for b in bs:
            lines.append(f"· {b['name']}：{b['goal']} —— {state.get(b['status'], b['status'])}")
        pend = [b for b in bs if b["status"] == "pending"]
        if pend:
            names = "、".join(b["name"] for b in pend)
            lines.append(f"\n待验收：{names}。做到了回「完成」，赖账回「输了」。")
        else:
            lines.append("\n全员已认账，本周闭幕。下周一再战。")
        return "\n".join(lines)

    def _wager_open_text(self, group_id: str) -> str:
        """周一早上的契约发起文案；带上周逾期未认的点名。"""
        lines = ["📢 本周学习契约开启！",
                 "参与方式：@我 发送「我赌+你的目标」",
                 "例：我赌数学作业全写完",
                 "规则：周日晚8点验收，做到了回「完成」，赖账回「输了」，全员见证。",
                 "输了的惩罚群规自定（红包/头像/叫爸爸都行），机器人只公证，不动手。"]
        od = wg.overdue_last_week(group_id)
        if od:
            names = "、".join(b["name"] for b in od)
            lines.append(f"⚠️ 上周逾期未认账：{names}。账不会消失，只会挂在那儿。")
        return "\n".join(lines)

    def _wager_check_text(self, group_id: str) -> str:
        """周日晚上验收文案。"""
        bs = wg.bets(group_id)
        if not bs:
            return ("⏰ 本周无人入局，契约空转。\n"
                    "想玩的随时@我发「我赌+目标」，下周一开始计。")
        mark = {"done": "✅", "lost": "❌", "overdue": "⚠️", "pending": "⏳"}
        lines = ["⏰ 本周赌约验收开始！", ""]
        for b in bs:
            lines.append(f"{mark.get(b['status'], '·')} {b['name']}：{b['goal']}")
        pend = [b for b in bs if b["status"] == "pending"]
        if pend:
            names = "、".join(b["name"] for b in pend)
            lines.append(f"\n还没认账的：{names}。做到了回「完成」，赖账回「输了」，全员见证。")
        else:
            lines.append("\n全员已认账，本周闭幕。下周一再战。")
        return "\n".join(lines)

    def _cron_start(self) -> None:
        """启动契约定时线程：周一9点发起赌约、周日20点验收、8/21点催学。不加载模型。"""

        def loop() -> None:
            print(f"[{self.bot_name}] 契约定时线程已启动", flush=True)
            last_hour = -1
            # 启动时静默：本周已过的周期动作不补发，只等下一个触发点
            last_fired: set[str] = set()
            while self.alive:
                try:
                    now = time.localtime()
                    # 周一 9:00 发起本周赌约（tm_wday: 周一=0 … 周日=6）
                    if now.tm_wday == 0 and now.tm_hour == 9 and "open" not in last_fired:
                        last_fired.add("open")
                        for gid in wg.known_groups():
                            self._cron_push(gid, self._wager_open_text(gid))
                    # 周日 20:00 主持验收
                    if now.tm_wday == 6 and now.tm_hour == 20 and "check" not in last_fired:
                        last_fired.add("check")
                        for gid in wg.known_groups():
                            self._cron_push(gid, self._wager_check_text(gid))
                    # 周一 0 点跨周，放行下一轮触发
                    if now.tm_wday == 0 and now.tm_hour == 0:
                        last_fired.discard("open")
                        last_fired.discard("check")
                    if now.tm_hour != last_hour:
                        last_hour = now.tm_hour
                        # 8/21 点催学：只留节奏，不带任何积分色彩
                        if now.tm_hour in (8, 21):
                            text = ("【催学】新的一天。今天打算学点啥？"
                                    "@我 发「我赌+目标」即可入局本周赌约。"
                                    if now.tm_hour == 8 else
                                    "【复盘】今晚复盘：今天的赌约推进得怎么样了？"
                                    "没动静的自己心里有数。")
                            for gid in wg.known_groups():
                                self._cron_push(gid, text)
                except Exception as exc:  # noqa: BLE001
                    print(f"[{self.bot_name}] cron 异常: {exc}", flush=True)
                time.sleep(30)

        threading.Thread(target=loop, daemon=True).start()

    def _cron_push(self, gid: str, text: str) -> None:
        ok, err = self._post_retry(
            f"/v2/groups/{gid}/messages", {"msg_type": 0, "content": text}
        )
        if not ok:
            print(f"[{self.bot_name}] cron 推送失败 {gid}: {err}", flush=True)

    def run(self) -> None:
        delay = 5
        fast_left = 0  # 剩余快速重试次数：沙箱出口 IP 每请求随机变，没抽中白名单 IP 就别干等
        while self.alive:
            try:
                url = self._gateway_url()
                self.ws = websocket.WebSocketApp(
                    url,
                    header={"Authorization": self._token(), "X-Shard-Id": "0"},
                    on_open=self.on_open, on_message=self.on_message,
                    on_error=self.on_error, on_close=self.on_close,
                )
                born = time.monotonic()
                self.ws.run_forever()
                delay = 5  # 正常退出（如被踢）后重置退避
                if time.monotonic() - born < 30:
                    # 秒级夭折 = 抽奖没中（出口 IP 不在白名单 / Identify 被 op=9 拒）。
                    # 每次重试都是新连接、新出口 IP，短间隔连试即可提高命中率。
                    fast_left = max(fast_left, 12)
            except QQBotError as exc:
                print(f"[{self.bot_name}] 连接失败: {exc}", flush=True)
                if exc.status in (401, 403) or "白名单" in str(exc):
                    fast_left = max(fast_left, 12)
            except Exception as exc:
                print(f"[{self.bot_name}] 连接失败: {exc}", flush=True)
            if fast_left > 0:
                fast_left -= 1
                print(f"[{self.bot_name}] 快速重试（剩 {fast_left} 次）", flush=True)
                time.sleep(1.5)
                continue
            # 指数退避：封顶 120s。
            # 教训：QQ 网关对短时间内反复鉴权会返回 100017（接口调用超过频率限制），
            # 把重试压得太密会把白名单刚配好的窗口也一起吃掉，所以上限调到 120s。
            print(f"[{self.bot_name}] {delay}s 后重连", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 120)


def main() -> None:
    load_env()
    Receiver().run()


if __name__ == "__main__":
    main()
