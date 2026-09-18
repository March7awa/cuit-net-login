"""图形化配置向导 + 主面板。

一个窗口搞定全部事情：第一次打开是分步向导（选认证方式 → 填服务器 →
填账号密码 → 测试登录 → 装开机自启），配好之后同一个窗口变成状态面板。

只用标准库的 tkinter，不装任何东西。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from . import __version__, config as config_mod
from .httpx import HttpClient
from .netutil import (CHECK_URLS, default_local_ip, detect_captive_redirect,
                      is_online, primary_mac)
from .presets import NO_PRESET, PRESETS, key_for_label, preset_choices
from .providers import get_provider, list_providers
from .providers.ruijie_sam_cas import service_choices
from .runner import SingleInstance, build_logger

#: 运营商下拉里的「不指定」项
SERVICE_AUTO = "自动（用列表第一项）"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_FONT = ("Microsoft YaHei UI", 10)
UI_FONT_SMALL = ("Microsoft YaHei UI", 9)
UI_FONT_BIG = ("Microsoft YaHei UI", 13, "bold")
UI_FONT_MONO = ("Consolas", 9)

TASK_NAME = "CampusNetLogin"


# ==========================================================================
# 后台任务辅助 —— 所有网络/子进程操作都不能阻塞界面
# ==========================================================================
def _trace(msg: str) -> None:
    """把关键步骤写到一个独立的跟踪文件。

    界面上的日志框要等 logger 建好才有内容；如果后台线程在建 logger 之前
    就挂了，日志里会一片空白、根本没法查。所以这里用一个不依赖任何东西的
    文件追加，保证「到底走到哪一步」永远有据可查。
    """
    try:
        path = config_mod.log_path().with_name("gui-trace.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 512 * 1024:
            path.unlink()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [pid {os.getpid()}] {msg}\n")
    except Exception:
        pass


class Background:
    """Run a callable on a worker thread and deliver the result on the UI thread.

    Tk is *not* thread-safe，在工作线程里直接调用 ``widget.after()`` 属于未定义
    行为（主线程不在 mainloop 里时会直接抛 RuntimeError，回调就永远丢了，
    界面会一直卡在「正在登录」）。所以工作线程只往队列里塞结果，由 UI 线程
    上的一个轮询器统一取出来执行。
    """

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self._results: queue.Queue = queue.Queue()
        self._poll()

    def _poll(self) -> None:
        while True:
            try:
                callback, result, error = self._results.get_nowait()
            except queue.Empty:
                break
            try:
                callback(result, error)
            except Exception:  # noqa: BLE001
                _trace("回调执行出错:\n" + traceback.format_exc())
        self.root.after(80, self._poll)

    def run(self, fn: Callable[[], Any],
            on_done: Callable[[Any, Exception | None], None]) -> None:
        def worker() -> None:
            try:
                result, error = fn(), None
            except BaseException as exc:  # noqa: BLE001 - 包括 SystemExit
                result, error = None, exc
            self._results.put((on_done, result, error))

        threading.Thread(target=worker, daemon=True).start()


class QueueLogHandler(logging.Handler):
    """Funnel log records into the on-screen log box."""

    def __init__(self, sink: queue.Queue) -> None:
        super().__init__(level=logging.DEBUG)
        self.sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.put_nowait(self.format(record))
        except Exception:
            pass


# ==========================================================================
# 开通/关闭开机自启（复用仓库里的 PowerShell 脚本）
# ==========================================================================
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


def _run_ps(script: Path, *extra: str, timeout: float = 90) -> tuple[bool, str]:
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(script), *extra]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              errors="replace", **_hidden_kwargs())
    except Exception as exc:  # noqa: BLE001
        return False, f"调用 PowerShell 失败: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()


def _open_in_editor(path: Path) -> None:
    """用系统默认程序打开一个文件（Windows 上是记事本/关联程序）。"""
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def autostart_installed() -> bool:
    if os.name != "nt":
        return False
    try:
        proc = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME],
                              capture_output=True, text=True, timeout=15,
                              creationflags=0x08000000)
        return proc.returncode == 0
    except Exception:
        return False


# ==========================================================================
# 自动识别认证方式
# ==========================================================================
PROVIDER_HINTS = (
    ("ruijie_sam_cas", ("/portal/entry", "/cas-sso", "/portal/portal-main")),
    ("ruijie_eportal", ("interface.do", "/eportal/")),
    ("srun", ("srun_portal", "/cgi-bin/", "srun_portal_pc")),
)


def classify_portal(location: str, body: str = "") -> str | None:
    blob = (location + " " + body).lower()
    for name, needles in PROVIDER_HINTS:
        if any(n in blob for n in needles):
            return name
    return None


def portal_origin(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


# ==========================================================================
# 向导
# ==========================================================================
class Wizard(ttk.Frame):
    STEPS = ("选择认证方式", "认证服务器（可跳过）", "账号密码", "测试登录", "完成")

    def __init__(self, master: tk.Misc, app: "App", start_page: int = 0) -> None:
        super().__init__(master, padding=(18, 14))
        self.app = app
        self.bg = Background(app.root)
        self.index = 0

        self.provider_name = tk.StringVar(value="ruijie_sam_cas")
        self.portal = tk.StringVar(value="")
        self.service = tk.StringVar(value=SERVICE_AUTO)
        self.nasip = tk.StringVar(value="")
        self.username = tk.StringVar(value="")
        self.password = tk.StringVar(value="")
        self.auto_start = tk.BooleanVar(value=True)
        self.detect_note = tk.StringVar(value="")
        self.test_note = tk.StringVar(value="")
        self.preset_label = tk.StringVar(value=NO_PRESET)

        self._prefill_from_existing()

        self._build_header()
        # 先把页脚贴到底部，再让内容区去抢剩余空间；
        # 反过来写的话，内容太高会把「下一步」按钮挤出窗口。
        self._build_footer()
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        self.pages = [self._page_provider, self._page_server,
                      self._page_account, self._page_test, self._page_done]
        self.show(start_page)

    def _prefill_from_existing(self) -> None:
        """已经有配置就填上，用户不用重新打一遍。"""
        try:
            existing = config_mod.load(self.app.cfg_path, must_exist=False)
        except Exception:
            return
        if existing.provider:
            self.provider_name.set(existing.provider)
        portal = existing.option("portal", "") or ""
        if portal:
            self.portal.set(portal)
            for item in PRESETS.values():
                if (item.get("options") or {}).get("portal") == portal:
                    self.preset_label.set(item["label"])
                    break
        self.service.set(str(existing.option("service", "") or "") or SERVICE_AUTO)
        self.nasip.set(str(existing.option("nasip", "") or ""))
        if existing.username:
            self.username.set(existing.username)

    # ---------------------------------------------------------------- chrome
    def _build_header(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", pady=(0, 10))
        ttk.Label(head, text="校园网自动登录", font=UI_FONT_BIG).pack(side="left")
        ttk.Label(head, text=f"  配置向导 · v{__version__}",
                  font=UI_FONT_SMALL, foreground="#666").pack(side="left", pady=(6, 0))

        self.stepbar = ttk.Frame(self)
        self.stepbar.pack(fill="x", pady=(0, 12))
        self.step_labels: list[ttk.Label] = []
        for i, title in enumerate(self.STEPS):
            lbl = ttk.Label(self.stepbar, text=f"{i + 1}. {title}", font=UI_FONT_SMALL,
                            foreground="#aaa")
            lbl.pack(side="left")
            if i < len(self.STEPS) - 1:
                ttk.Label(self.stepbar, text="  ›  ", foreground="#ccc").pack(side="left")
            self.step_labels.append(lbl)

    def _build_footer(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(side="bottom", fill="x", pady=(14, 0))
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x", pady=(12, 0))
        self.btn_back = ttk.Button(bar, text="上一步", command=self.back)
        self.btn_back.pack(side="left")
        self.btn_next = ttk.Button(bar, text="下一步", command=self.next)
        self.btn_next.pack(side="right")

    def _clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

    def show(self, index: int) -> None:
        self.index = max(0, min(index, len(self.pages) - 1))
        self._clear()
        # 每页的提示语是各自算出来的，翻页时别把上一页的结论留在屏幕上
        self.detect_note.set("")
        self.pages[self.index]()
        for i, lbl in enumerate(self.step_labels):
            if i == self.index:
                lbl.configure(foreground="#0a7", font=("Microsoft YaHei UI", 9, "bold"))
            elif i < self.index:
                lbl.configure(foreground="#0a7", font=UI_FONT_SMALL)
            else:
                lbl.configure(foreground="#aaa", font=UI_FONT_SMALL)
        self.btn_back.state(["!disabled"] if self.index > 0 else ["disabled"])
        last = self.index == len(self.pages) - 1
        self.btn_next.configure(text="完成" if last else "下一步",
                                command=self.finish if last else self.next)
        if self.index == 3:
            self.btn_next.state(["disabled"])
            self._run_test()
        else:
            self.btn_next.state(["!disabled"])

    # ---------------------------------------------------------------- pages
    def _page_provider(self) -> None:
        ttk.Label(self.body, text="你们学校的校园网是怎么认证的？",
                  font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Label(self.body, foreground="#666", wraplength=560, justify="left",
                  text="先看看有没有你学校 —— 有的话点一下，服务器地址会自动填好。"
                       "没有就点「自动检测」，或者自己在下面选。").pack(anchor="w", pady=(4, 10))

        picker = ttk.Frame(self.body)
        picker.pack(fill="x", pady=(0, 12))
        ttk.Label(picker, text="我的学校", width=10).pack(side="left")
        self.preset_label = tk.StringVar(value=NO_PRESET)
        combo = ttk.Combobox(picker, textvariable=self.preset_label, state="readonly",
                             values=preset_choices(), width=34)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", self._apply_preset)

        box = ttk.LabelFrame(self.body, text=" 认证方式 ", padding=10)
        box.pack(fill="x")
        for cls in list_providers():
            row = ttk.Frame(box)
            row.pack(fill="x", pady=2)
            ttk.Radiobutton(row, value=cls.name, variable=self.provider_name,
                            text=cls.description).pack(anchor="w")
            ttk.Label(row, text=f"     {cls.name}", font=UI_FONT_SMALL,
                      foreground="#999").pack(anchor="w")

        note = ttk.Label(self.body, textvariable=self.detect_note,
                         foreground="#0a7", wraplength=560, justify="left")
        note.pack(anchor="w", pady=(10, 0))

        ttk.Button(self.body, text="自动检测认证方式",
                   command=self._detect).pack(anchor="w", pady=(8, 0))

    def _apply_preset(self, _event=None) -> None:
        key = key_for_label(self.preset_label.get())
        if not key:
            return
        item = PRESETS[key]
        self.provider_name.set(item["provider"])
        portal = (item.get("options") or {}).get("portal", "")
        if portal:
            self.portal.set(portal)
        self.detect_note.set(
            f"已套用「{item['label']}」预设：服务器 {portal or '(待填)'}")

    def _page_server(self) -> None:
        ttk.Label(self.body, text="认证服务器",
                  font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Label(self.body, text="这一页可以整页跳过，直接点「下一步」。",
                  foreground="#0a7", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        ttk.Label(self.body, foreground="#666", wraplength=580, justify="left",
                  text="断开校园网时程序会自己去碰一下网络，从门户的跳转里把服务器地址"
                       "和接入设备地址探测出来，不需要你知道这些。").pack(anchor="w", pady=(4, 10))

        pick = ttk.Frame(self.body)
        pick.pack(fill="x", pady=(0, 10))
        ttk.Button(pick, text="自动检测（推荐先点这个）",
                   command=self._detect_server).pack(side="left")
        ttk.Label(pick, textvariable=self.detect_note, foreground="#0a7",
                  font=UI_FONT_SMALL).pack(side="left", padx=10)

        row = ttk.Frame(self.body)
        row.pack(fill="x")
        ttk.Label(row, text="服务器", width=10).pack(side="left")
        ttk.Entry(row, textvariable=self.portal, width=46).pack(side="left", fill="x", expand=True)
        ttk.Label(self.body, foreground="#888", font=UI_FONT_SMALL,
                  text="　　　留空即可（自动探测）。手填就填认证页那个 IP。").pack(anchor="w", pady=(2, 0))

        # 只有需要选运营商的认证方式（目前是锐捷 SAM）才显示这一行
        try:
            supports_service = "service" in get_provider(self.provider_name.get()).example_options
        except Exception:  # noqa: BLE001
            supports_service = False
        if supports_service:
            row1 = ttk.Frame(self.body)
            row1.pack(fill="x", pady=(8, 0))
            ttk.Label(row1, text="接入设备", width=10).pack(side="left")
            ttk.Entry(row1, textvariable=self.nasip, width=44).pack(
                side="left", fill="x", expand=True)
            ttk.Label(self.body, foreground="#888", font=UI_FONT_SMALL, wraplength=580,
                      justify="left",
                      text="　　　同样留空即可。这个是接入设备（AC）的地址，"
                           "不填的话门户会当成 1.1.1.1，导致拿不到运营商列表、\n"
                           "　　　认证也不会真正放行 —— 所以程序会自动探测、学到并记住它。").pack(
                anchor="w", pady=(2, 0))

            row2 = ttk.Frame(self.body)
            row2.pack(fill="x", pady=(8, 0))
            ttk.Label(row2, text="运营商", width=10).pack(side="left")
            ttk.Combobox(row2, textvariable=self.service, width=44,
                         values=[SERVICE_AUTO] + service_choices()).pack(
                side="left", fill="x", expand=True)
            ttk.Label(self.body, foreground="#888", font=UI_FONT_SMALL, wraplength=580,
                      justify="left",
                      text="很多学校 CAS 认证完还要再选一次运营商才会真正通网，"
                           "不选就一直上不了网。\n"
                           "按你办宽带时选的那家填；不确定就先用「自动」，"
                           "登录日志里会打印门户实际给了哪些选项。").pack(anchor="w", pady=(4, 0))

        info = ttk.LabelFrame(self.body, text=" 本机信息 ", padding=10)
        info.pack(fill="x", pady=14)
        ttk.Label(info, text=f"IP 地址：{default_local_ip() or '取不到'}").pack(anchor="w")
        mac = primary_mac()
        ttk.Label(info, text=f"MAC 地址：{mac or '取不到'}（自动识别，一般不用改）").pack(anchor="w", pady=(4, 0))

        ttk.Label(self.body, foreground="#666", wraplength=560, justify="left",
                  text="如果你的学校不在默认列表里，可以先用上面选好的通用方式，"
                       "配好后再按 README 里的说明补参数。").pack(anchor="w")

    def _detect_server(self) -> None:
        """断开校园网时，从门户的跳转里把服务器地址和接入设备地址探测出来。

        用户不需要知道这两个地址 —— 这正是它们该被自动填的原因。
        """
        self.detect_note.set("正在探测…")

        def work() -> dict:
            import json

            data = json.loads(json.dumps(config_mod.DEFAULTS))
            data["provider"] = self.provider_name.get()
            data["username"] = self.username.get().strip() or "probe"
            opts = data.setdefault("options", {})
            opts["portal"] = self.portal.get().strip()
            opts["nasip"] = self.nasip.get().strip()
            opts["mac"] = "auto"
            cfg = config_mod.Config(data, config_mod.resolve_path_for_probe())
            log = build_logger(cfg, console=False)
            log.addHandler(QueueLogHandler(self.app.log_queue))
            provider = get_provider(cfg.provider)(cfg, HttpClient(timeout=8), log)
            if not hasattr(provider, "discover"):
                return {"ok": False, "reason": f"{cfg.provider} 这个认证方式不需要填这些，直接下一步就行。"}
            return provider.discover()

        def done(result, error) -> None:
            if error:
                self.detect_note.set(f"探测出错：{error}")
                return
            if not result.get("ok"):
                self.detect_note.set("")
                messagebox.showinfo("探测不到", result.get("reason", "未知原因"))
                return
            found = []
            if result.get("portal"):
                self.portal.set(result["portal"])
                found.append(f"服务器 {result['portal']}")
            if result.get("nasip"):
                self.nasip.set(result["nasip"])
                found.append(f"接入设备 {result['nasip']}")
            self.detect_note.set("检测到：" + "，".join(found) if found
                                 else "探测到门户但没取到地址")
            if result.get("note"):
                self.detect_note.set(self.detect_note.get() + f"（{result['note']}）")

        self.bg.run(work, done)

    def _page_account(self) -> None:
        ttk.Label(self.body, text="输入你的校园网账号",
                  font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Label(self.body, foreground="#666", wraplength=560, justify="left",
                  text="密码存在你自己电脑上：Windows 下会用系统 DPAPI 加密，"
                       "换台电脑或换个 Windows 账号都解不开。").pack(anchor="w", pady=(4, 12))

        form = ttk.Frame(self.body)
        form.pack(fill="x")
        ttk.Label(form, text="学号 / 账号", width=12).grid(row=0, column=0, sticky="w", pady=5)
        user_entry = ttk.Entry(form, textvariable=self.username, width=40)
        user_entry.grid(row=0, column=1, sticky="w", pady=5)

        ttk.Label(form, text="密码", width=12).grid(row=1, column=0, sticky="w", pady=5)
        self.pwd_entry = ttk.Entry(form, textvariable=self.password, width=40, show="●")
        self.pwd_entry.grid(row=1, column=1, sticky="w", pady=5)

        self.show_pwd = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="显示密码", variable=self.show_pwd,
                        command=self._toggle_pwd).grid(row=1, column=2, padx=8)

        ttk.Label(self.body, foreground="#888", font=UI_FONT_SMALL,
                  text="提示：输密码时不会回显，这是正常的。").pack(anchor="w", pady=(10, 0))
        user_entry.focus_set()

    def _toggle_pwd(self) -> None:
        self.pwd_entry.configure(show="" if self.show_pwd.get() else "●")

    def _page_test(self) -> None:
        ttk.Label(self.body, text="测试登录",
                  font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Label(self.body, foreground="#666", wraplength=560, justify="left",
                  text="正在用你填的信息真实登录一次。成功就说明一切正常。").pack(anchor="w", pady=(4, 14))

        self.test_spinner = ttk.Progressbar(self.body, mode="indeterminate", length=420)
        self.test_spinner.pack(anchor="w")
        self.test_spinner.start(12)
        ttk.Label(self.body, textvariable=self.test_note, wraplength=560,
                  justify="left").pack(anchor="w", pady=(12, 0))
        ttk.Button(self.body, text="重新测试", command=self._run_test).pack(anchor="w", pady=(14, 0))
        ttk.Label(self.body, foreground="#888", font=UI_FONT_SMALL, wraplength=560,
                  justify="left",
                  text="测不通也不影响继续：可以直接点「下一步」把配置留着，\n"
                       "之后在主界面点「立即登录」或「重新配置」。").pack(anchor="w", pady=(10, 0))

    def _page_done(self) -> None:
        ttk.Label(self.body, text="配置完成 🎉",
                  font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(self.body, foreground="#666", wraplength=560, justify="left",
                  text="最后一步：把它设成开机自启，以后就不用管了。").pack(anchor="w", pady=(4, 12))

        ttk.Checkbutton(self.body, variable=self.auto_start,
                        text="安装开机自启（推荐）——开机自动登录，断线自动重连").pack(anchor="w")

        ttk.Label(self.body, foreground="#888", font=UI_FONT_SMALL, wraplength=560,
                  justify="left", text=(
                      "\n会在 Windows 计划任务里建两个任务：\n"
                      "  · 登录 Windows 时常驻后台看门狗\n"
                      "  · 每 5 分钟兜底检查一次\n"
                      "不需要管理员权限，随时可以关掉。")).pack(anchor="w", pady=(10, 0))

    # ---------------------------------------------------------------- actions
    def _detect(self) -> None:
        self.detect_note.set("正在探测…")
        self.update_idletasks()

        def work() -> dict:
            client = HttpClient(timeout=8)
            hit = detect_captive_redirect(client, checks=CHECK_URLS)
            if not hit:
                return {"found": False}
            url, location = hit
            body = ""
            if not location:
                try:
                    body = client.get(url, follow=True, timeout=8).text[:4000]
                except Exception:
                    pass
            return {"found": True, "url": url, "location": location or "", "body": body}

        def done(result, error) -> None:
            if error:
                self.detect_note.set(f"探测失败：{error}")
                return
            if not result.get("found"):
                self.detect_note.set(
                    "现在网络是通的，网关没有把我们重定向到认证页，所以检测不出来。\n"
                    "没关系 —— 直接按下面默认选中的那项继续就行。")
                return
            location = result.get("location", "")
            guess = classify_portal(location, result.get("body", ""))
            origin = portal_origin(location)
            if origin:
                self.portal.set(origin)
            if guess:
                self.provider_name.set(guess)
                cls = get_provider(guess)
                self.detect_note.set(
                    f"检测到认证服务器 {origin or '(同源)'}\n"
                    f"已自动选择：{cls.description}")
            else:
                self.detect_note.set(
                    f"检测到被重定向到：{location[:160]}\n"
                    "认不出具体厂商，已保留当前选择；如果登录失败请换一个认证方式试试。")

        self.bg.run(work, done)

    def next(self) -> None:
        if self.index == 0 and not self.provider_name.get():
            messagebox.showwarning("提示", "请先选择一种认证方式")
            return
        if self.index == 2:
            if not self.username.get().strip():
                messagebox.showwarning("提示", "请填写账号")
                return
            if not self.password.get():
                messagebox.showwarning("提示", "请填写密码")
                return
            if not self._save_config():
                return
        self.show(self.index + 1)

    def back(self) -> None:
        self.show(self.index - 1)

    def _save_config(self) -> bool:
        try:
            # 必须用 resolve_config_path()：它会认 -c / CAMPUS_LOGIN_CONFIG，
            # 而 default_config_path() 会无视它们，导致写到别处去。
            path = config_mod.resolve_config_path()
            cfg = config_mod.load(path, must_exist=False)
            cls = get_provider(self.provider_name.get())
            cfg.raw["provider"] = cls.name
            options = dict(cfg.raw.get("options") or {})
            options["portal"] = self.portal.get().strip().rstrip("/")
            options.setdefault("mac", "auto")
            for key, value in cls.example_options.items():
                options.setdefault(key, value)
            if "nasip" in cls.example_options:
                options["nasip"] = self.nasip.get().strip()
            if "service" in cls.example_options:
                chosen = self.service.get().strip()
                options["service"] = "" if chosen == SERVICE_AUTO else chosen
            cfg.raw["options"] = options
            cfg.raw["username"] = self.username.get().strip()
            cfg.set_password(self.password.get())
            cfg.save()
            self.app.cfg_path = cfg.path
            return True
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))
            return False

    TEST_TIMEOUT_SECONDS = 60

    def _run_test(self) -> None:
        self._test_gen = getattr(self, "_test_gen", 0) + 1
        gen = self._test_gen
        _trace(f"step4: 开始测试登录 (gen={gen})")

        self.test_note.set(f"正在登录，请稍候…（最多等 {self.TEST_TIMEOUT_SECONDS} 秒）")
        self.btn_next.state(["disabled"])
        self._cancel_test_timer()
        self._test_timer = self.after(self.TEST_TIMEOUT_SECONDS * 1000,
                                      lambda: self._test_timed_out(gen))

        def work() -> dict:
            _trace("step4: worker 启动")
            cfg = config_mod.load(self.app.cfg_path)
            _trace(f"step4: 配置已读 user={cfg.username!r} provider={cfg.provider!r}")
            log = build_logger(cfg, console=False)
            log.addHandler(QueueLogHandler(self.app.log_queue))
            client = HttpClient()
            provider = get_provider(cfg.provider)(cfg, client, log)
            problem = provider.validate()
            if problem:
                _trace(f"step4: 配置校验没过: {problem}")
                return {"ok": False, "message": f"配置有误：{problem}"}
            result = provider.login()
            _trace(f"step4: login 返回 ok={result.ok} msg={result.message}")
            return {"ok": result.ok, "message": result.message}

        def done(result, error) -> None:
            if gen != getattr(self, "_test_gen", 0):
                return
            self._cancel_test_timer()
            _trace(f"step4: 回调触发 ok={None if result is None else result.get('ok')} error={error!r}")
            try:
                self.test_spinner.stop()
            except Exception:
                pass
            if error is not None:
                self.test_note.set(
                    "❌ 测试过程中出错了：\n"
                    f"{type(error).__name__}: {error}\n\n"
                    "可以直接点「下一步」把配置留着，之后再排查。")
            elif result.get("ok"):
                self.test_note.set(f"✅ 登录成功！\n{result.get('message', '')}")
            else:
                self.test_note.set(
                    "❌ 登录没有成功：\n"
                    f"{result.get('message', '')}\n\n"
                    "可以点「上一步」改密码，或换个认证方式再试。\n"
                    "不影响你继续 —— 也可以先完成，之后再回来重新配置。")
            self.btn_next.state(["!disabled"])

        self.bg.run(work, done)

    def _cancel_test_timer(self) -> None:
        timer = getattr(self, "_test_timer", None)
        if timer:
            try:
                self.after_cancel(timer)
            except Exception:
                pass
        self._test_timer = None

    def _test_timed_out(self, gen: int) -> None:
        if gen != getattr(self, "_test_gen", 0):
            return
        _trace(f"step4: 超时（{self.TEST_TIMEOUT_SECONDS}s）")
        self._test_timer = None
        try:
            self.test_spinner.stop()
        except Exception:
            pass
        self.test_note.set(
            f"⏱ 等了 {self.TEST_TIMEOUT_SECONDS} 秒还没结果，先不等了。\n"
            "常见原因：门户没响应、网线没插好、或者被学校的安全策略拦了。\n"
            "可以先点「下一步」继续，之后在主界面点「立即登录」再试。")
        self.btn_next.state(["!disabled"])

    def finish(self) -> None:
        if self.auto_start.get() and os.name == "nt":
            ok, out = _run_ps(PROJECT_ROOT / "scripts" / "install-windows.ps1")
            if ok:
                messagebox.showinfo("完成", "开机自启已装好，以后不用管了。")
            else:
                messagebox.showwarning(
                    "开机自启没装上",
                    "其余配置都保存好了，但注册计划任务失败：\n\n" + out[-800:])
        self.app.show_main()


# ==========================================================================
# 主面板
# ==========================================================================
class MainPanel(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "App") -> None:
        super().__init__(master, padding=(18, 14))
        self.app = app
        self.bg = Background(app.root)
        self.status_var = tk.StringVar(value="正在检查…")
        self.detail_var = tk.StringVar(value="")
        self.start_var = tk.StringVar(value="")
        self._build()
        self.refresh()

    def _build(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x")
        ttk.Label(head, text="校园网自动登录", font=UI_FONT_BIG).pack(side="left")
        ttk.Label(head, text=f"  v{__version__}", font=UI_FONT_SMALL,
                  foreground="#999").pack(side="left", pady=(6, 0))

        card = ttk.LabelFrame(self, text=" 当前状态 ", padding=12)
        card.pack(fill="x", pady=(12, 0))
        self.status_label = ttk.Label(card, textvariable=self.status_var,
                                      font=("Microsoft YaHei UI", 14, "bold"))
        self.status_label.pack(anchor="w")
        ttk.Label(card, textvariable=self.detail_var, foreground="#555",
                  justify="left").pack(anchor="w", pady=(6, 0))

        btns = ttk.Frame(self)
        btns.pack(fill="x", pady=12)
        ttk.Button(btns, text="立即登录", command=self.login_now).pack(side="left")
        ttk.Button(btns, text="刷新状态", command=self.refresh).pack(side="left", padx=6)
        ttk.Button(btns, text="修改密码", command=self.change_password).pack(side="left", padx=6)
        ttk.Button(btns, text="打开配置文件", command=self.open_config).pack(side="left", padx=6)

        btns2 = ttk.Frame(self)
        btns2.pack(fill="x", pady=(0, 12))
        ttk.Button(btns2, text="重新配置", command=self.app.show_wizard).pack(side="left")
        ttk.Button(btns2, text="打开日志", command=self.open_log).pack(side="left", padx=6)
        ttk.Label(btns2, text="密码随时可以改，不用重跑向导", foreground="#888",
                  font=UI_FONT_SMALL).pack(side="left", padx=6)

        start = ttk.LabelFrame(self, text=" 开机自启 ", padding=12)
        start.pack(fill="x")
        ttk.Label(start, textvariable=self.start_var).pack(side="left")
        self.btn_start = ttk.Button(start, text="…", command=self.toggle_autostart)
        self.btn_start.pack(side="right")

        logbox = ttk.LabelFrame(self, text=" 运行日志 ", padding=6)
        logbox.pack(fill="both", expand=True, pady=(12, 0))
        self.log_text = tk.Text(logbox, height=10, font=UI_FONT_MONO, wrap="none",
                                background="#1e1e1e", foreground="#d4d4d4",
                                insertbackground="#d4d4d4", relief="flat")
        scroll = ttk.Scrollbar(logbox, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        self._drain_log()

    # ---------------------------------------------------------------- status
    def refresh(self) -> None:
        self.status_var.set("正在检查…")
        self.status_label.configure(foreground="#666")

        def work() -> dict:
            cfg = config_mod.load(self.app.cfg_path, must_exist=False)
            client = HttpClient(timeout=6)
            online = is_online(client)
            return {
                "online": online,
                "ip": default_local_ip(),
                "mac": primary_mac(),
                "provider": cfg.provider,
                "username": cfg.username,
                "start": autostart_installed(),
            }

        def done(result, error) -> None:
            if error:
                self.status_var.set("检查失败")
                self.detail_var.set(str(error))
                return
            if result["online"]:
                self.status_var.set("● 在线")
                self.status_label.configure(foreground="#0a7")
            else:
                self.status_var.set("● 离线 / 被门户拦截")
                self.status_label.configure(foreground="#d33")
            try:
                cls = get_provider(result["provider"])
                desc = cls.description
            except Exception:
                desc = result["provider"]
            self.detail_var.set(
                f"账号：{result['username'] or '(未设置)'}      认证方式：{desc}\n"
                f"IP：{result['ip'] or '未知'}      MAC：{result['mac'] or '未知'}\n"
                f"配置文件：{self.app.cfg_path}"
            )
            installed = result["start"]
            self.start_var.set("已开启：开机会自动登录，断线会自动重连"
                               if installed else "未开启：需要手动登录")
            self.btn_start.configure(text="关闭自启" if installed else "安装自启")

        self.bg.run(work, done)

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _drain_log(self) -> None:
        drained = 0
        while drained < 40:
            try:
                self._log(self.app.log_queue.get_nowait())
                drained += 1
            except queue.Empty:
                break
        self.after(250, self._drain_log)

    # ---------------------------------------------------------------- actions
    def login_now(self) -> None:
        self._log("— 手动登录 —")
        _trace("main: 点击立即登录")

        def work() -> dict:
            _trace("main: login worker 启动")
            cfg = config_mod.load(self.app.cfg_path)
            _trace(f"main: 配置已读 user={cfg.username!r} has_password={cfg.has_password()}")
            log = build_logger(cfg, console=False)
            log.addHandler(QueueLogHandler(self.app.log_queue))
            client = HttpClient()
            provider = get_provider(cfg.provider)(cfg, client, log)
            if not cfg.has_password():
                return {"ok": False, "need_password": True,
                        "message": "还没设置密码，点「修改密码」填一个就行。"}
            try:
                cfg.password()  # 顺便确认密文能解开（换了电脑/换了用户就会失败）
            except Exception:  # noqa: BLE001
                return {"ok": False, "need_password": True,
                        "message": "已保存的密码在这台电脑上解不开（配置是从别的电脑"
                                   "拷过来的，或者换过 Windows 账号）。\n"
                                   "点「修改密码」重新输一次就好了。"}
            result = provider.login()
            _trace(f"main: login 返回 ok={result.ok} msg={result.message}")
            return {"ok": result.ok, "message": result.message}

        def done(result, error) -> None:
            _trace(f"main: login 回调 error={error!r}")
            if error:
                self._log(f"失败：{error}")
                messagebox.showerror("登录失败", str(error))
            elif result["ok"]:
                self._log(f"成功：{result['message']}")
                messagebox.showinfo("登录成功", result["message"])
            else:
                self._log(f"失败：{result['message']}")
                if result.get("need_password"):
                    # 密码没设 / 解不开，别把用户丢在死路上，直接问他要不要现在改
                    if messagebox.askyesno("需要密码", result["message"] + "\n\n现在设置吗？"):
                        self.change_password()
                        return
                else:
                    messagebox.showerror("登录失败", result["message"])
            self.refresh()

        self.bg.run(work, done)

    def toggle_autostart(self) -> None:
        want_install = not autostart_installed()
        self._log("— 安装开机自启 —" if want_install else "— 关闭开机自启 —")

        def work() -> tuple[bool, str]:
            script = PROJECT_ROOT / "scripts" / "install-windows.ps1"
            return _run_ps(script, *([] if want_install else ["-Uninstall"]))

        def done(result, error) -> None:
            if error:
                messagebox.showerror("操作失败", str(error))
            else:
                ok, out = result
                if not ok:
                    messagebox.showerror("操作失败", out[-1200:] or "未知错误")
                else:
                    self._log(out[-600:])
            self.refresh()

        self.bg.run(work, done)

    def open_log(self) -> None:
        path = config_mod.log_path()
        if not path.exists():
            messagebox.showinfo("日志", f"还没有日志文件：\n{path}\n\n先点一次「立即登录」就会生成。")
            return
        _open_in_editor(path)

    def open_config(self) -> None:
        """直接打开 config.json，让用户想改什么就改什么。

        密码在 Windows 上是 DPAPI 密文，手改不了 —— 那就用「修改密码」。
        其它字段（账号、运营商、间隔……）手改都有效。
        """
        path = self.app.cfg_path
        try:
            if not path.exists():
                # 没有就现造一个，别让用户对着「文件不存在」发呆
                cfg = config_mod.load(path, must_exist=False)
                cfg.save()
            _open_in_editor(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "打不开配置文件",
                f"{path}\n\n{exc}\n\n你可以用记事本手动打开这个路径。")

    def change_password(self) -> None:
        """单独改密码 —— 不该为了改个密码重跑五步向导。"""
        _trace("main: 点击修改密码")
        first = simpledialog.askstring("修改密码", "新的校园网密码：",
                                       show="●", parent=self)
        if not first:
            return
        second = simpledialog.askstring("修改密码", "再输一次确认：",
                                        show="●", parent=self)
        if second is None:
            return
        if first != second:
            messagebox.showwarning("修改密码", "两次输入不一致，没改动。")
            return

        try:
            cfg = config_mod.load(self.app.cfg_path, must_exist=False)
            cfg.set_password(first)
            cfg.save()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("修改密码失败", str(exc))
            return

        self._log("— 密码已更新 —")
        if messagebox.askyesno("修改密码", "密码已保存。现在就登录一次试试吗？"):
            self.login_now()
        else:
            self.refresh()


# ==========================================================================
# 应用外壳
# ==========================================================================
class App:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("校园网自动登录")
        self.root.geometry("640x560")
        self.root.minsize(600, 520)
        self._center(640, 560)

        # 界面上任何没被处理的异常都要留下痕迹并告诉用户，
        # 否则 pythonw 没有控制台，出错就是「点了没反应」。
        self.root.report_callback_exception = self._on_ui_error
        self._set_icon()

        try:
            ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
        except Exception:
            pass

        self.cfg_path = config_mod.resolve_config_path()
        self.log_queue: queue.Queue = queue.Queue()
        self.container = ttk.Frame(self.root)
        self.container.pack(fill="both", expand=True)
        self.current: ttk.Frame | None = None

        _trace(f"GUI 启动: cfg_path={self.cfg_path} 已配置={self._configured()}")
        if self._configured():
            self.show_main()
        elif self._needs_password_only():
            _trace("GUI: 只差密码，向导从「账号密码」页开始")
            self.show_wizard(start_page=2)
        else:
            self.show_wizard()

    def _set_icon(self) -> None:
        """给窗口/任务栏换上自己的图标。

        assets/icon.ico / icon.png 是可选的：没有就安静地用 Python 默认图标，
        想换成自己学校的校徽就丢一张图进去重跑 tools/make_icon.py。
        """
        ico = PROJECT_ROOT / "assets" / "icon.ico"
        png = PROJECT_ROOT / "assets" / "icon.png"
        if ico.exists() and os.name == "nt":
            try:
                self.root.iconbitmap(default=str(ico))
            except Exception:
                pass
        if png.exists():
            try:
                # 必须留一个引用，否则 PhotoImage 会被 GC 掉、图标消失
                self._icon_image = tk.PhotoImage(file=str(png))
                self.root.iconphoto(True, self._icon_image)
            except Exception:
                pass

    def _on_ui_error(self, exc_type, exc_value, exc_tb) -> None:
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _trace("界面回调异常:\n" + detail)
        try:
            messagebox.showerror(
                "出错了",
                f"{exc_type.__name__}: {exc_value}\n\n"
                f"详细信息已写入:\n{config_mod.log_path().with_name('gui-trace.log')}")
        except Exception:
            pass

    def _center(self, w: int, h: int) -> None:
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _configured(self) -> bool:
        try:
            cfg = config_mod.load(self.cfg_path, must_exist=False)
            return self.cfg_path.exists() and cfg.has_password() and bool(cfg.username)
        except Exception:
            return False

    def _needs_password_only(self) -> bool:
        """配置都在、只差密码 —— 直接从「账号密码」那一步开始。"""
        try:
            cfg = config_mod.load(self.cfg_path, must_exist=False)
        except Exception:
            return False
        return bool(self.cfg_path.exists() and cfg.username
                    and cfg.option("portal") and not cfg.has_password())

    def _swap(self, frame: ttk.Frame) -> None:
        if self.current is not None:
            self.current.destroy()
        self.current = frame
        frame.pack(fill="both", expand=True)

    def show_wizard(self, start_page: int = 0) -> None:
        self.root.geometry("660x600")
        self._swap(Wizard(self.container, self, start_page=start_page))

    def show_main(self) -> None:
        self.root.geometry("640x600")
        self._swap(MainPanel(self.container, self))

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    if os.name == "nt":
        try:  # 高 DPI 下文字才清晰
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
