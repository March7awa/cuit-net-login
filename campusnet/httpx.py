"""Tiny HTTP layer built on ``urllib`` (no third-party dependencies).

Provides a cookie-aware session that can either follow redirects or stop at
the first one, which is what captive-portal discovery needs.
"""

from __future__ import annotations

import gzip
import http.cookiejar
import json as _json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_CTRL = re.compile(r"[\x00-\x20\x7f]")


def sanitize_url(url: str) -> str:
    """Percent-encode control characters and spaces.

    Real captive portals hand out ``Location`` headers containing raw spaces
    (e.g. ``accessTime=2026-09-18 12:37:40.801``), which :mod:`http.client`
    rejects outright.  Everything else, including reserved characters, is left
    untouched so the URL keeps its meaning.
    """
    return _CTRL.sub(lambda m: "".join(f"%{b:02X}" for b in m.group().encode()), url)


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class Response:
    __slots__ = ("status", "headers", "body", "url")

    def __init__(self, status: int, headers: dict[str, str], body: bytes, url: str) -> None:
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body
        self.url = url

    @property
    def text(self) -> str:
        raw = self.body
        if self.headers.get("content-encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        for enc in ("utf-8", "gb18030", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")

    def json(self) -> Any:
        return _json.loads(self.text)

    @property
    def location(self) -> str | None:
        return self.headers.get("location")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Response {self.status} {self.url[:120]!r}>"


class HttpError(Exception):
    """Raised for transport errors; HTTP status codes are *not* errors here."""

    def __init__(self, message: str, *, response: Response | None = None) -> None:
        super().__init__(message)
        self.response = response


class HttpClient:
    """A small stateful HTTP client with a cookie jar."""

    def __init__(self, *, timeout: float = 15.0, verify_tls: bool = True,
                 user_agent: str = DEFAULT_UA) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.jar = http.cookiejar.CookieJar()

        ctx = None
        if not verify_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPCookieProcessor(self.jar),
        ]
        if ctx is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ctx))

        self._follow = urllib.request.build_opener(*handlers)
        self._stop = urllib.request.build_opener(_NoRedirect(), *handlers)

    # -- request -----------------------------------------------------------
    def request(
        self,
        url: str,
        *,
        method: str | None = None,
        data: bytes | str | dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        follow: bool = True,
        timeout: float | None = None,
    ) -> Response:
        hdrs = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
        }
        hdrs.update(headers or {})

        body: bytes | None
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, str):
            body = data.encode()
        else:
            body = data

        req = urllib.request.Request(sanitize_url(url), data=body, headers=hdrs, method=method)
        opener = self._follow if follow else self._stop
        try:
            with opener.open(req, timeout=timeout or self.timeout) as resp:
                return Response(resp.status, dict(resp.headers), resp.read(), resp.geturl())
        except urllib.error.HTTPError as exc:
            # 4xx/5xx are legitimate responses for a login endpoint.
            try:
                payload = exc.read()
            except Exception:
                payload = b""
            return Response(exc.code, dict(exc.headers or {}), payload, url)
        except Exception as exc:  # noqa: BLE001 - surfaced to caller
            raise HttpError(f"{type(exc).__name__}: {exc}") from exc

    def get(self, url: str, **kw: Any) -> Response:
        kw.setdefault("method", "GET")
        return self.request(url, **kw)

    def post(self, url: str, **kw: Any) -> Response:
        kw.setdefault("method", "POST")
        return self.request(url, **kw)

    def post_json(self, url: str, obj: Any, **kw: Any) -> Response:
        headers = kw.pop("headers", {}) or {}
        headers.setdefault("Content-Type", "application/json")
        return self.post(url, data=_json.dumps(obj), headers=headers, **kw)

    # -- redirect walking --------------------------------------------------
    def walk_redirects(
        self,
        url: str,
        *,
        max_hops: int = 12,
        stop_when: Any = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[list[tuple[int, str, str | None]], Response]:
        """Follow redirects manually, returning every hop.

        ``stop_when(final_url)`` may return True to stop early (e.g. once the
        portal's session id appears in the URL).
        """
        hops: list[tuple[int, str, str | None]] = []
        current = url
        last: Response | None = None
        for _ in range(max_hops):
            resp = self.get(current, follow=False, headers=headers)
            last = resp
            loc = resp.location
            hops.append((resp.status, current, loc))
            if not loc or resp.status not in (301, 302, 303, 307, 308):
                break
            nxt = urllib.parse.urljoin(current, loc)
            if stop_when is not None and stop_when(nxt):
                # one more fetch so the caller sees the page body too
                last = self.get(nxt, follow=False, headers=headers)
                hops.append((last.status, nxt, last.location))
                current = nxt
                break
            current = nxt
        assert last is not None
        return hops, last
