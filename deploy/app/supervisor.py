# -*- coding: utf-8 -*-
"""Точка входа: watchdog + Telegram-бот в одном процессе. Запускается планировщиком при входе в систему."""
import faulthandler
import logging
import os
import sys
import time

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


_FAULT_FILE = None


def _enable_faulthandler():
    """Падение самого интерпретатора (access violation и т.п.) не оставляет traceback в
    логе — faulthandler допишет в logs\\crash_faults.log стек всех потоков в момент падения."""
    global _FAULT_FILE
    try:
        path = os.path.join(common.BASE_DIR, "logs", "crash_faults.log")
        _FAULT_FILE = open(path, "a", encoding="utf-8")
        _FAULT_FILE.write("=== supervisor start %s pid %d\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
        _FAULT_FILE.flush()
        faulthandler.enable(file=_FAULT_FILE, all_threads=True)
    except Exception:  # noqa: BLE001
        logging.exception("supervisor: faulthandler не включился")


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    common.setup_logging("supervisor")
    _enable_faulthandler()
    if not _acquire_lock():
        sys.exit(0)
    web = None
    pweb = None
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

        if (cfg.get("playerweb", {}) or {}).get("enabled", False):
            try:
                from playerweb import PlayerWeb

                pweb = PlayerWeb(cfg, state, web=web)
                pweb.start()
            except Exception:  # noqa: BLE001
                logging.exception("supervisor: панель игроков не запустилась (продолжаю без неё)")
                pweb = None

        logging.info("supervisor: watchdog запущен%s, стартую бота",
                     ", веб-панель запущена" if web else "")
        bot.run()
    except KeyboardInterrupt:
        logging.info("остановка по Ctrl+C")
    finally:
        if pweb:
            try:
                pweb.stop()
            except Exception:  # noqa: BLE001
                pass
        if web:
            try:
                web.stop()
            except Exception:  # noqa: BLE001
                pass
        _release_lock()


if __name__ == "__main__":
    main()
