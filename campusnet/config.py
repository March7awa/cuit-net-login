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
    primary = default_config_path()
    if primary.exists():
        return primary
    # 主位置没有：只有「主目录根本进不去」才认为是之前存不下、搬到别处去了。
    # 目录好好的却什么都没有，那就是真的还没配过 —— 别拿旧文件糊弄用户。
    if not _dir_listable(primary.parent):
        for fallback in fallback_config_paths():
            if fallback.exists():
                return fallback
    return primary


def log_path() -> Path:
    """日志文件位置 —— 默认那个目录写不进就换一个。

    配置目录被坏 ACL 锁住的话，日志也写不进去；而 build_logger 里的
    RotatingFileHandler 打不开文件会直接把程序打死，连错误都看不到。
    """
    candidates = [user_config_dir()]
    candidates += [p.parent for p in fallback_config_paths()]
    for candidate in candidates:
        if _writable_dir(candidate) is not None:
            return candidate / "campus-login.log"
    return user_config_dir() / "campus-login.log"


def _writable_dir(candidate: Path) -> Path | None:
    """能往这个目录里写文件吗？能就返回它，不能返回 None。"""
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return candidate
    except OSError:
        return None


def fallback_config_paths() -> list[Path]:
    """%APPDATA% 那份配置读不了 / 写不进时的备选位置。

    别人电脑上最常见的一幕：老版本用 ``icacls /inheritance:r`` 把权限改坏，
    属主从此读不了自己的 config.json，程序每次启动都 Errno 13。修不好就
    换个地方写 —— 能用永远比「写在对的地方」重要。
    """
    out = [local_config_dir() / "config.json"]
    try:
        import tempfile

        out.append(Path(tempfile.gettempdir()) / APP_NAME / "config.json")
    except Exception:  # noqa: BLE001
        pass
    return out


