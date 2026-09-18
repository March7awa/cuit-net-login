"""Network helpers: primary IP/MAC lookup and captive-portal detection.

The Windows MAC lookup goes straight to ``iphlpapi!GetAdaptersAddresses`` via
ctypes instead of shelling out to PowerShell/getmac.  That is instant,
locale-independent, and — unlike ``uuid.getnode()`` — it will not hand back a
VMware/Hyper-V/Bluetooth adapter when the real NIC is carrying the default
route.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import functools
import os
import platform
import re
import socket
import subprocess
import uuid
from typing import Iterable, Iterator

__all__ = [
    "default_local_ip",
    "primary_mac",
    "normalize_mac",
    "list_interfaces",
    "is_online",
    "detect_captive_redirect",
    "CHECK_URLS",
]

# Endpoints that answer with a known body/status when the network is open.
# Any HTTP response that is *not* the expected one means "intercepted".
# Ordered by how reliably they are reachable from mainland China; the first
# entry usually produces the captive-portal redirect, which lets the offline
# path finish without waiting on the slower probes.
CHECK_URLS: tuple[tuple[str, object], ...] = (
    # (url, expected) where expected is an int status or a substring of the body
    ("http://connect.rom.miui.com/generate_204", 204),
    ("http://www.msftconnecttest.com/connecttest.txt", "Microsoft Connect Test"),
    ("http://captive.apple.com/hotspot-detect.html", "Success"),
    ("http://connectivitycheck.gstatic.com/generate_204", 204),
)

PROBE_TIMEOUT = 5.0

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


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


def _run(cmd: list[str], timeout: float = 25.0) -> str:
    try:
        out = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            text=True, errors="replace", **_hidden_kwargs(),
        )
        return out.stdout or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------
# default-route IP
# --------------------------------------------------------------------------
def default_local_ip() -> str | None:
    """The local IP the OS would use to reach the internet (no packets sent).

    刻意**不缓存**：拔网线/换网络/开机过程中这个值会变。看门狗是个长期
    运行的进程，缓存住会导致「明明没网了还以为有 IP」，或者拿着旧 IP 去
    发请求。这个调用只是一次 UDP connect，开销可以忽略。
    """
    for target in (("8.8.8.8", 53), ("223.5.5.5", 53), ("114.114.114.114", 53)):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(target)
            return s.getsockname()[0]
        except Exception:
            continue
        finally:
            s.close()
    return None


def normalize_mac(value: str | None) -> str | None:
    """Return a bare lowercase 12-hex-char MAC, or None."""
    if not value:
        return None
    hexonly = re.sub(r"[^0-9a-fA-F]", "", value)
    if len(hexonly) != 12 or hexonly == "0" * 12:
        return None
    return hexonly.lower()


# --------------------------------------------------------------------------
# Windows: GetAdaptersAddresses
# --------------------------------------------------------------------------
if os.name == "nt":
    _AF_UNSPEC = 0
    _AF_INET = 2
    _GAA_FLAG_SKIP_ANYCAST = 0x0002
    _GAA_FLAG_SKIP_MULTICAST = 0x0004
    _GAA_FLAG_SKIP_DNS_SERVER = 0x0008
    _IF_TYPE_SOFTWARE_LOOPBACK = 24
    _IfOperStatusUp = 1
    _ERROR_BUFFER_OVERFLOW = 111

    class _SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]

    class _IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
        pass

    _IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
        ("Length", wt.ULONG),
        ("Flags", wt.DWORD),
        ("Next", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
        ("Address", _SOCKET_ADDRESS),
        ("PrefixOrigin", ctypes.c_int),
        ("SuffixOrigin", ctypes.c_int),
        ("DadState", ctypes.c_int),
        ("ValidLifetime", wt.ULONG),
        ("PreferredLifetime", wt.ULONG),
        ("LeaseLifetime", wt.ULONG),
        ("OnLinkPrefixLength", ctypes.c_ubyte),
    ]

    class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
        pass

    _IP_ADAPTER_ADDRESSES._fields_ = [
        ("Length", wt.ULONG),
        ("IfIndex", wt.DWORD),
        ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", wt.LPWSTR),
        ("Description", wt.LPWSTR),
        ("FriendlyName", wt.LPWSTR),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", wt.ULONG),
        ("Flags", wt.ULONG),
        ("Mtu", wt.ULONG),
        ("IfType", wt.ULONG),
        ("OperStatus", ctypes.c_int),
    ]

    def _sockaddr_to_ip(pointer: int, length: int) -> str | None:
        if not pointer or length < 8:
            return None
        raw = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ubyte * length)).contents
        if raw[0] != _AF_INET:  # sa_family is a little-endian u16
            return None
        return ".".join(str(raw[4 + i]) for i in range(4))

    def _windows_adapters() -> tuple[dict, ...]:
        """All adapters with MAC + unicast IPv4 addresses.

        不缓存：拔插网线、开关 Wi-Fi 之后网卡列表和 IP 都会变，看门狗是常驻
        进程，缓存住就会一直拿到旧状态。
        """
        try:
            iphlpapi = ctypes.WinDLL("iphlpapi")
        except OSError:
            return ()

        flags = (_GAA_FLAG_SKIP_ANYCAST | _GAA_FLAG_SKIP_MULTICAST
                 | _GAA_FLAG_SKIP_DNS_SERVER)
        size = wt.ULONG(16 * 1024)
        for _ in range(3):
            buf = ctypes.create_string_buffer(size.value)
            ret = iphlpapi.GetAdaptersAddresses(
                wt.ULONG(_AF_UNSPEC), wt.ULONG(flags), None, buf, ctypes.byref(size)
            )
            if ret == _ERROR_BUFFER_OVERFLOW:
                continue
            if ret != 0:
                return ()
            break
        else:
            return ()

        adapters: list[dict] = []
        node = ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
        while node:
            item = node.contents
            mac_bytes = bytes(item.PhysicalAddress[:item.PhysicalAddressLength])
            mac = normalize_mac(mac_bytes.hex()) if len(mac_bytes) == 6 else None

            ips: list[str] = []
            uni = item.FirstUnicastAddress
            while uni:
                addr = uni.contents.Address
                ip = _sockaddr_to_ip(addr.lpSockaddr, addr.iSockaddrLength)
                if ip:
                    ips.append(ip)
                uni = uni.contents.Next

            adapters.append({
                "ifindex": item.IfIndex,
                "mac": mac,
                "ipv4": tuple(ips),
                "name": item.FriendlyName or "",
                "description": item.Description or "",
                "oper_status": item.OperStatus,
                "if_type": item.IfType,
            })
            node = item.Next
        return tuple(adapters)


def list_interfaces() -> list[dict]:
    """Every network interface with its MAC and IPv4 addresses."""
    if os.name == "nt":
        return list(_windows_adapters())

    out: list[dict] = []
    if platform.system() == "Linux":
        try:
            for iface in sorted(os.listdir("/sys/class/net")):
                addr = f"/sys/class/net/{iface}/address"
                mac = None
                if os.path.exists(addr):
                    with open(addr) as fh:
                        mac = normalize_mac(fh.read().strip())
                ips = _run(["ip", "-o", "-4", "addr", "show", "dev", iface])
                out.append({
                    "ifindex": iface,
                    "mac": mac,
                    "ipv4": tuple(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", ips)),
                    "name": iface,
                    "description": iface,
                    "oper_status": 1 if os.path.exists(f"/sys/class/net/{iface}/operstate") else 0,
                })
        except Exception:
            pass
    elif platform.system() == "Darwin":
        current: dict | None = None
        for line in _run(["ifconfig"]).splitlines():
            if line and not line[0].isspace():
                if current:
                    out.append(current)
                current = {"ifindex": line.split(":")[0], "mac": None, "ipv4": (),
                           "name": line.split(":")[0], "description": "", "oper_status": 0}
            elif current is not None:
                m = re.search(r"ether\s+([0-9a-fA-F:]{17})", line)
                if m:
                    current["mac"] = normalize_mac(m.group(1))
                m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", line)
                if m:
                    current["ipv4"] = current["ipv4"] + (m.group(1),)
                    current["oper_status"] = 1
        if current:
            out.append(current)
    return out


@functools.lru_cache(maxsize=4)
def _mac_for_ip(ip: str) -> str | None:
    for iface in list_interfaces():
        if ip in iface.get("ipv4", ()) and iface.get("mac"):
            return iface["mac"]
    return None


def primary_mac() -> str | None:
    """MAC of the interface carrying the default route.

    Never falls back to a hypervisor/Bluetooth adapter while a real NIC owns
    the route, because the route lookup is done first.
    """
    mac = _mac_for_ip(default_local_ip() or "")
    if mac:
        return mac

    # Fall back to the first *up* physical adapter that has a MAC.
    for iface in list_interfaces():
        if iface.get("mac") and iface.get("oper_status") == 1 and iface.get("if_type") != 24:
            return iface["mac"]

    if os.name == "nt":
        out = _run(["getmac", "/fo", "csv", "/nh"], timeout=10)
        for line in out.splitlines():
            m = re.search(r"([0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})", line)
            if m:
                found = normalize_mac(m.group(1))
                if found:
                    return found

    node = uuid.getnode()
    if (node >> 40) & 0x01:  # multicast bit => not a real NIC
        return None
    return normalize_mac(f"{node:012x}")


# --------------------------------------------------------------------------
# connectivity
# --------------------------------------------------------------------------
def _matches(resp, expected) -> bool:
    if isinstance(expected, int):
        return resp.status == expected
    return expected in resp.text


def _looks_like_portal(location: str) -> bool:
    """A redirect that points at a private address is a captive portal.

    Public CDN/HTTPS upgrades (e.g. ``http://1.1.1.1`` -> ``https://1.1.1.1``)
    are *not* interception, so they should not flip us to "offline".
    """
    try:
        from urllib.parse import urlparse

        host = (urlparse(location).hostname or "").strip()
    except Exception:
        return False
    if not host:
        return False
    if host.startswith(("10.", "192.168.", "127.", "169.254.")):
        return True
    if host.startswith("172."):
        try:
            return 16 <= int(host.split(".")[1]) <= 31
        except (ValueError, IndexError):
            return False
    return False


def is_online(client, *, checks: Iterable = CHECK_URLS, min_ok: int = 1) -> bool:
    """True when at least ``min_ok`` probe endpoints answer as expected.

    Short-circuits on the first *success* (online) and on the first explicit
    captive-portal redirect (offline), so a disconnected machine does not have
    to wait out every remaining probe's timeout.
    """
    ok = 0
    for url, expected in checks:
        try:
            resp = client.get(url, follow=False, timeout=PROBE_TIMEOUT)
        except Exception:
            continue
        if _matches(resp, expected):
            ok += 1
            if ok >= min_ok:
                return True
            continue
        if resp.location and _looks_like_portal(resp.location):
            return False
        if resp.status in (301, 302, 303, 307, 308):
            return False
    return False


def detect_captive_redirect(client, *, checks: Iterable = CHECK_URLS):
    """Return ``(url, location)`` for the first probe that gets intercepted.

    The ``Location`` header of a captive portal is the authenticated-session
    entry point and usually carries the portal's session parameters.
    """
    for url, expected in checks:
        try:
            resp = client.get(url, follow=False, timeout=PROBE_TIMEOUT)
        except Exception:
            continue
        if _matches(resp, expected):
            continue
        if resp.location:
            return url, resp.location
        if resp.status in (301, 302, 303, 307, 308):
            return url, resp.location
        # Interception without a redirect (HTTP 200 carrying a login page).
        return url, None
    return None
