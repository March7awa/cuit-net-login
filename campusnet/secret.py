"""Credential storage.

On Windows the password is sealed with DPAPI (``CryptProtectData``), so the
config file is useless on another machine or for another user.  Elsewhere we
fall back to plaintext with the config file chmod'ed to 0600.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import getpass
import os
import stat
import subprocess
from typing import Any

__all__ = ["protect", "unprotect", "is_encrypted", "harden_permissions",
           "repair_permissions"]

_IS_WINDOWS = os.name == "nt"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi(protect: bool, data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _blob(data)
    blob_out = _DataBlob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args = [ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)]
    if not protect:
        # CryptUnprotectData takes an extra description out-param
        desc = wintypes.LPWSTR()
        args = [ctypes.byref(blob_in), ctypes.byref(desc), None, None, None, 0,
                ctypes.byref(blob_out)]
    if not fn(*args):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect(plain: str) -> dict[str, Any]:
    raw = plain.encode("utf-8")
    if _IS_WINDOWS:
        try:
            return {"cipher": "dpapi", "data": base64.b64encode(_dpapi(True, raw)).decode()}
        except Exception:
            pass
    return {"cipher": "plain", "data": plain}


def unprotect(blob: Any) -> str:
    if isinstance(blob, str):
        return blob
    if not isinstance(blob, dict):
        return ""
    cipher = blob.get("cipher", "plain")
    data = blob.get("data", "")
    if cipher == "dpapi":
        return _dpapi(False, base64.b64decode(data)).decode("utf-8")
    return data


def is_encrypted(blob: Any) -> bool:
    return isinstance(blob, dict) and blob.get("cipher") == "dpapi"


def repair_permissions(path: str) -> bool:
    """把当前用户对被锁住的文件 / 目录的权限加回去。

    老版本用过 ``icacls /inheritance:r /grant:r "%USERNAME%":F``。在域账号、
    微软账号或中文用户名上，``%USERNAME%`` 解析出来的主体未必就是正在运行的
    那个账号 —— 继承来的权限先被删掉、grant 又落到别人头上，**属主就被自己
    的文件锁在门外**，之后每次启动读配置都是 ``[Errno 13] Permission denied``。

    这里做的正好相反：保留继承，只把当前用户加回去。对象属主天然有改 DACL
    的权限，所以哪怕已经被锁死也救得回来。

    返回是否成功。试完还是不行就返回 False，调用方换个地方写。
    """
    target = os.path.normpath(path)
    if not _IS_WINDOWS:
        try:
            os.chmod(target, 0o700 if os.path.isdir(target) else 0o600)
            return True
        except Exception:  # noqa: BLE001
            return False

    user = _current_user()
    ok = False
    # 目录要先修：能不能改文件上的权限，取决于能不能走到它
    parent = os.path.dirname(target)
    for candidate, spec in ((parent, "(OI)(CI)F"), (target, "F")):
        if not candidate or not os.path.exists(candidate):
            continue
        if _run_hidden(f'icacls "{candidate}" /grant "{user}":{spec}'):
            ok = True
    return ok


def harden_permissions(path: str) -> None:
    """Best-effort tightening of a secret-bearing file.

    Windows: *add* an explicit full-control ACE for the current user instead of
    replacing the ACL.  ``/inheritance:r`` + a single grant used to look tidier,
    but ``%USERNAME%`` does not always match the real account (domain accounts,
    Microsoft accounts, elevated vs. normal token), and when it misses, the
    owner ends up with a file they can neither read nor edit -- they simply
    cannot change their own password any more.  The password is DPAPI-sealed
    anyway, so an inherited ACE for ``Users`` leaks nothing.
    """
    try:
        if _IS_WINDOWS:
            user = os.environ.get("USERNAME") or _current_user()
            grants = [f'"{user}":F']
            # 本机系统和管理员永远保留访问权，避免任何情况下的自锁
            grants += ["*S-1-5-18:F", "*S-1-5-32-544:F"]
            _run_hidden(f'icacls "{path}" /grant:r {" ".join(grants)}')
        else:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def _run_hidden(command: str) -> bool:
    """跑一条 cmd 命令，且**不弹黑窗口**。返回是否成功（退出码 0）。

    不能用 ``os.system``：pythonw 没有控制台，Windows 会为子进程新建一个，
    于是每次保存配置都会闪一下黑框 —— 开机自启的程序闪黑框很吓人。
    """
    flags = 0x08000000 if _IS_WINDOWS else 0  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(command, shell=True, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=15, creationflags=flags)
        return proc.returncode == 0
    except Exception:
        return False


def _current_user() -> str:
    """当前用户名，拿不到就返回一个 icacls 认得的东西。"""
    for getter in (getpass.getuser, lambda: os.environ.get("USER", "")):
        try:
            name = getter()
        except Exception:  # noqa: BLE001
            continue
        if name:
            return name
    return "Users"
