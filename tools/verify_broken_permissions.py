"""模拟别人那台机器的故障，确认程序能自救。

故障现场（别人电脑上截图里的那个）：
    [Errno 13] Permission denied:
    'C:\\Users\\<用户名>\\AppData\\Roaming\\campus-net-login\\config.json'
—— 读的时候就失败了，说明文件在、但当前账号读不了它。

三种坏情况各测一遍：

  A. 继承权限被删掉（老版本 ``icacls /inheritance:r`` 干的事）
     -> 应该能**就地修好**，用户的配置一点不丢
  B. 首选位置根本用不了（这里用「config.json 是个目录」来模拟）
     -> 应该**换个地方写**，而且下次启动还能自己找回来
  C. 配置目录进不去时，日志不能跟着写不进去
     （build_logger 打不开日志文件会直接把程序打死，连错都看不到）
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from campusnet import config as config_mod  # noqa: E402

failures: list[str] = []
USER = os.environ.get("USERNAME", "")


def check(label: str, ok: bool, extra: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + extra) if extra else ''}")
    if not ok:
        failures.append(label)


def _icacls(*args: str) -> None:
    subprocess.run(f'icacls {" ".join(args)}', shell=True, capture_output=True)


def lock_inheritance(path: Path) -> None:
    """复现老版本干的事：删掉继承权限，再把当前用户自己也摘掉。"""
    _icacls(f'"{path}"', "/inheritance:r", f'/remove:g "{USER}"')


def unlock(path: Path) -> None:
    _icacls(f'"{path}"', f'/grant "{USER}":(OI)(CI)F /T')


def can_read(path: Path) -> bool:
    try:
        path.read_text(encoding="utf-8-sig")
        return True
    except OSError:
        return False


def main() -> int:
    fake = Path(os.environ["TEMP"]) / "broken-machine"
    shutil.rmtree(fake, ignore_errors=True)
    appdata = fake / "Roaming" / "campus-net-login"
    local = fake / "Local" / "campus-net-login"
    appdata.mkdir(parents=True)
    local.mkdir(parents=True)

    old = {k: os.environ.get(k) for k in ("APPDATA", "LOCALAPPDATA")}
    os.environ["APPDATA"] = str(fake / "Roaming")
    os.environ["LOCALAPPDATA"] = str(fake / "Local")

    victim = appdata / "config.json"
    victim.write_text(json.dumps({"provider": "ruijie_sam_cas",
                                  "username": "2099000001",
                                  "options": {"portal": ""}},
                                 ensure_ascii=False), encoding="utf-8")

    print("A. 继承权限被删掉 —— 就是截图里那个 Errno 13")
    lock_inheritance(appdata)
    check("现场造好了：这份 config.json 读不了了", not can_read(victim))
    try:
        cfg = config_mod.load(must_exist=True)
        check("load() 没崩", True, f"用的是 {cfg.path}")
        check("读回了原来的账号，配置一点没丢",
              cfg.username == "2099000001", cfg.username)
        check("权限被就地修好，不用搬家", can_read(victim))
    except Exception as exc:  # noqa: BLE001
        check("load() 没崩", False, str(exc)[:150])

    unlock(fake)

    print("\nB. 首选位置彻底用不了时，换个地方写、并且下次还找得到")
    victim.unlink(missing_ok=True)
    victim.mkdir()          # 把 config.json 变成一个目录，怎么写都写不进去
    print(f"   （把 {victim.name} 做成了目录来模拟「怎么写都失败」）")
    try:
        cfg2 = config_mod.load(must_exist=False)
        cfg2.raw["username"] = "2099000002"
        cfg2.set_password("别人的密码")
        cfg2.save()
        check("save() 没崩", True, f"存到了 {cfg2.path}")
        check("换了地方存", cfg2.path.parent == local, str(cfg2.path))
        via_old = config_mod.load(str(victim), must_exist=True)
        check("下次按旧路径启动也能自己找回来",
              via_old.username == "2099000002", f"实际用 {via_old.path}")
        check("密码也在", via_old.password() == "别人的密码")
    except Exception as exc:  # noqa: BLE001
        check("save() 没崩", False, str(exc)[:200])

    print("\nC. 配置目录进不去时，日志也得写得出来")
    shutil.rmtree(victim, ignore_errors=True)
    unlock(fake)
    lock_inheritance(appdata)
    lp = config_mod.log_path()
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text("probe\n", encoding="utf-8")
        check("日志换到了能写的地方", lp.parent != appdata, str(lp))
    except OSError as exc:
        check("日志换到了能写的地方", False, str(exc)[:150])

    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    unlock(fake)
    shutil.rmtree(fake, ignore_errors=True)

    print()
    if failures:
        print(f"有 {len(failures)} 项没过 ❌")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
