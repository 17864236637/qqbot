"""QQ 机器人客户端：Access Token 获取 + 群消息推送（文本/Markdown）。

依据 QQ 开放平台官方文档：
- 换取凭证：POST https://api.bot.qq.com/app/getAppAccessToken
  （body: {appId, clientSecret}；返回 access_token，有效期 7200s）
- 群消息：POST https://api.bot.qq.com/v2/groups/{group_openid}/messages
  （header: Authorization: QQBot <token>）
网络抖动/5xx 自动退避重试；错误带 HTTP status 便于定位。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

API_BASE = "https://api.bot.qq.com"
TIMEOUT = 15

# 重试：次数与退避基数（网络异常/5xx 重试，4xx 不重试）
MAX_RETRIES = 2
RETRY_BACKOFF = 2.0


class QQBotError(Exception):
    """QQ 机器人 API 调用失败。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _retry_request(method: str, path: str, payload: dict | None,
                   headers: dict) -> tuple[int, dict | None]:
    """带退避重试的 POST 请求，返回 (status, data_json)。"""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    for attempt in range(MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                API_BASE + path, data=body, method=method, headers=headers
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                text = resp.read().decode("utf-8") or ""
                return resp.status, (json.loads(text) if text else None)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:300]
            if attempt >= MAX_RETRIES or exc.code < 500:
                raise QQBotError(
                    f"QQ API [{method} {path}]: HTTP {exc.code} {detail}",
                    status=exc.code,
                ) from exc
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
        except Exception as exc:
            if attempt >= MAX_RETRIES:
                raise QQBotError(f"QQ API [{method} {path}] 请求失败: {exc}") from exc
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
    raise AssertionError("不可达")  # 逻辑上不会执行


class QQBot:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: str | None = None
        self._token_expire: float = 0

    def get_access_token(self) -> str:
        """获取 access_token（7200s 有效期内缓存复用，临近过期自动刷新）。"""
        now = time.time()
        if self._token and now < self._token_expire - 60:
            return self._token
        status, data = _retry_request(
            "POST", "/app/getAppAccessToken",
            {"appId": self.app_id, "clientSecret": self.app_secret},
            {"Content-Type": "application/json"},
        )
        token = (data or {}).get("access_token")
        if not token:
            raise QQBotError(f"QQ access_token 返回异常: {data}", status=status)
        self._token = token
        self._token_expire = now + int((data or {}).get("expires_in", 7200))
        return token

    def _post(self, path: str, payload: dict) -> dict:
        token = self.get_access_token()
        status, data = _retry_request(
            "POST", path, payload,
            {"Authorization": f"QQBot {token}", "Content-Type": "application/json"},
        )
        return data or {}

    def send_group_message(self, group_openid: str, content: str,
                           msg_type: int = 0) -> dict:
        """发送群消息。

        msg_type: 0=纯文本(content)；2=Markdown(markdown.content)。
        """
        if msg_type == 2:
            payload = {"msg_type": 2, "markdown": {"content": content}}
        else:
            payload = {"msg_type": 0, "content": content}
        return self._post(f"/v2/groups/{group_openid}/messages", payload)

    def send_text(self, group_openid: str, text: str) -> dict:
        return self.send_group_message(group_openid, text, msg_type=0)

    def send_markdown(self, group_openid: str, markdown: str) -> dict:
        return self.send_group_message(group_openid, markdown, msg_type=2)
