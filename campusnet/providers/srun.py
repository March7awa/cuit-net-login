"""深澜 Srun 门户认证（``srun_portal`` CGI）。

这是国内高校最常见的认证系统之一（深澜软件）。链路::

    1. GET  {portal}/cgi-bin/get_challenge?callback={cb}&username={user}&ip={ip}&_={ts}
            -> {cb}({"challenge": "...", "client_ip": "...", ...})
    2. GET  {portal}/cgi-bin/srun_portal?callback={cb}&action=login&username=...
            &password={hmac_md5(password, challenge)}
            &ac_id={ac_id}&ip={ip}&chksum={hmac_md5(chkstr, challenge)}
            &info={"{SRBX1}" + srun_base64(xEncode(json, challenge))}
            &n=200&type=1&os=Windows+10&name=Windows&double_stack=0&_={ts}

``xEncode`` 与自定义 Base64 字母表都在本文件里用纯 Python 复刻，并且已经和
门户自身的 JavaScript 实现逐字节对拍通过（见 tools/srun_reference.js）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import urllib.parse

from .base import LoginResult, Provider

__all__ = ["SrunProvider", "PROVIDER", "xencode", "srun_b64"]

# 深澜自定义 Base64 字母表
_SRUN_ALPHABET = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"

# 32 位无符号溢出的模拟掩码（源自原始 JS 里的位运算常量）
_MASK_B = 0xEFB8D130 | 0x10472ECF
_MASK_C = 0xBB390742 | 0x44C6F8BD
_C = 0x86014019 | 0x183639A0


def _code_units(text: str) -> list[int]:
    """JS string semantics: a sequence of UTF-16 code units."""
    raw = text.encode("utf-16-le", "surrogatepass")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def _to_long_array(text: str, append_len: bool = True) -> list[int]:
    """JS ``s()``: pack 4 UTF-16 code units (little endian) into one 32-bit int."""
    units = _code_units(text)
    out = []
    for i in range(0, len(units), 4):
        value = 0
        for j in range(4):
            if i + j < len(units):
                value |= units[i + j] << (8 * j)
        out.append(value)
    if append_len:
        out.append(len(units))
    return out


def _from_long_array(values: list[int]) -> bytes:
    """JS ``l()``: turn the 32-bit int array back into a byte string."""
    out = bytearray()
    for value in values:
        out += bytes((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF, (value >> 24) & 0xFF))
    return bytes(out)


def xencode(msg: str, key: str) -> bytes:
    """深澜的 ``xEncode``（XXTEA 变体）。按 JS 的 UTF-16 语义处理输入。"""
    if not msg:
        return b""

    v = _to_long_array(msg)
    k = _to_long_array(key, append_len=False)

    def kget(index: int) -> int:
        # JS 里越界访问得到 undefined，而 ``undefined ^ z`` 就等于 ``z``
        # （ToInt32(undefined) == 0）。
        return k[index] if 0 <= index < len(k) else 0

    n = len(v) - 1
    if n < 1:
        return _from_long_array(v)

    z = v[n]
    y = v[0]
    d = 0
    q = 6 + 52 // (n + 1)

    while q > 0:
        d = (d + _C) & 0xFFFFFFFF
        e = (d >> 2) & 3
        p = 0
        while p < n:
            y = v[p + 1]
            m = ((z >> 5) ^ (y << 2)) & 0xFFFFFFFF
            m = (m + (((y >> 3) ^ (z << 4)) ^ (d ^ y))) & 0xFFFFFFFF
            m = (m + (kget((p & 3) ^ e) ^ z)) & 0xFFFFFFFF
            v[p] = (v[p] + m) & _MASK_B
            z = v[p]
            p += 1
        y = v[0]
        m = ((z >> 5) ^ (y << 2)) & 0xFFFFFFFF
        m = (m + (((y >> 3) ^ (z << 4)) ^ (d ^ y))) & 0xFFFFFFFF
        m = (m + (kget((p & 3) ^ e) ^ z)) & 0xFFFFFFFF
        v[n] = (v[n] + m) & _MASK_C
        z = v[n]
        q -= 1

    return _from_long_array(v)


def srun_b64(data: bytes) -> str:
    """深澜自定义字母表的 Base64（标准分组，非标准字符表）。"""
    out = []
    for i in range(0, len(data), 3):
        chunk = data[i:i + 3]
        size = len(chunk)
        value = (chunk[0] << 16) | ((chunk[1] if size > 1 else 0) << 8) | (chunk[2] if size > 2 else 0)
        out.append(_SRUN_ALPHABET[(value >> 18) & 0x3F])
        out.append(_SRUN_ALPHABET[(value >> 12) & 0x3F])
        out.append(_SRUN_ALPHABET[(value >> 6) & 0x3F] if size > 1 else "=")
        out.append(_SRUN_ALPHABET[value & 0x3F] if size > 2 else "=")
    return "".join(out)


def hmac_md5_hex(value: str, key: str) -> str:
    return hmac.new(key.encode("utf-8"), value.encode("utf-8"), hashlib.md5).hexdigest()


def _jsonp(text: str):
    m = re.search(r"\((\{.*\})\)", text, re.S)
    raw = m.group(1) if m else text.strip()
    return json.loads(raw)


class SrunProvider(Provider):
    name = "srun"
    description = "深澜 Srun 门户认证 (srun_portal)"
    required_options = ("portal",)
    example_options = {
        "portal": "http://10.0.0.1",
        "ac_id": "1",
        "n": "200",
        "type": "1",
        "os": "Windows 10",
        "name": "Windows",
        "double_stack": "0",
        "ip": "auto",
    }
    notes = (
        "portal 填深澜认证服务器地址（浏览器跳转到的那个 IP）。\n"
        "  ac_id 多数学校是 1；不确定时抓一次浏览器的登录请求就能看到。"
    )

    def _portal(self) -> str:
        portal = str(self.cfg.option("portal", "")).strip().rstrip("/")
        if not portal.startswith(("http://", "https://")):
            portal = "http://" + portal
        return portal

    def login(self) -> LoginResult:
        portal = self._portal()
        ac_id = str(self.cfg.option("ac_id", "1"))
        n = str(self.cfg.option("n", "200"))
        ptype = str(self.cfg.option("type", "1"))
        os_name = str(self.cfg.option("os", "Windows 10"))
        dev_name = str(self.cfg.option("name", "Windows"))
        double_stack = str(self.cfg.option("double_stack", "0"))
        callback = "jQuery" + str(int(time.time() * 1000))

        ip = str(self.cfg.option("ip", "auto") or "auto")
        if ip in ("auto", ""):
            from ..netutil import default_local_ip

            ip = default_local_ip() or ""
        if not ip:
            return LoginResult(False, "取不到本机 IP，请在 options.ip 里手工指定")

        ts = str(int(time.time() * 1000))
        challenge_url = (
            f"{portal}/cgi-bin/get_challenge"
            f"?callback={callback}&username={urllib.parse.quote(self.cfg.username)}"
            f"&ip={urllib.parse.quote(ip)}&_={ts}"
        )
        self.log.info("步骤1 get_challenge: %s", challenge_url)
        resp = self.http.get(challenge_url, follow=True)
        try:
            data = _jsonp(resp.text)
        except Exception as exc:  # noqa: BLE001
            return LoginResult(False, f"get_challenge 解析失败: {exc}; 原文={resp.text[:200]}")

        token = str(data.get("challenge", ""))
        client_ip = str(data.get("client_ip") or data.get("online_ip") or ip)
        if not token:
            return LoginResult(False, f"未拿到 challenge: {data}")

        password_hmac = hmac_md5_hex(self.cfg.password(), token)
        info_json = json.dumps({
            "username": self.cfg.username,
            "password": self.cfg.password(),
            "ip": client_ip,
            "acid": ac_id,
            "enc_ver": "srun_bx1",
        }, separators=(",", ":"), ensure_ascii=False)
        info = "{SRBX1}" + srun_b64(xencode(info_json, token))

        chkstr = token + self.cfg.username + password_hmac + ac_id + client_ip + n + ptype + info
        chksum = hmac_md5_hex(chkstr, token)

        login_url = (
            f"{portal}/cgi-bin/srun_portal"
            f"?callback={callback}&action=login"
            f"&username={urllib.parse.quote(self.cfg.username)}"
            f"&password={urllib.parse.quote(password_hmac)}"
            f"&ac_id={urllib.parse.quote(ac_id)}"
            f"&ip={urllib.parse.quote(client_ip)}"
            f"&chksum={urllib.parse.quote(chksum)}"
            f"&info={urllib.parse.quote(info, safe='')}"
            f"&n={urllib.parse.quote(n)}&type={urllib.parse.quote(ptype)}"
            f"&os={urllib.parse.quote(os_name)}&name={urllib.parse.quote(dev_name)}"
            f"&double_stack={urllib.parse.quote(double_stack)}&_={ts}"
        )
        self.log.info("步骤2 srun_portal (ip=%s ac_id=%s)", client_ip, ac_id)
        self.log.debug("URL: %s", login_url)
        resp = self.http.get(login_url, follow=True, headers={"Referer": portal + "/"})
        text = resp.text
        self.log.debug("响应: %s", text[:400])

        try:
            result = _jsonp(text)
        except Exception:
            return LoginResult(False, f"登录响应无法解析: {text[:200]}")

        error = str(result.get("error", ""))
        ecode = result.get("ecode")
        if result.get("res") == "ok" or (error in ("ok", "0", "") and ecode in (0, "0", None)):
            return LoginResult(True, "认证成功", result)
        msg = result.get("error_msg") or result.get("error") or result.get("res") or text[:160]
        return LoginResult(False, f"认证失败: {msg}", result)


PROVIDER = SrunProvider
