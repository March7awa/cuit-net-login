"""锐捷 SAM / ePortal 5.x + CAS 单点登录（含成都信息工程大学）。

认证链路（已用真实门户逐字节验证）::

    1. GET  {portal}/entry?mac={MAC}
            302 -> /eportal/identityAuth.jsp?source=gateway&from=client&mac=...
            302 -> /portal/portal-main?sessionId=...&userIp=...&nasIp=...
                   &customPageId=...&flowKey=...&userMac=...
    2. POST {portal}/eportal/workFlow/getCurrentNode   {"sessionId","flowKey"}
    3. GET  {portal}/cas-sso/login?flowSessionId=...&customPageId=...&...
            页面里带有 <p id="login-croypto">      -> AES 密钥(base64)
                      <p id="login-page-flowkey"> -> CAS execution 令牌
    4. POST {portal}/cas-sso/login?<同一串参数>
            form: username, password, croypto, captcha_payload,
                  type=UsernamePassword, _eventId=submit, geolocation, execution
            其中 password      = Base64(AES-128-ECB/PKCS7(密码, key=Base64解码(croypto)))
                 captcha_payload = Base64(AES-128-ECB/PKCS7("{}", 同一 key))

只要 `portal` / `entry_path` / `cas_prefix` 对得上，其它学校同一套锐捷 SAM
也可以直接复用。
"""

from __future__ import annotations

import re
import time
import urllib.parse

from ..aes import encrypt_cryptojs_style
from .base import LoginResult, Provider

__all__ = ["RuijieSamCasProvider", "PROVIDER"]


def _first_int(*values):
    for v in values:
        if v:
            return v
    return None


# --------------------------------------------------------------------------
# 「选运营商」这一步
#
# CAS 只证明「你是谁」。很多学校（尤其接了多家运营商的）在这之后还要再选一次
# 服务商（校园网 / 中国移动 / 中国电信 / 中国联通），选完才真正下发授权把网
# 放行。少了这一步的典型表现就是：
#     认证明明返回 ticket=ST-... 成功，但网络完全不通。
# --------------------------------------------------------------------------

#: 不同版本锐捷给字段起的名不一样，这里都兜住。
#: 实测成都信息工程大学返回的是 {"key": "联通", "value": "unicom"}。
_SERVICE_VALUE_KEYS = ("service", "value", "serviceId", "service_id", "id", "uuid")
_SERVICE_NAME_KEYS = ("key", "name", "serviceName", "service_name", "label", "text", "description")
_SERVICE_ORDER_KEYS = ("order", "sort", "index", "seq")


def _service_value(item) -> str | None:
    if not isinstance(item, dict):
        return str(item) if item else None
    for key in _SERVICE_VALUE_KEYS:
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _service_name(item) -> str:
    if not isinstance(item, dict):
        return str(item)
    for key in _SERVICE_NAME_KEYS:
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return _service_value(item) or "?"


def describe_service(item) -> str:
    return f"{_service_name(item)}  (service={_service_value(item)})"


#: 用户在界面上选的是「中国移动」这种通俗叫法，但门户里可能写成
#: ``CMCC`` / ``校园网(移动)`` / ``mobile`` 之类，所以两边都要认。
SERVICE_ALIASES: dict[str, tuple[str, ...]] = {
    "移动": ("中国移动", "移动", "cmcc", "cmnet", "mobile", "chinamobile", "yd"),
    "电信": ("中国电信", "电信", "chinanet", "telecom", "chinatelecom", "ctcc", "dx"),
    "联通": ("中国联通", "联通", "unicom", "chinaunicom", "cucc", "cnc", "lt"),
    "校园网": ("校园网", "校园", "校内", "campus", "xyw"),
}

#: 给 GUI / CLI 用的下拉候选
def service_choices() -> list[str]:
    return ["中国移动", "中国电信", "中国联通", "校园网"]


def _service_aliases(wanted: str) -> tuple[str, ...]:
    """把用户填的值扩展成一堆可能写法。"""
    low = wanted.strip().lower()
    out = [low]
    for key, names in SERVICE_ALIASES.items():
        if key in wanted or wanted in key or any(n.lower() == low for n in names):
            out.extend(n.lower() for n in names)
    return tuple(dict.fromkeys(a for a in out if a))


