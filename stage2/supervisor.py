# -*- coding: utf-8 -*-
"""Точка входа: watchdog + Telegram-бот в одном процессе. Запускается планировщиком при входе в систему."""
import logging
import os
import sys

import psutil

import common
from bot import Bot
from watchdog import Watchdog

LOCK = os.path.join(common.BASE_DIR, "supervisor.lock")


def _acquire_lock():
    if os.path.exists(LOCK):
        try:
            pid = int(open(LOCK).read().strip())
            if psutil.pid_exists(pid) and pid != os.getpid():
                name = (psutil.Process(pid).name() or "").lower()
                if "python" in name:
                    logging.error("Уже запущен экземпляр (PID %s) — выхожу.", pid)
                    return False
        except Exception:  # noqa: BLE001
            pass
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))
    return True


def _release_lock():
    try:
        os.remove(LOCK)
    except OSError:
        pass


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    common.setup_logging("supervisor")
    if not _acquire_lock():
        sys.exit(0)
    web = None
    try:
        cfg = common.load_config()
        state = common.State(os.path.join(cfg["base_dir"], "state.json"))
        with state.lock:
            state.data["boot_id"] = int(psutil.boot_time())
        state.save()

        bot = Bot(cfg, state)
        wd = Watchdog(cfg, state, alert=bot.push_alert)
        bot.wd = wd
        wd.start()

        if (cfg.get("webui", {}) or {}).get("enabled", True):
            try:
                from webui import WebUI

                web = WebUI(cfg, state, bot, wd)
                web.start()
                bot.web = web
            except Exception:  # noqa: BLE001
                logging.exception("supervisor: веб-панель не запустилась (продолжаю без неё)")
                web = None

        logging.info("supervisor: watchdog запущен%s, стартую бота",
                     ", веб-панель запущена" if web else "")
        bot.run()
    except KeyboardInterrupt:
        logging.info("остановка по Ctrl+C")
    finally:
        if web:
            try:
                web.stop()
            except Exception:  # noqa: BLE001
                pass
        _release_lock()


if __name__ == "__main__":
    main()
