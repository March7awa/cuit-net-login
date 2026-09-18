"""Configuration loading, discovery and persistence."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from . import secret

APP_NAME = "campus-net-login"

DEFAULTS: dict[str, Any] = {
    "provider": "ruijie_sam_cas",
    "username": "",
    "password": "",
    "options": {
        "portal": "",
        "mac": "auto",
    },
    "watch": {
        "interval": 20,
        "retry_interval": 5,
        "cooldown_after_login": 10,
        "check_min_ok": 1,
        # 登录成功后最多等这么久（秒），让 AC/RADIUS 真正把网放行。
        # 有些学校要等 1-3 分钟才生效；等太短会误判成失败并反复重登，
        # 而每次重登都会新建会话，反而可能把正在下发的授权打断。
        "online_wait_seconds": 120,
        # 开机时网络还没就绪的重试间隔（秒）
        "network_retry_seconds": 5,
        # 登录成功但迟迟不通网时，再次尝试登录前至少等这么久（秒）
        "relogin_delay_seconds": 60,
        "notify_on_first_failure": False,
    },
    "logging": {
        "level": "INFO",
        "file": True,
        "max_bytes": 1_048_576,
        "backups": 3,
    },
}


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------
def user_config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def default_config_path() -> Path:
    """Prefer a config next to the program, else the per-user directory."""
    local = Path.cwd() / "config.json"
    if local.exists():
        return local
    return user_config_dir() / "config.json"


def resolve_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("CAMPUS_LOGIN_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return default_config_path()


def log_path() -> Path:
    return user_config_dir() / "campus-login.log"


# --------------------------------------------------------------------------
# load / save
# --------------------------------------------------------------------------
def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, data: dict[str, Any], path: Path) -> None:
        self.raw = data
        self.path = path

    # -- access ------------------------------------------------------------
    @property
    def provider(self) -> str:
        return str(self.raw.get("provider", "")).strip()

    @property
    def username(self) -> str:
        return str(self.raw.get("username", ""))

    def password(self) -> str:
        try:
            return secret.unprotect(self.raw.get("password", ""))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "无法解密已保存的密码（DPAPI 密文只能被保存它的同一台机器的同一用户解开）。"
                " 请重新运行 set-password。原始错误: %s" % exc
            ) from exc

    def option(self, key: str, default: Any = None) -> Any:
        return (self.raw.get("options") or {}).get(key, default)

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.raw.get(name) or {})

    # -- persistence -------------------------------------------------------
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.raw, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        secret.harden_permissions(str(self.path))

    def set_password(self, plain: str) -> None:
        self.raw["password"] = secret.protect(plain)

    def has_password(self) -> bool:
        value = self.raw.get("password")
        return bool(value)


def load(path: str | Path | None = None, *, must_exist: bool = True) -> Config:
    resolved = resolve_config_path(str(path) if path else None)
    if not resolved.exists():
        if must_exist:
            raise FileNotFoundError(
                f"找不到配置文件: {resolved}\n"
                f"先运行:  python campus_login.py init"
            )
        return Config(json.loads(json.dumps(DEFAULTS)), resolved)
    try:
        # utf-8-sig 会顺手吃掉 BOM：Windows 记事本「另存为 UTF-8」会加 BOM，
        # 用户手改过 config.json 就会踩到，不能让 json 直接报错。
        data = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"配置文件不是合法 JSON ({resolved}): {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"配置文件顶层必须是对象: {resolved}")
    return Config(_deep_merge(DEFAULTS, data), resolved)


def write_example(path: str | Path) -> Path:
    target = Path(path)
    target.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
