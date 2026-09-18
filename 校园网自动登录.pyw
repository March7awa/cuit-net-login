"""校园网自动登录 —— 双击这个文件就行。

Windows 下 .pyw 会用 pythonw.exe 运行，不会弹黑框。
第一次打开是分步配置向导，配好之后就是状态面板。

注意：这里**故意不写 shebang 行**。Windows 把 .pyw 关联到 ``pyw.exe``
（Python 启动器），而启动器会去解析第一行的 shebang：写成
``#!/usr/bin/env pythonw`` 会让它去找一个叫 pythonw 的「运行时」，
直接报 "No suitable Python runtime found"，双击毫无反应。
不写 shebang 它就用默认的 Python，最稳。
"""

import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)


def _fatal(message: str, detail: str = "") -> None:
    """双击运行时没有控制台，出错了必须弹窗，否则用户以为没反应。"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("校园网自动登录 - 启动失败", message + ("\n\n" + detail if detail else ""))
        root.destroy()
    except Exception:
        sys.stderr.write(message + "\n" + detail + "\n")


try:
    import tkinter  # noqa: F401
except ImportError as exc:
    _fatal(
        "你的 Python 没有带 tkinter，界面起不来。\n\n"
        "请到 python.org 下载官方安装包重新安装，安装时勾选 "
        "'tcl/tk and IDLE'。",
        str(exc),
    )
    raise SystemExit(1)

try:
    from campusnet.gui import main

    raise SystemExit(main())
except SystemExit:
    raise
except Exception:
    _fatal("程序启动时出错：", traceback.format_exc())
    raise SystemExit(1)
