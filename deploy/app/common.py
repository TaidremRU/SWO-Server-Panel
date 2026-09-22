# -*- coding: utf-8 -*-
"""Общие утилиты: конфиг, логирование, состояние, клиент Telegram (curl + SOCKS5)."""
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
import urllib.parse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CURL = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "curl.exe")

_cfg = None


def load_config():
    global _cfg
    if _cfg is None:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _cfg = json.load(f)
    return _cfg


def save_config(cfg=None):
    """Атомарно записать config.json в чистом UTF-8 (БЕЗ BOM).

    ``json.load`` в проекте читает конфиг как обычный ``utf-8`` — BOM (который
    оставляет, например, PowerShell ``Set-Content -Encoding UTF8``) валит разбор и
    роняет супервизор. ``open(..., encoding="utf-8")`` в Python BOM не пишет.
    Обновляет и кэш ``_cfg`` — все, кто держит ссылку из ``load_config()``, видят
    изменения (роли применяются на лету без перезапуска).
    """
    global _cfg
    data = cfg if cfg is not None else _cfg
    if data is None:
        raise ValueError("save_config: нет данных конфига")
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    _cfg = data


def setup_logging(name="supervisor"):
    cfg = load_config()
    log_dir = os.path.join(cfg.get("base_dir", BASE_DIR), "logs")
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger()
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, name + ".log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if sys.stderr is not None:  # под pythonw.exe stderr может быть None
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


