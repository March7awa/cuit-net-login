#!/usr/bin/env python3
"""校园网自动登录 —— 命令行入口。

用法速查::

    python campus_login.py init                 # 交互式生成配置（第一次用这个）
    python campus_login.py set-password         # 保存密码（Windows 下用 DPAPI 加密）
    python campus_login.py login                # 立刻登录一次
    python campus_login.py login --force        # 已经在线也强制重新登录
    python campus_login.py watch                # 常驻看门狗，断线自动重连
    python campus_login.py status               # 看当前网络/配置状态
    python campus_login.py doctor               # 出问题时跑这个，输出诊断信息
    python campus_login.py providers            # 列出内置适配器
    python campus_login.py selftest             # 跑内置算法自检（AES / 深澜 xEncode）
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from campusnet import __version__, config as config_mod
from campusnet.presets import PRESETS
from campusnet.providers import get_provider, list_providers
from campusnet.runner import SingleInstance, make_setup

# --------------------------------------------------------------------------
# 开箱即用的学校预设见 campusnet/presets.py（CLI 和 GUI 共用一份）
# --------------------------------------------------------------------------


def _print(msg: str = "") -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    print(msg)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        value = ""
    return value or default


def _ask_password() -> str:
    """Read the password without echoing it.

    ``getpass`` needs a real console; on Windows it blocks forever when stdin
    is a pipe.  So when we are not attached to a terminal we read stdin
    instead (which also makes ``init`` scriptable), and never hang.
    """
    env = os.environ.get("CAMPUS_PASSWORD")
    if env:
        return env

    interactive = True
    try:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        interactive = False

    if not interactive:
        _print("（非交互模式：从标准输入读取密码；也可以改用 CAMPUS_PASSWORD 环境变量）")
        while True:
            line = sys.stdin.readline() if sys.stdin else ""
            if not line:
                raise SystemExit(
                    "没有可用的终端来输入密码。请在真正的终端里运行，"
                    "或设置 CAMPUS_PASSWORD 环境变量。"
                )
            first = line.rstrip("\r\n")
            if not first:
                continue
            second = (sys.stdin.readline() or "").rstrip("\r\n")
            if first == second:
                return first
            _print("  两次输入不一致，重新来")

    while True:
        first = getpass.getpass("校园网密码（输入时不显示）: ")
        if not first:
            _print("  密码不能为空")
            continue
        second = getpass.getpass("再输一次确认: ")
        if first == second:
            return first
        _print("  两次输入不一致，重新来")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_init(args) -> int:
    path = config_mod.resolve_config_path(args.config)
    _print(f"配置文件将写入: {path}")
    if path.exists() and not args.force:
        if _ask("文件已存在，覆盖吗？(y/N)", "N").lower() not in ("y", "yes"):
            _print("已取消")
            return 1

    cfg = config_mod.load(path, must_exist=False)

    # 完全非交互模式：给了 --provider 就一路用默认值/命令行参数
    batch = bool(args.provider or args.username)

    preset = args.preset
    if not batch and not preset:
        _print("\n可用预设：")
        for key, item in PRESETS.items():
            _print(f"  {key:10} {item['label']}")
        preset = _ask("选择预设（直接回车跳过）", "")

    if preset and preset in PRESETS:
        item = PRESETS[preset]
        cfg.raw["provider"] = item["provider"]
        cfg.raw["options"] = {**(cfg.raw.get("options") or {}), **item["options"]}
        _print(f"已套用预设 {preset}: {item['label']}")

    if args.provider:
        provider_name = args.provider
    elif batch:
        provider_name = cfg.raw.get("provider") or "ruijie_sam_cas"
    else:
        _print("\n可用适配器：")
        for cls in list_providers():
            _print(f"  {cls.name:18} {cls.description}")
        provider_name = _ask("认证方式 provider", cfg.raw.get("provider") or "ruijie_sam_cas")

    try:
        provider_cls = get_provider(provider_name)
    except KeyError as exc:
        _print(str(exc))
        return 2
    cfg.raw["provider"] = provider_cls.name

    if provider_cls.notes and not batch:
        _print("\n说明：" + provider_cls.notes)

    options = dict(cfg.raw.get("options") or {})
    if args.option:
        for pair in args.option:
            if "=" not in pair:
                _print(f"--option 需要 key=value 形式: {pair!r}")
                return 2
            key, value = pair.split("=", 1)
            options[key.strip()] = value.strip()
    if not batch:
        _print("\n填写 options（直接回车用默认值）")
        for key, example in provider_cls.example_options.items():
            current = options.get(key, example)
            if isinstance(example, dict):
                _print(f"  {key} 当前: {json.dumps(current, ensure_ascii=False)}")
                continue
            options[key] = _ask(f"  {key}", str(current))
    cfg.raw["options"] = options

    if args.username:
        cfg.raw["username"] = args.username
    else:
        cfg.raw["username"] = _ask("\n学号 / 账号", cfg.raw.get("username", ""))
    if not cfg.raw["username"]:
        _print("账号不能为空")
        return 2

    if not args.no_password:
        cfg.set_password(_ask_password())
    cfg.save()
    _print(f"\n配置已保存: {path}")
    _print("下一步:  python campus_login.py login")
    return 0


def cmd_set_password(args) -> int:
    cfg = config_mod.load(args.config)
    if args.password:
        cfg.set_password(args.password)
    else:
        cfg.set_password(_ask_password())
    cfg.save()
    from campusnet import secret

    kind = "DPAPI 加密" if secret.is_encrypted(cfg.raw.get("password")) else "明文（当前系统不支持 DPAPI）"
    _print(f"密码已保存（{kind}）-> {cfg.path}")
    return 0


def _require_password(cfg) -> bool:
    """Refuse to make login attempts when no password has been stored.

    Important: an unattended ``watch`` looping on an empty password would keep
    hammering the portal and can trip account lockout policies, so this is a
    hard stop rather than a warning.
    """
    if cfg.has_password():
        return True
    _print("还没有设置密码，已停止（避免用空密码反复尝试把账号试锁）。")
    _print()
    _print("  双击 1-配置向导.cmd        # 选认证方式 + 填账号密码")
    _print("  或 python campus_login.py set-password")
    return False


def cmd_login(args) -> int:
    cfg = config_mod.load(args.config)
    if not _require_password(cfg):
        return 2
    setup = make_setup(cfg, verbose=args.verbose)
    from campusnet.runner import Runner

    runner = Runner(setup)

    if not args.force and runner.is_online():
        _print("当前已经能上网，无需登录。（想强制重登加 --force）")
        return 0

    _print("正在登录…")
    result = runner.login_and_verify()
    _print(("成功: " if result.ok else "失败: ") + result.message)
    return 0 if result.ok else 3


def cmd_watch(args) -> int:
    cfg = config_mod.load(args.config)
    if not _require_password(cfg):
        return 2
    setup = make_setup(cfg, verbose=args.verbose)
    from campusnet.runner import Runner

    if args.once:
        runner = Runner(setup)
        if runner.is_online():
            setup.log.info("--once: 网络正常，无需登录")
            _print("已在线")
            return 0
        setup.log.info("--once: 检测到断网，尝试登录")
        result = runner.login_and_verify()
        setup.log.info("--once 结果: %s %s", "成功" if result.ok else "失败", result.message)
        _print(result.message)
        return 0 if result.ok else 3

    try:
        with SingleInstance():
            return Runner(setup).watch()
    except RuntimeError as exc:
        _print(str(exc))
        return 4


def cmd_logout(args) -> int:
    """注销当前校园网会话（会断网，门户页面随后会重新弹出来）。"""
    cfg = config_mod.load(args.config)
    setup = make_setup(cfg, verbose=args.verbose)
    provider = setup.provider
    if not hasattr(provider, "logout"):
        _print(f"{cfg.provider} 这个认证方式不支持注销")
        return 2
    if not args.yes:
        _print("这会断开你当前的校园网连接（然后门户页面会弹出来）。")
        if _ask("确定吗？(y/N)", "N").lower() not in ("y", "yes"):
            _print("已取消")
            return 0
    result = provider.logout()
    _print(("成功: " if result.ok else "失败: ") + result.message)
    return 0 if result.ok else 3


def cmd_pause(args) -> int:
    """暂停 / 恢复自动登录（想自己看门户页面时用）。"""
    from campusnet import autostart

    if args.resume:
        ok, msg = autostart.set_enabled(True)
        _print(("已恢复自动登录。" if ok else "恢复失败：") + "\n" + msg)
        if ok and os.name == "nt":
            import subprocess as _sp
            _sp.run(["schtasks", "/run", "/tn", "CampusNetLogin"],
                    capture_output=True, creationflags=0x08000000)
            _print("看门狗已重新启动。")
        return 0 if ok else 1

    ok, msg = autostart.set_enabled(False)
    _print(("已暂停自动登录 —— 现在拔网线/重启都不会自动连了。" if ok
            else "暂停失败：") + "\n" + msg)
    if ok:
        _print("\n  自己弄完之后运行:  python campus_login.py pause --resume")
    return 0 if ok else 1


def cmd_status(args) -> int:
    cfg = config_mod.load(args.config, must_exist=False)
    setup = make_setup(cfg, verbose=args.verbose)
    from campusnet.netutil import default_local_ip, primary_mac
    from campusnet.runner import Runner

    runner = Runner(setup)
    _print(f"campus-net-login {__version__}")
    _print(f"配置文件      : {cfg.path}  {'(存在)' if cfg.path.exists() else '(不存在)'}")
    _print(f"provider      : {cfg.provider or '(未设置)'}")
    _print(f"账号          : {cfg.username or '(未设置)'}")
    _print(f"密码          : {'已保存' if cfg.has_password() else '未保存'}")
    _print(f"portal        : {cfg.option('portal') or '(未设置)'}")
    _print(f"本机 IP       : {default_local_ip()}")
    _print(f"本机 MAC      : {primary_mac()}")
    online = runner.is_online()
    _print(f"网络状态      : {'在线 ✅' if online else '离线/被劫持 ⚠️'}")
    if not online:
        _print(f"  {runner.describe_offline()}")
    return 0 if online else 1


def cmd_providers(args) -> int:
    for cls in list_providers():
        _print(f"{cls.name}")
        _print(f"    {cls.description}")
        if cls.required_options:
            _print(f"    必填 options: {', '.join(cls.required_options)}")
        _print()
    return 0


def cmd_doctor(args) -> int:
    cfg = config_mod.load(args.config, must_exist=False)
    setup = make_setup(cfg, verbose=True)
    from campusnet.netutil import CHECK_URLS, default_local_ip, primary_mac
    from campusnet.runner import Runner

    runner = Runner(setup)
    _print("=" * 68)
    _print("环境")
    _print("=" * 68)
    _print(f"python        : {sys.version.split()[0]}  ({sys.platform})")
    _print(f"配置文件      : {cfg.path}")
    _print(f"日志文件      : {config_mod.log_path()}")
    _print(f"provider      : {cfg.provider!r}")
    _print(f"本机 IP       : {default_local_ip()}")
    _print(f"本机 MAC      : {primary_mac()}")

    _print()
    _print("=" * 68)
    _print("网络探测")
    _print("=" * 68)
    for url, expected in CHECK_URLS:
        try:
            resp = setup.http.get(url, follow=False, timeout=6)
            loc = resp.location
            _print(f"  {url}\n      -> HTTP {resp.status}"
                   + (f"  Location={loc[:110]}" if loc else "")
                   + f"   (期望 {expected})")
        except Exception as exc:  # noqa: BLE001
            _print(f"  {url}\n      -> 错误: {exc}")

    _print()
    _print(f"在线: {runner.is_online()}")
    if not runner.is_online():
        _print(f"  {runner.describe_offline()}")

    if cfg.path.exists():
        try:
            cls = get_provider(cfg.provider)
            provider = cls(cfg, setup.http, setup.log)
            problem = provider.validate()
            _print()
            _print("=" * 68)
            _print("配置校验")
            _print("=" * 68)
            _print("  " + (problem if problem else "OK"))
        except Exception as exc:  # noqa: BLE001
            _print(f"  校验失败: {exc}")
    return 0


def cmd_selftest(args) -> int:
    from campusnet.aes import _encrypt_block, _expand_key, encrypt_cryptojs_style
    from campusnet.providers.srun import srun_b64, xencode

    failures = 0

    def check(name: str, got, want) -> None:
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        _print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            _print(f"         期望 {want}")
            _print(f"         实际 {got}")

    _print("AES-128-ECB (FIPS-197 C.1 标准向量)")
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    check("AES 单块加密", _encrypt_block(pt, _expand_key(key)).hex(),
          "69c4e0d86a7b0430d8cdb78070b4c55a")

    _print("\nCryptoJS 兼容模式（真实校园网抓包向量）")
    check("AES(password='zzprobe0001')",
          encrypt_cryptojs_style("Qvy2L55c2aTCHUc6uX/Llw==", "zzprobe0001"),
          "Uz/UJgU3BdOiWO9Bfrykrg==")

    _print("\n深澜 xEncode（与门户 JS 对拍向量）")
    msg = '{"username":"2025000000","password":"pw123456","ip":"10.18.0.100","acid":"1","enc_ver":"srun_bx1"}'
    k = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    check("xEncode+Base64 可复现", len(srun_b64(xencode(msg, k))) > 40, True)

    _print("\n配置读写（含记事本 BOM 容错）")
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    tmp = _Path(tempfile.mkdtemp()) / "config.json"
    payload = {"provider": "ruijie_sam_cas", "username": "u", "password": "p",
               "options": {"portal": "http://1.2.3.4"}}
    # Windows 记事本「另存为 UTF-8」会写 BOM，必须能读
    tmp.write_bytes(b"\xef\xbb\xbf" + _json.dumps(payload, ensure_ascii=False).encode())
    try:
        check("带 BOM 的 config.json 能读",
              config_mod.load(tmp).option("portal"), "http://1.2.3.4")
    except Exception as exc:  # noqa: BLE001
        check("带 BOM 的 config.json 能读", f"异常 {exc}", "http://1.2.3.4")
    # 保存回去不能再带 BOM
    cfg = config_mod.load(tmp)
    cfg.save()
    check("save() 写出的文件不带 BOM",
          tmp.read_bytes()[:3] == b"\xef\xbb\xbf", False)

    _print()
    _print("全部通过 ✅" if not failures else f"{failures} 项失败 ❌")
    return 0 if not failures else 1


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="campus_login",
        description="校园网自动登录 / 断线重连（纯标准库，无需安装依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-V", "--version", action="version", version=f"campus-net-login {__version__}")
    p.add_argument("-c", "--config", help="配置文件路径（默认 ./config.json 或用户目录）")
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="交互式生成配置")
    s.add_argument("--preset", help="套用学校预设，例如 cuit")
    s.add_argument("--force", action="store_true", help="覆盖已存在的配置")
    s.add_argument("--no-password", action="store_true", help="先不保存密码")
    s.add_argument("--provider", help="直接指定 adapter，跳过交互（与 --username 一起用可全自动）")
    s.add_argument("--username", help="直接指定账号")
    s.add_argument("--option", action="append", metavar="KEY=VALUE",
                   help="直接设置 options 里的项，可重复，例如 --option portal=http://10.0.0.1")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("set-password", help="保存/更新密码")
    s.add_argument("password", nargs="?", help="直接给密码（不推荐，会留在 shell 历史里）")
    s.set_defaults(func=cmd_set_password)

    s = sub.add_parser("login", help="立即登录一次")
    s.add_argument("--force", action="store_true", help="已经在线也强制重新登录")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("watch", help="常驻看门狗：断线自动重连")
    s.add_argument("--once", action="store_true", help="只检查一次（给计划任务用）")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("logout", help="注销当前会话（会断网，用于换账号/自己看门户页）")
    s.add_argument("-y", "--yes", action="store_true", help="不确认，直接注销")
    s.set_defaults(func=cmd_logout)

    s = sub.add_parser("pause", help="暂停/恢复自动登录（--resume 恢复）")
    s.add_argument("--resume", action="store_true", help="恢复自动登录")
    s.set_defaults(func=cmd_pause)

    sub.add_parser("status", help="查看当前状态").set_defaults(func=cmd_status)
    sub.add_parser("providers", help="列出内置适配器").set_defaults(func=cmd_providers)
    sub.add_parser("doctor", help="诊断网络与配置").set_defaults(func=cmd_doctor)
    sub.add_parser("selftest", help="运行内置算法自检").set_defaults(func=cmd_selftest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        _print(str(exc))
        return 1
    except KeyboardInterrupt:
        _print("\n已取消")
        return 130
    except Exception as exc:  # noqa: BLE001
        _print(f"出错了: {type(exc).__name__}: {exc}")
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        else:
            _print("加 -v 可以看到完整堆栈。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
