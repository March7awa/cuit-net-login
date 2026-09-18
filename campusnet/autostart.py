"""开关自动登录（Windows 计划任务 + 常驻进程）。

做这个是为了「我想自己看看门户页面，但一拔网线它就把我登进去了」这种情况：
先暂停，随便折腾；弄完再恢复。
"""

from __future__ import annotations

import os
import subprocess

__all__ = ["TASK_NAMES", "is_installed", "is_enabled", "set_enabled",
           "stop_watch_processes", "is_watch_running"]

TASK_NAMES = ("CampusNetLogin", "CampusNetLogin-Fallback")


def _hidden_kwargs() -> dict:
    """隐藏子进程窗口，但**不要**用 CREATE_NO_WINDOW。

    实测：带 CREATE_NO_WINDOW 时 schtasks 的输出会换语言（Folder -> 文件夹）
    且编码不对，导致解析永远匹配不上 —— 这个坑害过 MAC 查询和计划任务检测。
    用 STARTUPINFO/SW_HIDE 同样不弹窗，输出还正常。
    """
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"startupinfo": si}


def _run(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              errors="replace", **_hidden_kwargs())
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def _task_state(task: str) -> str | None:
    code, out = _run(["schtasks", "/query", "/tn", task, "/fo", "list"])
    if code != 0:
        return None
    for line in out.splitlines():
        if ":" in line:
            key = line.split(":", 1)[0].strip().lower()
            if key in ("status", "状态"):
                return line.split(":", 1)[1].strip()
    return ""


def is_installed() -> bool:
    return os.name == "nt" and _task_state(TASK_NAMES[0]) is not None


def is_enabled() -> bool:
    """任务存在且没有被禁用。"""
    if os.name != "nt":
        return False
    state = _task_state(TASK_NAMES[0])
    if state is None:
        return False
    low = state.lower()
    return not any(w in low for w in ("disabled", "已禁用", "禁用"))


def set_enabled(enabled: bool) -> tuple[bool, str]:
    """启用/禁用计划任务。禁用时顺便把正在跑的看门狗停掉。"""
    if os.name != "nt":
        return False, "只有 Windows 需要这个开关（其它系统用 systemd/launchd）"
    flag = "/change" if enabled else "/change"
    action = "/enable" if enabled else "/disable"
    lines = []
    ok_any = False
    for task in TASK_NAMES:
        if _task_state(task) is None:
            continue
        code, out = _run(["schtasks", flag, "/tn", task, action])
        lines.append(f"{task}: {'OK' if code == 0 else out}")
        ok_any = ok_any or code == 0
    if not ok_any:
        return False, "没有找到计划任务（可能还没装开机自启）"
    if not enabled:
        killed = stop_watch_processes()
        lines.append(f"已停止 {killed} 个正在运行的看门狗进程")
    return True, "\n".join(lines)


def _list_watch_pids() -> list[int]:
    if os.name != "nt":
        return []
    code, out = _run([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -like '*campus_login.py*' } | "
        "Select-Object -ExpandProperty ProcessId",
    ], timeout=25)
    if code != 0:
        return []
    return [int(x) for x in out.split() if x.strip().isdigit()]


def is_watch_running() -> bool:
    return bool(_list_watch_pids())


def stop_watch_processes() -> int:
    pids = _list_watch_pids()
    n = 0
    for pid in pids:
        code, _ = _run(["taskkill", "/F", "/PID", str(pid)])
        if code == 0:
            n += 1
    return n