class State:
    """Счётчики и последний снапшот, сохраняются в state.json."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {
            "game_restarts": 0,
            "steam_restarts": 0,
            "last_game_exit_code": None,
            "last_restart_ts": None,
            "watchdog_enabled": None,
            "boot_id": None,
            "last_snapshot": {},
            "user_lang": {},
        }
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:  # терпим BOM
                self.data.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            logging.warning("Не удалось прочитать %s: %s", self.path, e)

    def save(self):
        tmp = self.path + ".tmp"
        with self.lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)


ACTION_LOCK_TTL = 300  # 5 минут — окно взаимоисключения опасных действий (gamectl.LOCKED_OPS)


def try_action_lock(state, actor, op):
    """Не даёт двум админам/модераторам одновременно дёргать restart/stop игры,
    Steam или VM — держит один активный лок ``ACTION_LOCK_TTL`` секунд с момента
    первого действия (не продлевается и не снимается досрочно по завершении).

    -> ``(True, None)`` — лок взят (или это тот же ``actor``, что и держит его),
    можно выполнять; ``(False, {"actor", "op", "left"})`` — занято другим, с
    сколько секунд осталось ждать."""
    now = time.time()
    with state.lock:
        lock = state.data.get("action_lock")
        if lock and lock.get("actor") != actor and now - lock.get("started", 0) < ACTION_LOCK_TTL:
            left = int(ACTION_LOCK_TTL - (now - lock["started"]))
            return False, {"actor": lock.get("actor"), "op": lock.get("op"), "left": max(left, 1)}
        state.data["action_lock"] = {"actor": actor, "op": op, "started": now}
    state.save()
    return True, None


class Telegram:
    """Минимальный клиент Bot API поверх curl.exe с SOCKS5-прокси."""

    def __init__(self, token, proxy, poll_timeout=50, send_timeout=20):
        self.base = "https://api.telegram.org/bot" + token
        p = urllib.parse.urlparse(proxy)
        self.proxy_arg = "{}:{}".format(p.hostname, p.port or 1080)
        self.poll_timeout = poll_timeout
        self.send_timeout = send_timeout

    # коды curl, при которых имеет смысл повторить (сеть/прокси/TLS моргнули)
    _RETRY_RC = {7, 28, 35, 52, 56}

    def _curl(self, method, args, max_time, attempts=3):
        cmd = [
            CURL, "-s", "--max-time", str(max_time),
            "--socks5-hostname", self.proxy_arg,
            self.base + "/" + method,
        ] + args
        last = {"ok": False, "error": "curl: no attempts"}
        for i in range(attempts):
            try:
                out = subprocess.run(cmd, capture_output=True, timeout=max_time + 15)
            except subprocess.TimeoutExpired:
                last = {"ok": False, "error": "curl timeout"}
            except Exception as e:  # noqa: BLE001
                last = {"ok": False, "error": "curl spawn: %s" % e}
            else:
                if out.returncode == 0:
                    try:
                        return json.loads(out.stdout.decode("utf-8", "replace"))
                    except Exception as e:  # noqa: BLE001
                        return {"ok": False, "error": "bad json: %s" % e}
                last = {
                    "ok": False,
                    "error": "curl rc=%d %s" % (out.returncode, out.stderr.decode("utf-8", "replace")[:200]),
                }
                if out.returncode not in self._RETRY_RC:
                    return last
            if i < attempts - 1:
                time.sleep(2)
        return last

    def call(self, method, **params):
        args = []
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            else:
                v = str(v)
            args += ["--data-urlencode", "%s=%s" % (k, v)]
        # короткий таймаут + 2 попытки — отправки не должны надолго блокировать вызывающего
        return self._curl(method, args, self.send_timeout, attempts=2)

    def get_updates(self, offset, timeout):
        return self._curl(
            "getUpdates",
            ["-d", "timeout=%d" % timeout, "-d", "offset=%d" % offset],
            timeout + 10,
            attempts=2,
        )

    def send_message(self, chat_id, text, reply_markup=None):
        return self.call(
            "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
            disable_web_page_preview=True, reply_markup=reply_markup,
        )

    def send_photo(self, chat_id, path, caption=None):
        args = ["-F", "chat_id=%s" % chat_id, "-F", "photo=@%s" % path]
        if caption:
            args += ["-F", "caption=%s" % caption]
        return self._curl("sendPhoto", args, 45, attempts=2)

    def answer_callback(self, cbid, text=None):
        return self.call("answerCallbackQuery", callback_query_id=cbid, text=text)


class Discord:
    """Минимальный клиент вебхука Discord поверх curl.exe (прокси — опционально,
    как и у ``Telegram``: нужен только если Discord заблокирован с этой VM)."""

    def __init__(self, webhook_url, proxy=None, timeout=15):
        self.webhook_url = webhook_url.rstrip("/")
        self.proxy_arg = None
        if proxy:
            p = urllib.parse.urlparse(proxy)
            self.proxy_arg = "{}:{}".format(p.hostname, p.port or 1080)
        self.timeout = timeout

    def _curl(self, method, url, body):
        cmd = [CURL, "-s", "--max-time", str(self.timeout)]
        if self.proxy_arg:
            cmd += ["--socks5-hostname", self.proxy_arg]
        cmd += ["-X", method, "-H", "Content-Type: application/json",
                "-d", json.dumps(body, ensure_ascii=False), "-w", "\n%{http_code}", url]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=self.timeout + 15)
        except subprocess.TimeoutExpired:
            return 0, {"error": "curl timeout"}
        except Exception as e:  # noqa: BLE001
            return 0, {"error": "curl spawn: %s" % e}
        if out.returncode != 0:
            return 0, {"error": "curl rc=%d %s" % (out.returncode, out.stderr.decode("utf-8", "replace")[:200])}
        raw = out.stdout.decode("utf-8", "replace")
        body_raw, _, code_raw = raw.rpartition("\n")
        try:
            code = int(code_raw.strip())
        except ValueError:
            code = 0
        data = None
        if body_raw.strip():
            try:
                data = json.loads(body_raw)
            except Exception:  # noqa: BLE001
                data = {"raw": body_raw[:300]}
        return code, data

    def post(self, embed):
        """Новое сообщение с ``embed``. -> (ok, message_id|None, error|None)."""
        code, data = self._curl("POST", self.webhook_url + "?wait=true", {"embeds": [embed]})
        if not (200 <= code < 300):
            return False, None, "http %d: %s" % (code, data)
        mid = (data or {}).get("id")
        if not mid:
            return False, None, "нет id сообщения в ответе"
        return True, mid, None

    def edit(self, message_id, embed):
        """Правка ранее отправленного сообщения по его id. -> (ok, error|None)."""
        code, data = self._curl("PATCH", "%s/messages/%s" % (self.webhook_url, message_id), {"embeds": [embed]})
        if 200 <= code < 300:
            return True, None
        return False, "http %d: %s" % (code, data)
