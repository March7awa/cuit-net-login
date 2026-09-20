"""离线自检：拔网线再插回去，看门狗多久才会重新认证？

用户反馈「只有在弹出校园网登录窗口的时候才会自动连接」—— 说白了就是
插回网线之后有一大段空白，人在这段时间里自己开了浏览器，于是以为是
那个窗口触发的。

原来的一轮是这样的：4 个探测点各自等满 5 秒超时 = 20 秒，然后才轮到
「要不要登录」。插回网线时如果正卡在这一轮里，就得白等十几秒。

这个脚本不碰真网卡也不碰真门户：把 ``link_up`` / ``is_online`` / ``login``
换成可控的桩，量「链路恢复 → 发起登录」之间的延迟。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 中文 Windows 的控制台默认是 GBK，打印 ✅ 会直接抛 UnicodeEncodeError。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

import campusnet.runner as runner_mod  # noqa: E402
from campusnet.providers import LoginResult  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + extra) if extra else ''}")
    if not ok:
        failures.append(label)


class FakeLog:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class FakeRunner(runner_mod.Runner):
    """把「在线判断」「链路状态」「登录」全换成可控的桩。"""

    def __init__(self, state):
        self.state = state
        self.cfg = SimpleNamespace(
            option=lambda *a, **k: None,
            section=lambda _name: {
                "interval": 20, "cooldown_after_login": 0,
                "online_wait_seconds": 5, "network_retry_seconds": 5,
                "relogin_delay_seconds": 0, "check_min_ok": 1,
            },
        )
        self.log = FakeLog()
        self.http = None

    def is_online(self):
        if not self.state["link"]:
            # 拔着网线：真实实现会在这里让探测点各等满超时（4 × 3s）
            time.sleep(0.3)
            return False
        return self.state["online"]

    def _network_ready(self):
        return True

    def describe_offline(self):
        return "假断网"

    def login(self):
        self.state["logins"] += 1
        if not self.state["link"]:
            time.sleep(0.3)
            return LoginResult(False, "网络错误: URLError: timed out")
        self.state["online"] = True
        self.state["login_at"] = time.time()
        return LoginResult(True, "假门户：认证成功")


def scenario(flip_after: float, cycles: int = 60):
    """跑一次看门狗：链路在 flip_after 秒后恢复，返回 (状态, 恢复时刻)。"""
    state = {"link": False, "online": False, "logins": 0, "login_at": None}
    runner_mod.link_up = lambda: state["link"]  # 网卡链路状态也一起做桩
    runner = FakeRunner(state)
    started = time.time()

    def flip():
        time.sleep(flip_after)
        state["link"] = True

    threading.Thread(target=flip, daemon=True).start()
    threading.Thread(target=lambda: runner.watch(max_cycles=cycles), daemon=True).start()

    deadline = time.time() + flip_after + 20
    while state["login_at"] is None and time.time() < deadline:
        time.sleep(0.02)
    return state, started + flip_after


def main() -> int:
    real_link_up = runner_mod.link_up
    try:
        print("1) 链路恢复后，看门狗多久才发起登录")
        state, link_back_at = scenario(flip_after=0.5)
        if state["login_at"] is None:
            check("链路恢复后能自己发起登录", False, "20 秒内没动静")
        else:
            delay = state["login_at"] - link_back_at
            check("链路恢复后能自己发起登录", True, f"延迟 {delay:.1f} 秒")
            check("延迟在 3 秒以内（人感觉不到空白）", delay <= 3.0, f"{delay:.1f}s")

        print("\n2) 拔着网线的时候，不该反复去敲门户（原来每轮白等 20 秒）")
        state = {"link": False, "online": False, "logins": 0, "login_at": None}
        runner_mod.link_up = lambda: state["link"]
        runner = FakeRunner(state)
        threading.Thread(target=lambda: runner.watch(max_cycles=30), daemon=True).start()
        time.sleep(2.0)
        check("拔线期间一次登录都没试过", state["logins"] == 0,
              f"试了 {state['logins']} 次")

        print("\n3) 网线一直插着、门户正常时，仍然会正常重连")
        state = {"link": True, "online": False, "logins": 0, "login_at": None}
        runner_mod.link_up = lambda: state["link"]
        runner = FakeRunner(state)
        threading.Thread(target=lambda: runner.watch(max_cycles=5), daemon=True).start()
        deadline = time.time() + 5
        while state["login_at"] is None and time.time() < deadline:
            time.sleep(0.02)
        check("门户在线但被劫持时能自己登录", state["logins"] >= 1,
              f"登录 {state['logins']} 次")
    finally:
        runner_mod.link_up = real_link_up

    print()
    if failures:
        print(f"有 {len(failures)} 项没过 ❌")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
