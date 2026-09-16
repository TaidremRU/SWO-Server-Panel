# -*- coding: utf-8 -*-
"""Watchdog: поддерживает Steam и игру запущенными, шлёт события в Telegram."""
import logging
import threading
import time
from collections import deque

import gamectl
import i18n
import sysinfo


class Watchdog(threading.Thread):
    def __init__(self, cfg, state, alert):
        super().__init__(name="watchdog", daemon=True)
        self.cfg = cfg
        self.state = state
        self.alert = alert  # callable(text)
        # алерты watchdog идут всем админам сразу — язык по умолчанию из конфига
        self._alang = i18n.norm(cfg.get("telegram", {}).get("default_lang", i18n.DEFAULT))

        wd = cfg.get("watchdog", {})
        self.enabled = state.data.get("watchdog_enabled")
        if self.enabled is None:
            self.enabled = wd.get("enabled", True)
        self.apply()
        self._login_state = "idle"  # idle | running | done; do_seq сам определит «уже в игре»
        self._game_up_since = 0.0
        self._login_next = 0.0
        self._last_game_pid = None

        self._restarts = deque()
        self._last_launch = 0.0
        self._prev_game = None
        self._prev_steam = None
        self._limit_warned = False
        self._stop = threading.Event()

        # зафиксировать актуальное значение сразу, чтобы /status не показал «выкл» до первого тика
        with self.state.lock:
            self.state.data["watchdog_enabled"] = self.enabled
        self.state.save()

    def apply(self):
        """Пересчитать параметры из ``self.cfg`` (общий объект с webui/config.json).

        Вызывается из ``__init__`` и веб-панелью после сохранения настроек —
        auto_start_*/grace/лимиты/интервалы применяются на лету, без
        перезапуска задачи планировщика.
        """
        wd = self.cfg.get("watchdog", {})
        self.auto_steam = wd.get("auto_start_steam", True)
        self.auto_game = wd.get("auto_start_game", True)
        self.auto_login = wd.get("auto_login", True)
        self.grace = wd.get("grace_after_launch_seconds", 150)
        self.max_per_hour = wd.get("max_restarts_per_hour", 8)
        self.poll = self.cfg.get("poll_seconds", 30)
        self.login_settle = wd.get("login_settle_seconds", 25)
        self.login_retry = wd.get("login_retry_seconds", 120)

    def stop(self):
        self._stop.set()

    def set_enabled(self, val):
        self.enabled = bool(val)
        with self.state.lock:
            self.state.data["watchdog_enabled"] = self.enabled
        self.state.save()
        logging.info("watchdog enabled -> %s", self.enabled)

    def _rate_ok(self):
        now = time.time()
        while self._restarts and now - self._restarts[0] > 3600:
            self._restarts.popleft()
        return len(self._restarts) < self.max_per_hour

    def _mark_restart(self, kind):
        self._restarts.append(time.time())
        with self.state.lock:
            key = "game_restarts" if kind == "game" else "steam_restarts"
            self.state.data[key] = self.state.data.get(key, 0) + 1
            self.state.data["last_restart_ts"] = time.time()
        self.state.save()

    def run(self):
        delay = self.cfg.get("initial_delay_seconds", 45)
        logging.info("watchdog: первый прогон через %d с", delay)
        if self._stop.wait(delay):
            return
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logging.exception("watchdog tick error")
            self._stop.wait(self.poll)

    def _t(self, key, **kw):
        return i18n.t(self._alang, key, **kw)

    def _gc(self, res):
        """(ok, key, params) от gamectl -> (ok, локализованная строка)."""
        ok, key, params = res
        return ok, i18n.t(self._alang, key, **(params or {}))

    def _limit_msg(self):
        if not self._limit_warned:
            self._limit_warned = True
            self.alert(self._t("wd_alert.limit", n=self.max_per_hour))

    def _game_pid(self):
        pids = [p.pid for p in sysinfo.find_procs(["sigmaworld.exe"], self.cfg.get("game_install_dir"))]
        return min(pids) if pids else None

    def _tick(self):
        cfg = self.cfg
        steam_up = gamectl.steam_running()
        game_up = gamectl.game_running(cfg)

        # новый инстанс игры (PID сменился) -> нужен новый вход, даже если «падение» проскочили между тиками
        pid = self._game_pid()
        if pid != getattr(self, "_last_game_pid", None):
            if pid is not None and getattr(self, "_last_game_pid", None) is not None:
                logging.info("watchdog: новый PID игры %s (был %s) — сбрасываю login_state", pid, self._last_game_pid)
            self._last_game_pid = pid
            self._login_state = "idle"
            self._game_up_since = time.time() if pid else 0.0

        if self._prev_steam is True and not steam_up:
            self.alert(self._t("wd_alert.steam_exited"))
        if self._prev_game is True and not game_up:
            self.alert(self._t("wd_alert.game_closed", name=cfg["game_name"]))

        if self.enabled:
            within_grace = (time.time() - self._last_launch) < self.grace
            if self.auto_steam and not steam_up:
                if self._rate_ok():
                    self.alert(self._t("wd_alert.steam_down_starting"))
                    ok, msg = self._gc(gamectl.start_steam(cfg))
                    self._last_launch = time.time()
                    self._mark_restart("steam")
                    self.alert(self._t("wd_alert.result_ok" if ok else "wd_alert.result_fail", msg=msg))
                    steam_up = ok
                else:
                    self._limit_msg()
            elif steam_up and self.auto_game and not game_up and not within_grace:
                if self._rate_ok():
                    self.alert(self._t("wd_alert.game_down_starting"))
                    ok, msg = self._gc(gamectl.start_game(cfg))
                    self._last_launch = time.time()
                    self._mark_restart("game")
                    self.alert(self._t("wd_alert.result_ok" if ok else "wd_alert.result_fail", msg=msg))
                    game_up = ok
                else:
                    self._limit_msg()
            if self._rate_ok():
                self._limit_warned = False

        # авто-вход в игру: игра поднялась, но мы не в игре -> прогнать последовательность
        if not game_up:
            self._login_state = "idle"
            self._game_up_since = 0.0
        else:
            if self._game_up_since == 0.0:
                self._game_up_since = time.time()
            if (self.auto_login and self._login_state == "idle"
                    and time.time() - self._game_up_since >= self.login_settle
                    and time.time() >= self._login_next):
                self._start_login()

        try:
            snap = sysinfo.collect(cfg, self.state)
            with self.state.lock:
                self.state.data["last_snapshot"] = snap
                self.state.data["watchdog_enabled"] = self.enabled
                self.state.data["login_state"] = self._login_state
            self.state.save()
        except Exception:  # noqa: BLE001
            logging.exception("collect error")

        self._prev_steam = steam_up
        self._prev_game = game_up

    def _start_login(self):
        self._login_state = "running"

        def work():
            try:
                import runner

                ok, _out = runner.run_nav("seq login", timeout=280)
            except Exception:  # noqa: BLE001
                logging.exception("auto-login error")
                ok = False
            if ok:
                self._login_state = "done"
                self.alert(self._t("wd_alert.login_ok"))
            else:
                self._login_state = "idle"
                self._login_next = time.time() + self.login_retry
                self.alert(self._t("wd_alert.login_retry", sec=self.login_retry))

        threading.Thread(target=work, name="autologin", daemon=True).start()