def local_config_dir() -> Path:
    """%LOCALAPPDATA%（Windows）/ 等价的本地数据目录。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    return user_config_dir()


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
        #: 保存时被迫换了位置的话，这里是原来的路径（用来提醒用户）
        self.moved_from: Path | None = None
        #: 读原配置失败的原因（权限坏了之类），保存时会拿它做判断
        self.load_error: OSError | None = None

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
    def _write_to(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.raw, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, target)

    def save(self) -> None:
        """写配置。首选路径不行就修权限，还不行就换个地方写。

        绝不因为「写不到那个位置」让程序直接不能用 —— 别人的电脑上什么怪事
        都有：老版本留下的坏 ACL、同步盘、杀毒、只读的 Program Files。
        """
        tried: list[str] = []
        targets: list[Path] = []
        if self.load_error is None:
            targets.append(self.path)
        else:
            # 原来那份**读都读不了**，那就绝不往上写 —— 万一还能救回来呢，
            # 覆盖掉就等于把用户的账号设置抹了。直接换地方。
            tried.append(f"  {self.path}\n"
                         f"      （这份本来就读不了：{self.load_error}，不动它）")
        targets += [p for p in fallback_config_paths() if p not in targets]

        for target in targets:
            try:
                self._write_to(target)
            except OSError as exc:
                tried.append(f"  {target}\n      （{type(exc).__name__}: {exc}）")
                # 先试着把权限修回来（属主天然有权改 DACL），修好就还用原地
                if not secret.repair_permissions(str(target)):
                    continue
                try:
                    self._write_to(target)
                except OSError as exc2:
                    tried.append(f"  ↑ 修完权限还是不行（{exc2}）")
                    continue
            self._finish_save(target)
            return

        raise RuntimeError(
            "配置写不进去，这几个位置都试过了：\n" + "\n".join(tried) + "\n\n"
            "常见原因：\n"
            "  · 老版本把 config.json 的权限改坏了，当前账号读不了自己的文件\n"
            "  · 程序放在 C:\\Program Files 这类只读目录里\n"
            "  · 安全软件 / 同步盘把文件锁住了\n\n"
            "手动修一行命令即可（把路径换成上面报错的那个）：\n"
            '  icacls "<那个目录>" /grant "%USERNAME%":(OI)(CI)F /T'
        )

    def _finish_save(self, target: Path) -> None:
        if target != self.path:
            self.moved_from = self.path
            self.path = target
        secret.harden_permissions(str(target))

    def set_password(self, plain: str) -> None:
        self.raw["password"] = secret.protect(plain)

    def has_password(self) -> bool:
        value = self.raw.get("password")
        return bool(value)


def resolve_path_for_probe() -> Path:
    """给「探测」用的临时配置路径。

    故意**不**返回真实配置路径：探测只是拿一个临时 Config 去试网络，
    万一将来哪条代码路径顺手 save()，也不能把用户真配置覆盖掉。
    """
    import tempfile

    return Path(tempfile.gettempdir()) / "campus-net-probe" / "config.json"


def _dir_listable(directory: Path) -> bool:
    """能不能列出这个目录的内容（用来分清「真空」和「没权限看不见」）。

    目录**压根不存在**时返回 True —— 那只是还没配过，没什么可藏的，
    别把它当成权限问题（否则第一次运行就会被误判成「配置文件被锁住了」）。
    """
    try:
        os.listdir(directory)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _ensure_visible(path: Path) -> bool:
    """文件「看不见」时，分清是**真没有**还是**权限坏了看不见**。

    目录被坏 ACL 锁住的时候，目录本身还在，但里面的文件连 ``exists()``
    都返回 False，直接下结论就会误报成「找不到配置文件，先运行 init」
    —— 而用户明明配过。

    判据是**能不能列出目录内容**：没权限会直接抛错，真空目录只会返回 []。
    只有真的列不出来才去动权限（icacls 是个子进程，能不跑就不跑）。
    这里不建目录 —— 读操作不该有副作用，而且「父目录不存在」本来就
    等同于「还没配过」。
    """
    if path.exists():
        return True
    parent = path.parent
    if parent.is_dir() and not _dir_listable(parent):
        secret.repair_permissions(str(path))
    return path.exists()


def load(path: str | Path | None = None, *, must_exist: bool = True) -> Config:
    resolved = resolve_config_path(str(path) if path else None)
    if not _ensure_visible(resolved):
        if must_exist:
            raise FileNotFoundError(
                f"找不到配置文件: {resolved}\n"
                f"先运行:  python campus_login.py init"
            )
        cfg = Config(json.loads(json.dumps(DEFAULTS)), resolved)
        if not _dir_listable(resolved.parent):
            # 目录在、里面却什么都看不见（权限也没修好）—— 那里面可能真的
            # 躺着一份配置，只是我们看不到。标上 load_error，保存时会绕开它。
            # 注意别用 is_dir() 判断：权限被拒时它也会返回 False。
            cfg.load_error = PermissionError(f"看不见 {resolved.parent} 里的内容")
        return cfg


    try:
        # utf-8-sig 会顺手吃掉 BOM：Windows 记事本「另存为 UTF-8」会加 BOM，
        # 用户手改过 config.json 就会踩到，不能让 json 直接报错。
        data = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"配置文件不是合法 JSON ({resolved}): {exc}") from exc
    except OSError as exc:
        # 文件在、但读不了：老版本把 ACL 改坏留下的烂摊子（别人电脑上常见）。
        # 先试着把权限修回来；修不好就找找之前是不是已经搬到别处去了。
        if secret.repair_permissions(str(resolved)):
            try:
                data = json.loads(resolved.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                data = None
            if data is not None and isinstance(data, dict):
                return Config(_deep_merge(DEFAULTS, data), resolved)

        for fallback in fallback_config_paths():
            if fallback == resolved:
                continue
            try:
                data = json.loads(fallback.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                # 之前存不下的时候存到这儿了，接着用它 —— 开机自启的计划任务
                # 里把 --config 老路径写死了，靠这一步才能自己找回来。
                return Config(_deep_merge(DEFAULTS, data), fallback)

        if not must_exist:
            cfg = Config(json.loads(json.dumps(DEFAULTS)), resolved)
            cfg.load_error = exc
            return cfg
        raise RuntimeError(
            f"读不了配置文件：\n  {resolved}\n"
            f"（{type(exc).__name__}: {exc}）\n\n"
            "这个文件还在，只是当前账号没有读它的权限 —— 通常是老版本程序\n"
            "（用 icacls /inheritance:r 那一版）把权限改坏了。\n"
            "在「命令提示符」里跑这一行就能修回来（把路径换成上面那个）：\n"
            f'  icacls "{resolved.parent}" /grant "%USERNAME%":(OI)(CI)F /T'
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"配置文件顶层必须是对象: {resolved}")
    return Config(_deep_merge(DEFAULTS, data), resolved)


def write_example(path: str | Path) -> Path:
    target = Path(path)
    target.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
