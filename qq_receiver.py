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
- 沙箱模式下 WebSocket 会被服务器以 op=9 拒绝（Invalid Session），
  这是开放平台限制，发布/上架机器人后自动解除，代码侧无法绕过。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import websocket  # websocket-client
from qq_bot import QQBot, QQBotError

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
ENV_FILE = os.path.join(BASE, ".env")
import qq_scoreboard as sb
import qq_rules as rl
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
        self._cron_start()  # 启动积分定时线程（8/21点提醒、每月重置、≤3分会会议）

    def _token(self) -> str:
        return "QQBot " + self.bot.get_access_token()

    def _gateway_url(self) -> str:
        import urllib.request
        r = urllib.request.Request(GATEWAY, headers={"Authorization": self._token()})
        with urllib.request.urlopen(r, timeout=15) as resp:
            return json.loads(resp.read().decode())["url"]

    # ----- websocket 回调 -----
    def on_open(self, ws) -> None:
        print(f"[{self.bot_name}] WS 已连接，等待 Hello", flush=True)

    def on_message(self, ws, raw: str) -> None:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return
        op = obj.get("op")
        if op == 10:  # Hello
            self.hb_interval = max(1, obj["d"]["heartbeat_interval"] / 1000)
            self._identify(ws)
            self._heartbeat_loop(ws)
        elif op == 11:  # Heartbeat ACK
            pass
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
        def _loop() -> None:
            while self.alive:
                self._heartbeat(ws)
                time.sleep(self.hb_interval)
        threading.Thread(target=_loop, daemon=True).start()

    # ----- 业务 -----
    def _handle_event(self, obj) -> None:
        t = obj.get("t")
        d = obj.get("d", {})
        if t in ("AT_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
            self._reply(d)

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
        content = msg.get("content", "").replace("@", "").strip()
        group_id = msg.get("group_id")
        author = msg.get("author", {})
        user_openid = author.get("user_openid") or author.get("member_openid")
        msg_id = msg.get("id")
        print(f"[{self.bot_name}] 收到消息: {content!r}", flush=True)
        # ---- 群内积分：违纪检测 + 扣分 ----
        score_extra = ""
        if group_id and user_openid:
            reasons, score = self._process_score(group_id, user_openid, content, author)
            if reasons:
                rs = "；".join(f"{r}({d:+d})" for r, d in reasons)
                score_extra = f"\n【本次判定】{rs}。该成员当前积分 {score} 分。"
            else:
                score_extra = f"\n该成员当前积分 {score} 分。"
        try:
            from qq_brain import generate
            reply = generate(
                f"对方说：{content}",
                system=f"你是「{self.bot_name}」——执行积分制督学的严格小管理员。每人初始10分、每月重置。规则：①每天8:00与21:00报备手机时长(标准7h，超时扣1、熬夜扣1)；②禁刷屏、禁骂人(各扣1)；③0点宵禁后发消息扣1；④不得随意惹事(扣1)；⑤写作业群里报备手机时长对比(玩手机扣1、不按时提交扣2)；⑥剩3分即开会议。违规记录、分值、惩罚账目群内存档。风格：毒舌但讲规矩、有梗、带中二、爱call名字，不爹味。最多两句话，不要解释规则，只给监督反馈或扣分播报。{score_extra}",
                max_tokens=60, temperature=0.9,
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

    def _process_score(self, group_id, user_openid, content, author) -> tuple:
        """群消息积分：违纪检测 + 扣分，返回 (扣分记录[(原因,分值)], 当前分数)。"""
        name = author.get("nickname") or author.get("user_name") or user_openid
        sb.ensure_member(group_id, user_openid, name)
        reasons = []
        # 写作业报备状态机
        if rl.is_hw_start(content):
            if rl.start_homework(group_id, user_openid):
                reasons.append(("报备开始写作业", 0))
        elif rl.is_hw_end(content):
            play, overdue, dur = rl.end_homework(group_id, user_openid)
            if play:
                reasons.append(("写作业时玩手机", -1))
            if overdue:
                reasons.append(("不按时提交作业", -2))
            if not play and not overdue:
                reasons.append(("按时完成作业", 0))
        else:
            rl.note_homework_msg(group_id, user_openid)
        # 违纪检测（所有消息都查）
        if rl.check_spam(group_id, user_openid):
            reasons.append(("刷屏", -1))
        if rl.check_badword(content):
            reasons.append(("骂人", -1))
        if rl.check_curfew():
            reasons.append(("宵禁发消息", -1))
        if rl.check_provoke(content):
            reasons.append(("惹事", -1))
        # 执行扣分
        score = sb.get_score(group_id, user_openid)
        for reason, delta in reasons:
            if delta != 0:
                score, _ = sb.deduct(group_id, user_openid, delta, reason, name)
        return reasons, score

    def _cron_start(self) -> None:
        """启动积分定时线程：8/21 点报备提醒、每月重置、≤3 分召会议。不加载模型。"""

        def loop() -> None:
            print(f"[{self.bot_name}] 积分定时线程已启动", flush=True)
            last_month = time.strftime("%Y-%m")
            last_hour = -1
            while self.alive:
                try:
                    now = time.localtime()
                    m = time.strftime("%Y-%m")
                    if m != last_month:
                        sb.monthly_reset_if_needed(m)
                        last_month = m
                    if now.tm_hour != last_hour:
                        last_hour = now.tm_hour
                        if now.tm_hour in (8, 21):
                            for gid in sb.all_groups():
                                self._cron_push(
                                    gid,
                                    "【积分督学】报备手机时长！标准7h，超时扣1分、熬夜扣1分。"
                                    "格式示例：我手机用了Xh",
                                )
                        for gid in sb.all_groups():
                            snap = sb.snapshot(gid) or []
                            low = [x["name"] for x in snap if x["score"] <= 3]
                            if low:
                                self._cron_push(
                                    gid,
                                    f"【会议通知】以下成员分数≤3，召开复盘会议：{', '.join(low)}",
                                )
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
        while self.alive:
            try:
                url = self._gateway_url()
                self.ws = websocket.WebSocketApp(
                    url,
                    header={"Authorization": self._token(), "X-Shard-Id": "0"},
                    on_open=self.on_open, on_message=self.on_message,
                    on_error=self.on_error, on_close=self.on_close,
                )
                self.ws.run_forever()
                delay = 5  # 正常退出（如被踢）后重置退避
            except Exception as exc:
                print(f"[{self.bot_name}] 连接失败: {exc}", flush=True)
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
