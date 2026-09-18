"""通用 HTML 表单认证。

当你学校的门户既不是锐捷也不是深澜，而是一个普通的 ``<form>`` 时使用：

1. 打开认证页，找到登录表单的 ``action`` 和所有 ``<input name=...>``；
2. 把参数名写进 ``fields``（``{username}`` / ``{password}`` 会被替换）；
3. 也可以打开浏览器 F12 -> Network，直接照抄登录请求。

配置示例::

    "provider": "generic_form",
    "options": {
      "login_url": "http://10.0.0.1/login",
      "method": "POST",
      "fields": {
        "username": "{username}",
        "password": "{password}",
        "domain": "default"
      },
      "success_contains": ["success", "认证成功"],
      "failure_contains": ["fail", "错误"]
    }
"""

from __future__ import annotations

import json
from typing import Any

from .base import LoginResult, Provider

__all__ = ["GenericFormProvider", "PROVIDER"]


class GenericFormProvider(Provider):
    name = "generic_form"
    description = "通用 HTML 表单 POST（自己填写 action 和字段）"
    required_options = ("login_url",)
    example_options = {
        "login_url": "http://10.0.0.1/login",
        "method": "POST",
        "content_type": "form",
        "fields": {
            "username": "{username}",
            "password": "{password}",
        },
        "extra_headers": {},
        "success_contains": ["success", "ok", "认证成功"],
        "failure_contains": ["fail", "error", "错误"],
    }
    notes = (
        "fields 里写 {username} / {password} 占位符即可。\n"
        "  content_type 可选 form（默认）或 json。\n"
        "  判断成功：响应里出现 success_contains 中任意一个词，且不含 failure_contains。"
    )

    def _substitute(self, value: Any) -> Any:
        if isinstance(value, str):
            return (value.replace("{username}", self.cfg.username)
                         .replace("{password}", self.cfg.password()))
        if isinstance(value, dict):
            return {k: self._substitute(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._substitute(v) for v in value]
        return value

    def login(self) -> LoginResult:
        url = str(self.cfg.option("login_url"))
        method = str(self.cfg.option("method", "POST")).upper()
        content_type = str(self.cfg.option("content_type", "form")).lower()
        fields = self._substitute(self.cfg.option("fields", {}) or {})
        headers = dict(self.cfg.option("extra_headers", {}) or {})

        if content_type == "json":
            headers.setdefault("Content-Type", "application/json")
            payload: Any = json.dumps(fields, ensure_ascii=False)
        else:
            payload = fields

        self.log.info("%s %s 字段=%s", method, url, sorted(fields))
        resp = self.http.request(url, method=method, data=payload, headers=headers, follow=True)
        text = resp.text
        self.log.debug("响应 %s: %s", resp.status, text[:300])

        success = [s for s in (self.cfg.option("success_contains") or ["success", "ok"])]
        failure = [s for s in (self.cfg.option("failure_contains") or ["fail", "error"])]

        hit_fail = next((w for w in failure if w.lower() in text.lower()), None)
        hit_ok = next((w for w in success if w.lower() in text.lower()), None)

        if hit_fail and not hit_ok:
            return LoginResult(False, f"响应包含失败标志 {hit_fail!r}: {text[:160]}",
                               {"http_status": resp.status})
        if hit_ok:
            return LoginResult(True, "认证成功", {"http_status": resp.status})
        if resp.status == 200 and not hit_fail:
            return LoginResult(True, "HTTP 200 且无失败标志，按成功处理（请确认 watch 能判定在线）",
                               {"http_status": resp.status})
        return LoginResult(False, f"HTTP {resp.status}，未匹配成功标志: {text[:160]}",
                           {"http_status": resp.status})


PROVIDER = GenericFormProvider
