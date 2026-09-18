"""锐捷 ePortal 经典接口（``InterFace.do?method=login``）。

适用于 4.x 及更早的锐捷 Web 认证，很多学校至今仍在使用。

链路::

    1. GET  {check_url}（不跟随重定向）
       Location: http://portal/eportal/InterFace.do?method=pageInfo&wlanuserip=...&...
       把 "?" 之后的整串保存为 queryString
    2. POST {portal}/eportal/InterFace.do?method=login
       userId, password, service, queryString, operatorPwd,
       operatorUserId, validcode, passwordEncrypt=false
       返回 JSON: {"result":"success"|"fail", "message": "..."}
"""

from __future__ import annotations

import urllib.parse

from .base import LoginResult, Provider

__all__ = ["RuijieEportalProvider", "PROVIDER"]


class RuijieEportalProvider(Provider):
    name = "ruijie_eportal"
    description = "锐捷 ePortal 经典网页认证 (InterFace.do)"
    required_options = ("portal",)
    example_options = {
        "portal": "http://10.0.0.55",
        "login_path": "/eportal/InterFace.do?method=login",
        "service": "",
        "operator_user_id": "",
        "operator_pwd": "",
    }
    notes = (
        "portal 填认证服务器地址。service 多数学校留空即可；\n"
        "  个别学校需要填运营商服务名（如 '校园网'）。"
    )

    def _portal(self) -> str:
        portal = str(self.cfg.option("portal", "")).strip().rstrip("/")
        if not portal.startswith(("http://", "https://")):
            portal = "http://" + portal
        return portal

    def _query_string(self) -> str:
        """从被劫持的请求里取回 queryString（ePortal 用它绑定本次会话）。"""
        from ..netutil import CHECK_URLS

        for url, expected in CHECK_URLS:
            try:
                resp = self.http.get(url, follow=False, timeout=6)
            except Exception:
                continue
            loc = resp.location or ""
            if "InterFace.do" in loc and "?" in loc:
                qs = loc.split("?", 1)[1]
                if "method=" in qs:
                    qs = qs.split("method=", 1)[1]
                    qs = qs.split("&", 1)[1] if "&" in qs else ""
                self.log.info("捕获到 queryString: %s", qs[:200])
                return qs
            if resp.status not in (200, 204):
                # 门户可能直接回一个带参数的地址
                if "?" in loc:
                    return loc.split("?", 1)[1]
        explicit = self.cfg.option("query_string")
        if explicit:
            return str(explicit)
        return ""

    def login(self) -> LoginResult:
        portal = self._portal()
        login_path = self.cfg.option("login_path", "/eportal/InterFace.do?method=login")
        query_string = self._query_string()

        form = {
            "userId": self.cfg.username,
            "password": self.cfg.password(),
            "service": self.cfg.option("service", "") or "",
            "queryString": query_string,
            "operatorPwd": self.cfg.option("operator_pwd", "") or "",
            "operatorUserId": self.cfg.option("operator_user_id", "") or "",
            "validcode": "",
            "passwordEncrypt": "false",
        }
        self.log.info("提交 %s%s", portal, login_path)
        resp = self.http.post(
            portal + login_path, data=form, follow=True,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": portal + "/",
            },
        )
        text = resp.text
        self.log.debug("响应 %s: %s", resp.status, text[:300])

        try:
            data = resp.json()
        except Exception:
            return LoginResult(False, f"响应不是 JSON（HTTP {resp.status}）: {text[:160]}",
                               {"http_status": resp.status})

        result = str(data.get("result", "")).lower()
        message = str(data.get("message", ""))
        if result == "success":
            return LoginResult(True, "认证成功", data)
        return LoginResult(False, f"认证失败: {message or data}", data)


PROVIDER = RuijieEportalProvider