def _service_matches(item, wanted: str) -> bool:
    """门户列表条目 vs 用户选择 —— 名字、取值、别名都试一遍。"""
    if not wanted.strip():
        return False
    value = (_service_value(item) or "").lower()
    name = _service_name(item).lower()
    for alias in _service_aliases(wanted):
        if alias == value or alias == name:
            return True
        if alias in name or alias in value:
            return True
    return False


def _service_sort_key(item):
    if isinstance(item, dict):
        for key in _SERVICE_ORDER_KEYS:
            if item.get(key) is not None:
                try:
                    return float(item[key])
                except (TypeError, ValueError):
                    pass
    return 1e9


class RuijieSamCasProvider(Provider):
    name = "ruijie_sam_cas"
    description = "锐捷 SAM/ePortal 5.x + CAS 单点登录（成都信息工程大学等）"
    # portal 不是必填 —— 没配的话程序会从门户重定向里自己探测
    required_options = ()
    example_options = {
        "portal": "http://10.254.241.66",
        "mac": "auto",
        "entry_path": "/entry",
        "workflow_path": "/eportal/workFlow/getCurrentNode",
        "cas_prefix": "/cas-sso",
        "app_type": "normal",
        "language": "zh-CN",
        "identity_auth_path": "/eportal/identityAuth.jsp",
        "nasip": "",
        "service": "",
        "service_list_path": "/eportal/network/serviceSelection",
        "service_login_path": "/eportal/network/serviceLogin",
    }
    notes = (
        "portal 可以不填 —— 断网时程序会从门户重定向里自己探测。\n"
        "  想手填就填认证页那个 IP（不要带 /portal 后缀）。\n"
        "  mac 一般写 auto，程序会自动取默认路由网卡的 MAC；\n"
        "  如果门户绑定了别的网卡，就手工填 12 位十六进制，例如 001122334455。\n"
        "  nasip 是接入设备（AC/NAS）的地址，很重要：不带它门户会填占位值\n"
        "  1.1.1.1，运营商列表拿不到、认证也不会真正放行。程序会尽量从 AC 的\n"
        "  重定向里自动学到并记住；也可以手工填（浏览器 F12 控制台搜 nasIp）。\n"
        "  service 是运营商（校园网/中国移动/中国电信/中国联通）。\n"
        "  很多学校 CAS 认证完还要再选一次运营商才会真正放行网络 ——\n"
        "  留空的话程序会自动选列表里的第一项，并把完整列表写进日志；\n"
        "  填 'none' 表示跳过这一步。"
    )

    # -- helpers -----------------------------------------------------------
    def _resolve_mac(self) -> str | None:
        from ..netutil import normalize_mac, primary_mac

        configured = str(self.cfg.option("mac", "auto") or "auto").strip()
        if configured.lower() not in ("auto", "", "none"):
            mac = normalize_mac(configured)
            if not mac:
                raise RuntimeError(f"options.mac 不是合法的 MAC 地址: {configured!r}")
            return mac
        return primary_mac()

    @staticmethod
    def _normalize_portal(value: str) -> str:
        value = value.strip().rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            value = "http://" + value
        return value

    def _portal_from_captive(self) -> str:
        """从 AC 的重定向里推出门户地址。"""
        captive = self._captive_entry()
        if not captive:
            return ""
        parts = urllib.parse.urlsplit(captive[0])
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
        return ""

    def _portal(self) -> str:
        """门户地址。没配就自己探测 —— 用户不用知道这个地址。"""
        configured = self._normalize_portal(str(self.cfg.option("portal", "") or ""))
        if configured:
            return configured
        found = self._portal_from_captive()
        if found:
            self.log.info("配置里没写 portal，已从门户重定向里探测到: %s", found)
            return found
        raise RuntimeError(
            "没有配置认证服务器，而且当前网络没有被门户劫持，探测不到。\n"
            "      解法：断开校园网（或拔网线）后重试，程序会自动探测；\n"
            "      或者手工填上认证页的地址（浏览器地址栏里那个 IP）。")

    def discover(self) -> dict:
        """探测门户地址和接入设备地址。

        需要在**未认证**状态（被门户劫持）下才有结果，这也正是用户需要
        配置的时候。返回 ``{"ok": bool, "portal": str, "nasip": str, ...}``。
        """
        captive = self._captive_entry()
        if not captive:
            return {"ok": False, "reason":
                    "现在网络是通的（没被门户劫持），探测不到。\n"
                    "断开校园网 / 拔掉网线后再点一次就行。"}
        entry, probe = captive
        parts = urllib.parse.urlsplit(entry)
        portal = f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""
        result: dict = {"ok": True, "portal": portal, "nasip": "", "entry": entry,
                        "from": probe}

        # 跟着 AC 给的链走一遍，会话参数里带着真实的 nasIp
        try:
            params, _ = self._walk_to_session(portal, self._resolve_mac())
            result["nasip"] = params.get("nasIp", "") or ""
            if result["nasip"] in ("1.1.1.1",):
                result["nasip"] = ""
        except Exception as exc:  # noqa: BLE001
            result["note"] = f"跟随重定向取 nasip 失败: {exc}"
        return result

    def _remember_nasip(self, nas_ip: str) -> bool:
        """把学到的真实 nasIp 写回配置文件（失败不影响登录）。"""
        try:
            self.cfg.raw.setdefault("options", {})["nasip"] = nas_ip
            self.cfg.save()
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.debug("写入 nasip 失败: %s", exc)
            return False

    def _captive_entry(self) -> tuple[str, str] | None:
        """碰一个外网地址，从 AC 的 302 里拿到真正的入口 URL。

        这一步**非常关键**，是整条链路能不能真正生效的分水岭：

        * 走 AC 重定向进来 —— 门户知道我们接在哪个接入设备上，会话里的
          ``nasIp`` 是真实值（实测成都信息工程大学是 10.254.0.1），
          于是 ``serviceSelection`` 能返回运营商列表，认证也会真正下发。
        * 直接访问 ``/entry?mac=`` —— 门户不知道我们在哪台 AC 下，会把
          ``nasIp`` 填成占位值 1.1.1.1。这时 ``serviceSelection`` 返回空
          列表，CAS 虽然照样发票据，但网络不会放行。

        之前只走后者，所以「认证成功却上不了网」。
        """
        import re

        from ..netutil import CHECK_URLS, _looks_like_portal

        for probe_url, _expected in CHECK_URLS:
            try:
                resp = self.http.get(probe_url, follow=False, timeout=6)
            except Exception:  # noqa: BLE001
                continue

            # 1) 标准做法：AC 直接 302
            if resp.location:
                # 有些 AC 给的是相对路径（Location: /portal?...），先补成绝对地址，
                # 否则下面的私网判断会因为解析不出主机名而漏掉。
                target = urllib.parse.urljoin(probe_url, resp.location)
                if _looks_like_portal(target):
                    return target, probe_url

            # 2) 有些 AC 不 302，而是回一个 200 + meta refresh / JS 跳转，
            #    把门户地址写在正文里。这种也要认出来。
            body = resp.text[:8000]
            for pattern in (
                r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+url=([^"'>\s]+)""",
                r"""location\.(?:replace|href)\s*=\s*["']([^"']+)["']""",
                r"""["'](https?://[\d.]+(?::\d+)?/[^"'\s]*nasip=[^"'\s]*)["']""",
            ):
                m = re.search(pattern, body, re.I)
                if m:
                    cand = m.group(1).strip()
                    if _looks_like_portal(cand):
                        self.log.debug("从探测页面正文里发现门户地址: %s", cand[:160])
                        return urllib.parse.urljoin(probe_url, cand), probe_url
        return None

    def _walk_to_session(self, portal: str, mac: str | None):
        """步骤 1：跟着重定向走到带 sessionId 的那个 URL。

        优先用 AC 的重定向进入（nasIp 才正确），拿不到再退回 /entry?mac=。
        """
        entry_path = self.cfg.option("entry_path", "/entry")
        nasip = str(self.cfg.option("nasip", "") or "").strip()
        candidates: list[tuple[str, str]] = []

        captive = self._captive_entry()
        if captive:
            entry, probe = captive
            candidates.append((entry, f"AC 重定向（来自 {probe}）"))
            self.log.info("步骤1 检测到门户劫持，将从 AC 给的入口进入: %s", entry[:160])

        fallback = f"{portal}{entry_path}" + (f"?mac={mac}" if mac else "")
        if nasip:
            # 关键：带上 nasip **小写**，门户才会用真实的接入设备地址建会话。
            # 不带的话门户填占位值 1.1.1.1，serviceSelection 返回空、认证不真正下发。
            fallback += f"&nasip={urllib.parse.quote(nasip)}"
            why = f"直接访问 {entry_path}（带上已配置的 nasip={nasip}）"
        else:
            why = f"直接访问 {entry_path}（未配置 nasip，门户会给占位值）"
        candidates.append((fallback, why))

        last_error = "门户没有返回 sessionId"
        for url, why in candidates:
            self.log.info("步骤1 入口[%s]: %s", why, url[:160])
            params: dict[str, str] = {}
            for hop in range(12):
                resp = self.http.get(
                    url, follow=False,
                    headers={"Referer": f"{portal}/", "isPortal": "true"},
                )
                self.log.debug("  跳转[%d] %s -> %s", hop, resp.status, url)
                if resp.location:
                    nxt = urllib.parse.urljoin(url, resp.location)
                    self.log.debug("          Location: %s", nxt)
                    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(nxt).query))
                    if q.get("sessionId"):
                        params = q
                        # 抓一次页面本身，确保服务端把我们当成这次会话的终端
                        self.http.get(nxt, follow=False, headers={"Referer": url})
                        self.log.info("步骤1 完成: sessionId=%s userIp=%s nasIp=%s",
                                      q.get("sessionId"), q.get("userIp"), q.get("nasIp"))
                        return params, nxt
                    url = nxt
                    if resp.status not in (301, 302, 303, 307, 308):
                        break
                    continue
                # 没有 Location 了，看看当前 URL 里有没有参数
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
                if q.get("sessionId"):
                    return q, url
                break
            last_error = f"{why} 这条路径没拿到 sessionId"

        raise RuntimeError(f"门户没有返回 sessionId（{last_error}）")

    # -- main --------------------------------------------------------------
    def login(self) -> LoginResult:
        portal = self._portal()
        mac = self._resolve_mac()
        self.log.info("portal=%s  mac=%s  user=%s", portal, mac or "(未取到)", self.cfg.username)

        params, session_url = self._walk_to_session(portal, mac)
        session_id = params.get("sessionId", "")
        nas_ip = params.get("nasIp", "")
        if nas_ip in ("", "1.1.1.1"):
            # 占位值 = 门户不知道我们接在哪台 AC 上。这时 CAS 仍会成功，
            # 但 serviceSelection 返回空、网络也不会真正放行。
            self.log.warning(
                "本次会话的 nasIp 是 %r（占位值）—— 没能拿到真实接入设备地址。"
                "若一直「认证已通过但网络没放行」，请在 options.nasip 里手工填上"
                "真实值（浏览器 F12 控制台里搜 nasIp 能看到，例如 10.254.0.1）。",
                nas_ip or "(空)")
        else:
            # 从 AC 重定向拿到了真值 —— 记下来，下次就算没被劫持也能直接用
            configured = str(self.cfg.option("nasip", "") or "").strip()
            if configured != nas_ip:
                if self._remember_nasip(nas_ip):
                    self.log.info("已记住真实 nasIp=%s（下次直接使用）", nas_ip)
                elif configured:
                    self.log.warning("真实 nasIp=%s 与配置的 %s 不一致，但没能写入配置",
                                     nas_ip, configured)

        # 步骤 2：推进工作流（部分环境省略也能登录，失败不致命）
        wf_path = self.cfg.option("workflow_path", "/eportal/workFlow/getCurrentNode")
        flow_key = params.get("flowKey", "")
        if flow_key:
            try:
                r = self.http.post_json(
                    f"{portal}{wf_path}",
                    {"sessionId": session_id, "flowKey": flow_key},
                    headers={"isPortal": "true", "Referer": session_url},
                )
                self.log.debug("步骤2 workFlow/getCurrentNode -> %s %s", r.status, r.text[:200])
            except Exception as exc:  # noqa: BLE001
                self.log.warning("步骤2 推进工作流失败（继续尝试登录）: %s", exc)

        # 步骤 3：取 CAS 登录页，解析 croypto 与 execution
        cas_prefix = str(self.cfg.option("cas_prefix", "/cas-sso")).rstrip("/")
        cas_query = urllib.parse.urlencode({
            "flowSessionId": session_id,
            "customPageId": params.get("customPageId", ""),
            "preview": "false",
            "appType": self.cfg.option("app_type", "normal"),
            "language": self.cfg.option("language", "zh-CN"),
            "timer": str(int(time.time() * 1000)),
            "nasIp": params.get("nasIp", ""),
            "userIp": params.get("userIp", ""),
            "userMac": params.get("userMac", "") or (mac or ""),
        }, quote_via=urllib.parse.quote)

        cas_url = f"{portal}{cas_prefix}/login?{cas_query}"
        page = self.http.get(
            cas_url, follow=True,
            headers={"Referer": session_url, "isPortal": "true"},
        )
        html = page.text
        self.log.debug("步骤3 CAS 登录页 %s (%d 字节)", page.status, len(html))

        croypto = self._pick(html, "login-croypto")
        execution = self._pick(html, "login-page-flowkey")
        if not croypto:
            raise RuntimeError("CAS 登录页里找不到 login-croypto（页面结构可能已变更）")
        if not execution:
            raise RuntimeError("CAS 登录页里找不到 login-page-flowkey（页面结构可能已变更）")
        self.log.info("步骤3 完成: croypto=%s… execution=%s…", croypto[:8], execution[:8])

        # 步骤 4：提交凭据
        password_ct = encrypt_cryptojs_style(croypto, self.cfg.password())
        captcha_ct = encrypt_cryptojs_style(croypto, "{}")
        form = {
            "username": self.cfg.username,
            "password": password_ct,
            "croypto": croypto,
            "captcha_payload": captcha_ct,
            "type": "UsernamePassword",
            "_eventId": "submit",
            "geolocation": "",
            "execution": execution,
        }
        post_url = f"{cas_url}&accept-language={urllib.parse.quote(self.cfg.option('language', 'zh-CN'))}"
        referer = f"{portal}/pc/center?{cas_query}"
        resp = self.http.post(
            post_url, data=form, follow=False,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": referer,
                "Origin": portal,
                "isPortal": "true",
            },
        )
        self.log.info("步骤4 提交结果: HTTP %s", resp.status)

        ok, message = self._judge(resp)
        detail = {
            "sessionId": session_id,
            "http_status": resp.status,
            "location": resp.location,
        }

        # 步骤 5：选运营商（很多学校不选这一步网络不会真正通）
        if ok:
            service = self._select_service(portal, session_id, referer)
            if service:
                detail["service"] = service
                message += f"｜已选择运营商: {service}"
        return LoginResult(ok=ok, message=message, detail=detail)

    # -- 注销 --------------------------------------------------------------
    def logout(self) -> LoginResult:
        """注销当前会话（**会断网**）。

        用途：换账号、或者想像浏览器那样自己看一遍门户页面（选运营商那步）。
        注销后认证页就会重新弹出来。
        """
        portal = self._portal()
        mac = self._resolve_mac()
        try:
            params, session_url = self._walk_to_session(portal, mac)
        except Exception as exc:  # noqa: BLE001
            return LoginResult(False, f"建会话失败，没法注销: {exc}")
        session_id = params.get("sessionId", "")
        if not session_id:
            return LoginResult(False, "拿不到 sessionId，没法注销")

        resp = self.http.post_json(
            f"{portal}/eportal/network/offline", {"sessionId": session_id},
            headers={"isPortal": "true", "Referer": session_url},
        )
        self.log.info("注销 %s -> HTTP %s %s", session_id, resp.status,
                      resp.text[:160].replace("\n", " "))
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        if body.get("code") == 200:
            return LoginResult(True, "已注销，网络应该已断开（门户页面会重新弹出来）", body)
        return LoginResult(False, f"注销失败: {body.get('message') or resp.text[:160]}", body)

    # -- 选运营商 ----------------------------------------------------------
    def _service_paths(self) -> tuple[str, str]:
        return (
            str(self.cfg.option("service_list_path", "/eportal/network/serviceSelection")),
            str(self.cfg.option("service_login_path", "/eportal/network/serviceLogin")),
        )

    def fetch_services(self, portal: str, session_id: str, referer: str = "") -> list:
        """取可选的运营商列表。不在认证流程里时会返回空列表。"""
        list_path, _ = self._service_paths()
        resp = self.http.post_json(
            f"{portal}{list_path}", {"sessionId": session_id},
            headers={"isPortal": "true", "Referer": referer or f"{portal}/"},
        )
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            self.log.debug("serviceSelection 返回的不是 JSON: %s", resp.text[:200])
            return []
        items = data.get("data")
        return items if isinstance(items, list) else []

    def _select_service(self, portal: str, session_id: str, referer: str) -> str | None:
        """CAS 认证通过后再选一次运营商，否则网络可能完全不通。"""
        if str(self.cfg.option("service", "") or "").strip().lower() in ("none", "skip"):
            self.log.info("按配置跳过选运营商")
            return None
        try:
            services = self.fetch_services(portal, session_id, referer)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("取运营商列表失败（当作不需要选）: %s", exc)
            return None

        if not services:
            self.log.debug("serviceSelection 返回空 —— 这次不需要选运营商")
            return None

        self.log.info("认证已通过，但还需要选择运营商，共 %d 个：", len(services))
        for item in services:
            self.log.info("    · %s", describe_service(item))

        wanted = str(self.cfg.option("service", "") or "").strip()
        pick = None
        if wanted:
            pick = next((s for s in services if _service_matches(s, wanted)), None)
            if pick is None:
                self.log.warning("配置的 options.service=%r 不在上面列表里，改选第一项", wanted)
        if pick is None:
            pick = sorted(services, key=_service_sort_key)[0]
            self.log.warning(
                "未配置 options.service，自动选第一项：%s\n"
                "        如果这不对，把 options.service 设成上面列表里的名字或 service 值。",
                describe_service(pick))

        value = _service_value(pick)
        if not value:
            self.log.error("认不出该运营商条目的取值字段，原始内容：%r", pick)
            return None

        _, login_path = self._service_paths()
        resp = self.http.post_json(
            f"{portal}{login_path}", {"sessionId": session_id, "service": value},
            headers={"isPortal": "true", "Referer": referer or f"{portal}/"},
        )
        self.log.info("步骤5 选择运营商 %s -> HTTP %s %s",
                      describe_service(pick), resp.status, resp.text[:200].replace("\n", " "))
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        if body.get("code") not in (None, 200):
            self.log.error("选运营商失败: %s", body.get("message") or resp.text[:200])
            return None
        return _service_name(pick)

    # -- parsing -----------------------------------------------------------
    @staticmethod
    def _pick(html: str, element_id: str) -> str | None:
        m = re.search(rf'id="{re.escape(element_id)}"[^>]*>([^<]*)<', html)
        if m and m.group(1).strip():
            return m.group(1).strip()
        return None

    @staticmethod
    def _judge(resp) -> tuple[bool, str]:
        loc = (resp.location or "")
        if resp.status in (301, 302, 303, 307, 308) and "login" not in loc.lower():
            return True, f"认证成功（跳转到 {loc[:120]}）"
        text = resp.text
        for pat in (
            r'id="login-error-msg"[^>]*>([^<]+)<',
            r'id="loginFailMessage"[^>]*>([^<]+)<',
            r'id="login-error-code"[^>]*>([^<]+)<',
        ):
            m = re.search(pat, text)
            if m and m.group(1).strip():
                return False, f"认证被拒绝: {m.group(1).strip()}"
        if resp.status == 401:
            return False, "认证被拒绝（401，通常是账号或密码不对）"
        if resp.status == 200 and "login-croypto" in text:
            return False, "认证被拒绝（服务端返回了登录页）"
        return False, f"未识别的响应: HTTP {resp.status}"


PROVIDER = RuijieSamCasProvider
