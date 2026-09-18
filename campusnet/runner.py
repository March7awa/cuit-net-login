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
from .netutil import CHECK_URLS, detect_captive_redirect, is_online
from .providers import LoginResult, get_provider

__all__ = ["Setup", "build_logger", "SingleInstance", "Runner"]

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


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
class SingleInstance:
    """Cross-platform advisory lock so two watchdogs never fight each other."""

    def __init__(self, name: str = "watch.lock") -> None:
        self.path = config_mod.user_config_dir() / name
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(str(os.getpid()))
            self._fh.flush()
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            self._fh.close()
            self._fh = None

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

    def watch(self, *, max_cycles: int | None = None) -> int:
        watch = self.cfg.section("watch")
        interval = max(5, int(watch.get("interval", 20)))
        cooldown = max(0, int(watch.get("cooldown_after_login", 10)))
        online_wait = max(5, int(watch.get("online_wait_seconds", 120)))
        net_retry = max(2, int(watch.get("network_retry_seconds", 5)))
        relogin_delay = max(0, int(watch.get("relogin_delay_seconds", 60)))

        self.log.info("看门狗启动: 每 %ss 检查一次网络（Ctrl+C 退出）", interval)
        failures = 0
        cycle = 0
        try:
            while max_cycles is None or cycle < max_cycles:
                cycle += 1
                if self.is_online():
                    if failures:
                        self.log.info("网络已恢复")
                    failures = 0
                    self._sleep(interval)
                    continue

                # 开机瞬间网卡还没起来，这时去登录既没意义、又把退避时间拉长
                if not self._network_ready():
                    self._sleep(net_retry)
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
                    self._sleep(net_retry)
                else:
                    backoff = min(300, interval * (2 ** min(failures, 5)))
                    self.log.error("登录失败: %s（%ds 后重试）", result.message, backoff)
                    self._sleep(backoff)
        except KeyboardInterrupt:
            self.log.info("收到退出信号，看门狗停止")
        return 0

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(min(1.0, end - time.time()))
