#!/usr/bin/env python3
"""把项目清理成可以公开发布的状态。

会做四件事：

1. 删掉不该发出去的工作目录：``_recon/``（逆向过程产生的抓包、票据、
   截图）、所有 ``__pycache__/``；
2. 把**已入库文件**里的个人信息替换掉：学号/账号、本机 MAC、本机 IP、
   接入设备地址、CAS 票据；
3. 检查 git 历史里是否还残留这些值 —— 改文件是不会改历史的，
   所以提供 ``--reset-git`` 把历史压成一次干净提交；
4. 可选 ``--purge-installed`` 删掉本机 ``%APPDATA%`` 下存的配置和日志
   （注意：删了要重新填一次密码）。

用法::

    python tools/clean-for-sharing.py              # 先看会改什么（不动手）
    python tools/clean-for-sharing.py --apply      # 真的清理
    python tools/clean-for-sharing.py --apply --reset-git        # 连历史一起压干净
    python tools/clean-for-sharing.py --apply --purge-installed  # 连本机配置/日志一起删

给别人用：先 ``init`` 配好、用一阵子，想开源时跑一遍这个脚本即可。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: 替换成什么（看起来明显是假的，不会误导别人）
PLACEHOLDER = {
    "username": "2025000000",
    "mac": "001122334455",
    "mac_dashed": "00-11-22-33-44-55",
    "ip": "10.18.0.100",
    "nasip": "10.254.0.1",
    "ticket": "ST-EXAMPLE-TICKET",
}

#: 要抹掉的值**不写死在这里** —— 否则脚本自己就成了泄露源。
#: 一律从本机配置 / 网卡 / 命令行参数推导出来。

SKIP_DIRS = {"_recon", "__pycache__", ".git", ".venv", "venv", "node_modules"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".js", ".txt", ".sh", ".ps1", ".yml",
                 ".yaml", ".toml", ".cfg", ".ini", ".example", ".cmd", ".bat", ""}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          errors="replace", **kw)


def tracked_files() -> list[Path]:
    r = run(["git", "ls-files"])
    if r.returncode != 0:
        # 没装 git / 不是仓库：退化成遍历
        return [p for p in ROOT.rglob("*")
                if p.is_file() and not any(s in p.parts for s in SKIP_DIRS)]
    return [ROOT / f for f in r.stdout.split() if f.strip()]


#: 这些是"哨兵值"不是秘密 —— 曾经把 mac 的 "auto" 当成账号去替换，
#: 结果全项目的 auto 子串都被改掉（autostart -> 2025000000start）。
SKIP_VALUES = {"auto", "none", "skip", "true", "false", "system", ""}


def _classify(value: str) -> str:
    """判断这个值该换成什么占位符。"""
    if re.fullmatch(r"[0-9a-fA-F]{12}", value):
        return PLACEHOLDER["mac"]
    if re.fullmatch(r"[0-9a-fA-F]{2}(-[0-9a-fA-F]{2}){5}", value):
        return PLACEHOLDER["mac_dashed"]
    if re.fullmatch(r"[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}", value):
        return PLACEHOLDER["mac_dashed"].replace("-", ":")
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", value):
        return PLACEHOLDER["nasip"] if value.startswith("10.254.") else PLACEHOLDER["ip"]
    return PLACEHOLDER["username"]


def collect_targets(extra: list[str] | None = None) -> dict[str, str]:
    """要抹掉的值 -> 替换成什么。

    全部现推：配置里的账号/nasip/mac、本机当前的 IP 和 MAC，外加
    ``--redact`` 手工补充。脚本本身不含任何真实数据。
    """
    mapping: dict[str, str] = {}
    for value in extra or []:
        if value and value.strip().lower() not in SKIP_VALUES:
            mapping[value] = _classify(value)

    try:
        from campusnet import config as cfgmod

        cfg = cfgmod.load(must_exist=False)
        if cfg.username:
            mapping[cfg.username] = PLACEHOLDER["username"]
        nasip = str(cfg.option("nasip", "") or "")
        if nasip:
            mapping[nasip] = PLACEHOLDER["nasip"]
        cfg_mac = str(cfg.option("mac", "") or "")
        if cfg_mac and cfg_mac.strip().lower() not in SKIP_VALUES:
            mapping[cfg_mac] = _classify(cfg_mac)
    except Exception:
        pass
    try:
        from campusnet.netutil import default_local_ip, primary_mac

        ip = default_local_ip()
        if ip:
            mapping[ip] = PLACEHOLDER["ip"]
        mac = primary_mac()
        if mac:
            mapping[mac] = PLACEHOLDER["mac"]
    except Exception:
        pass

    # 去掉"自己替换自己"的条目
    return {k: v for k, v in mapping.items() if k and k != v}


def scan(files: list[Path], mapping: dict[str, str]) -> list[tuple[Path, dict[str, int]]]:
    found = []
    for p in files:
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        hits = {v: text.count(v) for v in mapping if v in text}
        if hits:
            found.append((p, hits))
    return found


TICKET_RE = re.compile(r"ST-[0-9A-Za-z\-]{12,}")


def redact(files: list[Path], mapping: dict[str, str]) -> list[tuple[Path, int]]:
    changed = []
    for p in files:
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = original = p.read_text(encoding="utf-8")
        except Exception:
            continue
        n = 0
        for value, repl in mapping.items():
            if value in text:
                n += text.count(value)
                text = text.replace(value, repl)
        text, k = TICKET_RE.subn(PLACEHOLDER["ticket"], text)
        n += k
        if text != original:
            p.write_text(text, encoding="utf-8")
            changed.append((p, n))
    return changed


def remove_junk(apply: bool) -> list[str]:
    removed = []
    for name in ("_recon",):
        p = ROOT / name
        if p.exists():
            size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            removed.append(f"{name}/ ({size/1048576:.1f} MB)")
            if apply:
                shutil.rmtree(p, ignore_errors=True)
    for p in list(ROOT.rglob("__pycache__")):
        if p.is_dir():
            removed.append(f"{p.relative_to(ROOT)}/")
            if apply:
                shutil.rmtree(p, ignore_errors=True)
    return removed


def history_leaks(mapping: dict[str, str]) -> int:
    """git 历史里还有多少条提交带着这些值。"""
    r = run(["git", "log", "--all", "-p", "--pretty=format:%h %s"])
    if r.returncode != 0:
        return 0
    return sum(r.stdout.count(v) for v in mapping)


def force_rmtree(path: Path) -> None:
    """删目录，连只读文件一起删。

    ``shutil.rmtree(ignore_errors=True)`` 会被 ``.git`` 里的只读 pack 文件
    挡住，而且**失败是静默的** —— 结果是备份目录没被清掉、下一步 move
    直接报「目标已存在」，重建历史悄悄失败。这里显式清掉只读属性。
    """
    def onerror(func, p, _exc):  # noqa: ANN001
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except Exception:
            pass

    shutil.rmtree(path, onerror=onerror)


def reset_git(apply: bool) -> bool:
    git = ROOT / ".git"
    if not git.exists():
        return False
    if not apply:
        return True
    backup = ROOT.parent / (ROOT.name + "-git-backup")
    if backup.exists():
        force_rmtree(backup)
    if backup.exists():  # 还是没删掉就别硬来，避免把历史弄丢
        raise RuntimeError(f"清不掉旧备份 {backup}，请手动删除后再试")
    shutil.move(str(git), str(backup))
    run(["git", "init", "-b", "main"])
    run(["git", "config", "user.name", "campus-net-login"])
    run(["git", "config", "user.email", "campus-net-login@users.noreply.github.com"])
    run(["git", "add", "-A"])
    run(["git", "commit", "-m",
         "chore: 首次提交（已清理个人信息）"])
    return True


def purge_installed(apply: bool) -> list[str]:
    from campusnet import config as cfgmod

    removed = []
    d = cfgmod.user_config_dir()
    if d.exists():
        for f in sorted(d.iterdir()):
            removed.append(str(f))
        if apply:
            shutil.rmtree(d, ignore_errors=True)
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="把项目清理成可公开发布的状态")
    ap.add_argument("--apply", action="store_true", help="真的执行（默认只看不动）")
    ap.add_argument("--reset-git", action="store_true",
                    help="重建 git 历史（原 .git 会备份到同级目录）")
    ap.add_argument("--purge-installed", action="store_true",
                    help="同时删除本机的配置和日志（要重新填密码）")
    ap.add_argument("--redact", action="append", metavar="VALUE", default=[],
                    help="额外要抹掉的值，可重复（配置里已不存在、只剩在文件里的历史值用这个）")
    args = ap.parse_args()

    mode = "执行清理" if args.apply else "试运行（不会改动任何东西）"
    print("=" * 74)
    print(f"campus-net-login 发布前清理 —— {mode}")
    print(f"项目目录: {ROOT}")
    print("=" * 74)

    mapping = collect_targets()
    if mapping:
        print("\n【要抹掉的值】")
        for k, v in sorted(mapping.items()):
            print(f"    {k!r:26} -> {v!r}")

    files = tracked_files()

    print("\n【A】已入库文件里的个人信息")
    hits = scan(files, mapping)
    if hits:
        for p, h in hits:
            detail = ", ".join(f"{k}({n})" for k, n in h.items())
            print(f"    {p.relative_to(ROOT)}  ->  {detail}")
        print(f"    共 {len(hits)} 个文件")
    else:
        print("    干净 ✅")

    print("\n【B】不该发出去的工作目录")
    junk = remove_junk(args.apply)
    print("    " + ("\n    ".join(junk) if junk else "无 ✅"))

    n_hist = history_leaks(mapping)
    print("\n【C】git 历史残留")
    if n_hist:
        print(f"    历史里仍有 {n_hist} 处敏感值。**只改文件是清不掉历史的**，")
        print("    发布前建议加 --reset-git（会用一次干净提交重建，原 .git 自动备份）。")
    else:
        print("    干净 ✅")

    if args.purge_installed:
        print("\n【D】本机配置与日志")
        inst = purge_installed(args.apply)
        for f in inst:
            print(f"    {f}")
        if not inst:
            print("    无")

    if args.apply:
        changed = redact(files, mapping)
        print("\n【A 已执行】改写文件:")
        for p, n in changed:
            print(f"    {p.relative_to(ROOT)}  ({n} 处)")
        if not changed:
            print("    无改动")

        if args.reset_git:
            print("\n【C 已执行】重建 git 历史")
            if reset_git(True):
                print("    完成：现在是单次提交；原 .git 备份在同级 *-git-backup 目录")
            else:
                print("    跳过：没有 .git")

        print("\n" + "=" * 74)
        print("清理完成。建议再跑一次不带 --apply 的检查确认：")
        print(f'    "{sys.executable}" "{Path(__file__).resolve()}"')
        print("=" * 74)
    else:
        print("\n" + "=" * 74)
        print("以上都是预览，没有改动任何文件。")
        print("确认无误后执行：")
        print(f'    "{sys.executable}" "{Path(__file__).resolve()}" --apply')
        print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
