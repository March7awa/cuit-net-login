"""Connectivity checking, login orchestration and the watchdog loop."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from . import config as config_mod
from .httpx import HttpClient, HttpError, sanitize_url
from .netutil import CHECK_URLS, detect_captive_redirect, is_online, link_up
from .providers import LoginResult, get_provider

__all__ = ["Setup", "build_logger", "SingleInstance", "Runner"]

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 在线时每隔这么久往日志里写一条心跳，证明看门狗还活着、还在查
ONLINE_LOG_SECONDS = 300.0


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------
def build_logger(cfg, *, verbose: bool = False, console: bool = True) -> logging.Logger:
    logger = logging.getLogger("campusnet")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    level = logging.DEBUG if verbose else getattr(
        logging, str(cfg.section("logging").get("level", "INFO")).upper(), logging.INFO
    )

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    if console:
        # Windows consoles default to a legacy code page and mangle Chinese,
        # so force UTF-8 where the stream allows it.
        stream = sys.stdout
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
        handler = logging.StreamHandler(stream)
        handler.setLevel(level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    log_cfg = cfg.section("logging")
    if log_cfg.get("file", True):
        path = config_mod.log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        size = int(log_cfg.get("max_bytes", 1_048_576))
        backups = int(log_cfg.get("backups", 3))
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=size, backupCount=backups, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG if verbose else level)
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger


# --------------------------------------------------------------------------
# single instance
# --------------------------------------------------------------------------
#: Windows 上锁的字节数 —— 必须覆盖锁文件里可能出现的任何偏移量
_LOCK_BYTES = 1024


class SingleInstance:
    """Cross-platform advisory lock so two watchdogs never fight each other.

    另外单独写一个 ``watch.heartbeat`` 文件当心跳 —— 不能写进锁文件本身：
    Windows 的字节锁连**读**都会挡住别的进程，``--once`` 根本读不到内容。
    心跳是给 ``watch --once`` 判断「常驻看门狗是活着还是卡死了」用的：
    活着就别插手（两个进程同时重登会各自新建会话，把 AC 正在下发的授权
    打断），卡死了才接管。
    """

    HEARTBEAT_STALE_SECONDS = 180

    def __init__(self, name: str = "watch.lock") -> None:
        self.path = config_mod.user_config_dir() / name
        self.heartbeat_path = self.path.with_name(self.path.name + ".heartbeat")
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            # 一定要先 seek(0)，而且锁一整个区间而不是 1 个字节：
            # msvcrt 锁的是「当前文件位置起 N 个字节」，追加模式下位置在
            # 文件末尾，两个进程会各自锁到不同的字节上，于是双双「加锁成功」。
            # 锁 0..1023 能覆盖任何写进锁文件的字节偏移，跟老版本也重叠。
            self._fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, _LOCK_BYTES)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._write_heartbeat()
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def _write_heartbeat(self) -> None:
        """心跳写到独立文件（锁文件本身读不了）。失败无所谓。"""
        try:
            self.heartbeat_path.write_text(
                f"{os.getpid()} {int(time.time())}", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def touch(self) -> None:
        """告诉别人「我还活着」。看门狗每轮调一次。"""
        if self._fh:
            self._write_heartbeat()

    def heartbeat(self) -> tuple[int | None, float | None]:
        """``(pid, 心跳距今秒数)``；读不出来就是 ``(None, None)``。"""
        try:
            parts = self.heartbeat_path.read_text(
                encoding="utf-8", errors="replace").split()
            return int(parts[0]), max(0.0, time.time() - int(parts[1]))
        except Exception:  # noqa: BLE001
            return None, None

    def is_stale(self) -> bool:
        """持锁的那个进程是不是已经卡死了。

        心跳文件**读不到**时返回 False（当成对方还活着）：宁可不插手 ——
        两个进程同时重登会各自新建会话，把 AC 正在下发的授权打断。
        只有明明有心跳、但它已经过期很久，才认定对方卡死并接管。
        """
        pid, age = self.heartbeat()
        return (pid is not None and age is not None
                and age > self.HEARTBEAT_STALE_SECONDS)

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTES)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            self._fh.close()
            self._fh = None
            try:
                self.heartbeat_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise RuntimeError("已经有一个 campus_login watch 在运行了")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# --------------------------------------------------------------------------
# setup bundle
# --------------------------------------------------------------------------
class Setup:
    """Everything a command needs: config, logger, http client, provider."""

    def __init__(self, cfg, log, http: HttpClient) -> None:
        self.cfg = cfg
        self.log = log
        self.http = http
        self.provider = get_provider(cfg.provider)(cfg, http, log)


def make_setup(cfg, *, verbose: bool = False, console: bool = True,
               timeout: float = 15.0) -> Setup:
    log = build_logger(cfg, verbose=verbose, console=console)
    http = HttpClient(timeout=timeout)
    return Setup(cfg, log, http)


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
class Runner:
    def __init__(self, setup: Setup) -> None:
        self.setup = setup
        self.cfg = setup.cfg
        self.log = setup.log
        self.http = setup.http

    # -- connectivity ------------------------------------------------------
    def checks(self):
        extra = self.cfg.option("check_urls")
        if extra:
            return [(u, 204) for u in extra]
        return CHECK_URLS

    def is_online(self) -> bool:
        min_ok = int(self.cfg.section("watch").get("check_min_ok", 1))
        return is_online(self.http, checks=self.checks(), min_ok=min_ok)

    def describe_offline(self) -> str:
        hit = detect_captive_redirect(self.http, checks=self.checks())
        if not hit:
            return "网络不通（探测点全部失败，可能是没插网线 / 没连 Wi-Fi）"
        url, loc = hit
        if loc:
            return f"被门户劫持: {url} -> {sanitize_url(loc)[:160]}"
        return f"被门户劫持: {url}（HTTP 响应不是预期的内容）"

    # -- login -------------------------------------------------------------
    def login(self) -> LoginResult:
        problem = self.setup.provider.validate()
        if problem:
            return LoginResult(False, f"配置有误: {problem}")
        try:
            return self.setup.provider.login()
        except HttpError as exc:
            return LoginResult(False, f"网络错误: {exc}")
        except Exception as exc:  # noqa: BLE001
            self.log.debug("登录异常", exc_info=True)
            return LoginResult(False, f"{type(exc).__name__}: {exc}")

    def login_and_verify(self, *, wait: float | None = None) -> LoginResult:
        # 认证通过 ≠ 立刻能上网：AC 下发授权可能要 1-2 分钟。默认等久一点，
        # 否则会误报失败并诱导用户反复重登。
        if wait is None:
            wait = float(self.cfg.section("watch").get("online_wait_seconds", 120))
        result = self.login()
        if not result.ok:
            return result
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.is_online():
                return LoginResult(True, result.message + "（已验证可上网）", result.detail)
            time.sleep(2.0)
        if self.is_online():
            return LoginResult(True, result.message, result.detail)
        return LoginResult(
            False,
            f"认证已通过，但 {int(wait)} 秒内网络还没放行。"
            "这通常不是密码问题，而是门户那边还缺一步（比如选运营商/选套餐），"
            "或者 AC 下发慢。先别急着反复重登 —— 重登会新建会话，可能越弄越糟。",
            result.detail)

    # -- watchdog ----------------------------------------------------------
    @staticmethod
    def _is_network_problem(message: str) -> bool:
        """区分「连不上认证服务器」和「账号密码不对」。

        开机后网卡/路由要几十秒才就绪，这期间登录必然失败。但它**不是一次
        认证失败**，不该按指数退避去惩罚（那会白等 40 秒，而这 40 秒里
        Windows 检测不到网络，就会自己弹出认证页找用户要密码）。
        """
        needles = (
            "网络错误", "urlerror", "timed out", "timeout",
            "winerror 10051", "winerror 10065", "winerror 10060", "winerror 10061",
            "no route", "network is unreachable", "connection refused", "连接",
        )
        low = message.lower()
        return any(n in low for n in needles)

    def _wait_online(self, timeout: float = 45.0, step: float = 3.0) -> bool:
        """登录成功后轮询等待网络真正可用。

        门户返回成功 ≠ 立刻能上网：AC 下发授权、RADIUS 同步都要几百毫秒到
        几十秒。以前只 sleep 10 秒查一次，于是常常误报「登录成功但还没通网」
        并触发多余的重登。
        """
        deadline = time.time() + timeout
        while True:
            if self.is_online():
                return True
            if time.time() >= deadline:
                return False
            time.sleep(step)

    def _network_ready(self) -> bool:
        """本机是否已经拿到可用地址（开机瞬间往往还没有）。"""
        try:
            from .netutil import default_local_ip

            ip = default_local_ip()
        except Exception:  # noqa: BLE001
            return True
        if ip:
            return True
        self.log.info("本机还没拿到 IP，等网络就绪…")
        return False

    def watch(self, *, max_cycles: int | None = None,
              lock: "SingleInstance | None" = None) -> int:
        watch = self.cfg.section("watch")
        interval = max(5, int(watch.get("interval", 20)))
        cooldown = max(0, int(watch.get("cooldown_after_login", 10)))
        online_wait = max(5, int(watch.get("online_wait_seconds", 120)))
        net_retry = max(2, int(watch.get("network_retry_seconds", 5)))
        relogin_delay = max(0, int(watch.get("relogin_delay_seconds", 60)))

        self.log.info("看门狗启动: 每 %ss 检查一次网络（Ctrl+C 退出）", interval)
        failures = 0
        cycle = 0
        link_down_skips = 0
        online_since: float | None = None
        last_online_log = 0.0
        try:
            while max_cycles is None or cycle < max_cycles:
                cycle += 1
                if lock is not None:
                    lock.touch()  # 心跳：让 --once 知道我还活着
                # 先问本地链路状态。拔了网线时它毫秒级就返回 False，
                # 不用让 4 个探测点各自等满超时（原来一轮要 20 秒白等）。
                # 也不能全信它 —— 虚拟网卡偶尔会报错状态，所以攒够 15 次
                # 就强制真探测一轮兜底，保证不会永远卡在这儿。
                if link_up() is False and link_down_skips < 15:
                    link_down_skips += 1
                    self.log.debug("网卡链路是断的（没插网线 / 网卡被禁用），等它恢复")
                    self._sleep(net_retry, wake_on_link=True)
                    continue
                link_down_skips = 0

                if self.is_online():
                    if failures:
                        self.log.info("网络已恢复")
                    failures = 0
                    if online_since is None:
                        online_since = time.time()
                    # 在线的时候原来一行日志都不打，于是「它还在不在盯着」完全
                    # 看不出来 —— 用户说「断网了它没反应」时，也没法判断程序当时
                    # 以为自己是在线还是离线。每 5 分钟留一条心跳。
                    if time.time() - last_online_log >= ONLINE_LOG_SECONDS:
                        last_online_log = time.time()
                        self.log.info("在线（已持续 %d 分钟），仍在每 %d 秒检查一次",
                                      max(1, int((time.time() - online_since) // 60)),
                                      interval)
                    self._sleep(interval)
                    continue
                online_since = None
                last_online_log = 0.0

                # 开机瞬间网卡还没起来，这时去登录既没意义、又把退避时间拉长
                if not self._network_ready():
                    self._sleep(net_retry, wake_on_link=True)
                    continue

                failures += 1
                self.log.warning("检测到断网（第 %d 次）: %s", failures, self.describe_offline())

                result = self.login()
                if result.ok:
                    self.log.info("登录成功: %s", result.message)
                    failures = 0
                    if self._wait_online(online_wait):
                        self.log.info("已恢复上网")
                        time.sleep(cooldown)
                    else:
                        # 认证过了但网络迟迟不放行。**不要再立刻重登** ——
                        # 每次重登都会新建会话，可能把 AC 正在下发的授权打断，
                        # 于是永远好不了。这里是实测踩出来的：原来每 75 秒重登
                        # 一次，连续三次都没通；拉长间隔后才恢复。
                        self.log.warning(
                            "认证已通过，但 %d 秒内网络还没放行 —— 不再反复重登"
                            "（重登会新建会话，可能打断 AC 下发）。%d 秒后再看一次。"
                            "若一直如此，通常是还缺一步「选运营商」或需要选套餐。",
                            online_wait, relogin_delay)
                        self._sleep(relogin_delay)
                    self._sleep(interval)
                elif self._is_network_problem(result.message):
                    # 网络还没就绪而已，不算认证失败，也不算退避
                    failures = max(0, failures - 1)
                    self.log.info("认证服务器暂时连不上（%s），%d 秒后重试",
                                  result.message, net_retry)
                    self._sleep(net_retry, wake_on_link=True)
                else:
                    backoff = min(300, interval * (2 ** min(failures, 5)))
                    self.log.error("登录失败: %s（%ds 后重试）", result.message, backoff)
                    self._sleep(backoff)
        except KeyboardInterrupt:
            self.log.info("收到退出信号，看门狗停止")
        return 0

    def _sleep(self, seconds: float, *, wake_on_link: bool = False) -> None:
        """睡 seconds 秒；``wake_on_link`` 时链路一恢复就立刻醒。

        断网等待期间用这个：插回网线到重新认证之间不该有几十秒的空白 ——
        实测用户就是在这段空白里自己开浏览器，然后以为是「校园网登录窗口
        弹出来它才连上」。
        """
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(min(1.0, max(0.0, end - time.time())))
            if wake_on_link and link_up() is True:
                return
