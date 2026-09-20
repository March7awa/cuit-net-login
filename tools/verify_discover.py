"""离线自检：在「没配 portal / 没配 nasip」的情况下能不能自动探测出来。

这个是新用户最关心的问题 —— 他们不知道认证服务器和接入设备的地址，
程序必须自己从 AC（接入设备）的重定向里学出来。

做法：在本机起一个假的 AC + 门户，模仿真实链路
    /probe           -> 302 到门户 /entry?mac=..&nasip=1.1.1.1
    /entry?mac=..    -> 302 到 /portal?sessionId=..&nasIp=<真实值>
    /portal?sessionId-> 200 空页
然后让 RuijieSamCasProvider 在 portal="" / nasip="" 的配置下去探测，
断言它拿到的正是真值。全程不碰外网、不碰真实配置。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 中文 Windows 的控制台默认是 GBK，打印 ✅ 会直接抛 UnicodeEncodeError。
# 别人 clone 下来照着 README 跑一下就撞上，所以先把输出切成 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

from campusnet import config as config_mod           # noqa: E402
from campusnet import netutil                          # noqa: E402
from campusnet.httpx import HttpClient                 # noqa: E402
from campusnet.providers.ruijie_sam_cas import RuijieSamCasProvider  # noqa: E402

REAL_NASIP = "10.9.9.9"
REAL_SESSION = "S-abc123"

failures: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + extra) if extra else ''}")
    if not ok:
        failures.append(label)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/probe":
            # 假装被 AC 劫持
            self.send_response(302)
            self.send_header("Location", "/entry?mac=001122334455&nasip=1.1.1.1")
            self.end_headers()
            return
        if path == "/entry":
            # 假装门户建好了会话，并且知道我们挂在哪台 AC 上
            self.send_response(302)
            self.send_header(
                "Location",
                f"/portal?sessionId={REAL_SESSION}&nasIp={REAL_NASIP}"
                "&userIp=10.0.0.2&flowKey=FK1&userMac=001122334455",
            )
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html>ok</html>")


class NullLog:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    # 只让「探测」走我们这台假服务器
    netutil.CHECK_URLS = ((f"{base}/probe", None),)

    data = json.loads(json.dumps(config_mod.DEFAULTS))
    data["provider"] = "ruijie_sam_cas"
    data["username"] = "probe"
    data["options"] = {"portal": "", "nasip": "", "mac": "auto"}
    cfg = config_mod.Config(data, config_mod.resolve_path_for_probe())

    print("1) 探测用的临时配置不能是真实配置")
    real = config_mod.resolve_config_path()
    check("resolve_path_for_probe() 指向临时目录", cfg.path != real, str(cfg.path))

    print("\n2) portal / nasip 都留空时的自动探测")
    provider = RuijieSamCasProvider(cfg, HttpClient(timeout=6), NullLog())
    result = provider.discover()
    print(f"   探测结果: {result}")
    check("探测成功", bool(result.get("ok")), str(result.get("reason", "")))
    check("服务器地址探测正确", result.get("portal") == base, str(result.get("portal")))
    check("接入设备地址探测正确（不是占位值 1.1.1.1）",
          result.get("nasip") == REAL_NASIP, str(result.get("nasip")))

    print("\n3) 探测过程不能污染真实配置")
    check("临时配置文件没有被创建", not cfg.path.exists(), str(cfg.path))

    print("\n4) 在线（没被劫持）时要给出人话提示，而不是报错")
    netutil.CHECK_URLS = (("http://127.0.0.1:1/none", None),)
    silent = provider.discover()
    print(f"   {silent.get('reason', '').strip()}")
    check("返回 ok=False 且有可读原因",
          silent.get("ok") is False and "探测不到" in str(silent.get("reason", "")))

    print("\n5) 校验不能因为 portal/nasip 留空就拦人")
    netutil.CHECK_URLS = ((f"{base}/probe", None),)
    check("provider.validate() 放行", provider.validate() is None,
          str(provider.validate()))
    check("登录时的门户地址也能自己探测出来",
          provider._portal() == base, provider._portal())

    server.shutdown()

    print()
    if failures:
        print(f"有 {len(failures)} 项没过 ❌")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
