"""Credential storage.

On Windows the password is sealed with DPAPI (``CryptProtectData``), so the
config file is useless on another machine or for another user.  Elsewhere we
fall back to plaintext with the config file chmod'ed to 0600.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import os
import stat
from typing import Any

__all__ = ["protect", "unprotect", "is_encrypted"]

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


def harden_permissions(path: str) -> None:
    """Best-effort tightening of a secret-bearing file."""
    try:
        if _IS_WINDOWS:
            os.system(f'icacls "{path}" /inheritance:r /grant:r "%USERNAME%":F >nul 2>&1')
        else:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
