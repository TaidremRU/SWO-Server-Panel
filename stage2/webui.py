# -*- coding: utf-8 -*-
"""Веб-панель управления SigmaSteamBot (HTTP на 0.0.0.0).

Запускается **потоком внутри supervisor.py** — как ``bot`` и ``watchdog``, —
поэтому держит прямые ссылки на ``bot`` / ``watchdog`` / ``cfg`` / ``state``:
watchdog-тумблер, правка ролей на лету и остановка бота работают без IPC.

Нагрузка на супервизор минимальна: ``GET /api/state`` отдаёт последний снапшот,
который watchdog и так пишет в ``state.json`` (``last_snapshot``). Живой
``sysinfo.collect()`` — только по запросу ``?live=1`` (кнопка «обновить сейчас»).

Аутентификация: ``webui_auth.json`` в ``base_dir`` (в .gitignore). При первом
запуске создаётся с логином **admin / admin** и требованием сменить пароль
(``must_change``) — до смены доступен только экран смены пароля. Хэш —
PBKDF2-HMAC-SHA256. Сессия — cookie ``sid`` (в памяти процесса). POST-запросы
защищены CSRF-токеном (заголовок ``X-CSRF-Token``). Неудачные входы — с лок-аутом
по IP.

Протокол — обычный HTTP: панель только для локальной сети (как и остальной
доступ к этому боксу).
"""
import hashlib
import http.cookies
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import common
import gamectl
import i18n
import players
import screenshot
import serverlist
import sysinfo

try:
    import psutil
except Exception:  # noqa: BLE001
    psutil = None

VERSION = "1.0"
SESSION_TTL = 12 * 3600
SHOT_MIN_INTERVAL = 4.0
SERVERS_CACHE_SEC = 45
PLAYERS_CACHE_SEC = 15


def _now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class _Bad(Exception):
    """Ошибка валидации ввода — отдаётся клиенту как 400."""


# --------------------------------------------------------------------------- auth
class AuthStore:
    """Логин/пароль веб-панели в ``webui_auth.json`` (PBKDF2-HMAC-SHA256)."""

    ITERS = 200_000

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._load_or_init()

    def _load_or_init(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.d = json.load(f)
            if not {"username", "salt", "hash"} <= set(self.d):
                raise ValueError("неполный файл")
        except FileNotFoundError:
            self.d = self._make("admin", "admin", must_change=True)
            self._save()
            logging.warning(
                "webui: создан %s — вход admin/admin, СМЕНИТЕ ПАРОЛЬ при первом входе", self.path
            )
        except Exception:  # noqa: BLE001
            logging.exception("webui: %s повреждён — пересоздаю admin/admin", self.path)
            self.d = self._make("admin", "admin", must_change=True)
            self._save()

    def _make(self, user, pw, must_change):
        salt = secrets.token_bytes(16)
        return {
            "username": user,
            "algo": "pbkdf2_sha256",
            "iterations": self.ITERS,
            "salt": salt.hex(),
            "hash": self._hash(pw, salt, self.ITERS),
            "must_change": bool(must_change),
            "updated": _now_iso(),
        }

    @staticmethod
    def _hash(pw, salt, iters):
        return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iters).hex()

    @property
    def username(self):
        return self.d.get("username", "admin")

    @property
    def must_change(self):
        return bool(self.d.get("must_change"))

    def verify(self, user, pw):
        if user != self.d.get("username"):
            return False
        got = self._hash(pw, bytes.fromhex(self.d["salt"]), int(self.d.get("iterations", self.ITERS)))
        return secrets.compare_digest(got, self.d.get("hash", ""))

    def set_password(self, newpw, newuser=None):
        with self._lock:
            self.d = self._make(newuser or self.username, newpw, must_change=False)
            self._save()

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


class Sessions:
    """Сессии в памяти: token -> {user, ip, csrf, born, seen}."""

    def __init__(self):
        self._d = {}
        self._lock = threading.Lock()

    def new(self, user, ip):
        tok = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with self._lock:
            self._d[tok] = {"user": user, "ip": ip, "csrf": csrf,
                            "born": time.time(), "seen": time.time()}
        return tok, csrf

    def get(self, tok):
        with self._lock:
            s = self._d.get(tok)
            if not s:
                return None
            if time.time() - s["seen"] > SESSION_TTL:
                self._d.pop(tok, None)
                return None
            s["seen"] = time.time()
            return dict(s)

    def drop(self, tok):
        with self._lock:
            self._d.pop(tok, None)


class Throttle:
    """Лок-аут входа по IP: N неудач за window -> блок на block секунд."""

    def __init__(self, max_fail=5, window=300, block=60):
        self.max_fail, self.window, self.block = max_fail, window, block
        self._d = {}
        self._lock = threading.Lock()

    def check(self, ip):
        with self._lock:
            e = self._d.get(ip)
            if not e:
                return True, 0
            if e.get("until", 0) > time.time():
                return False, int(e["until"] - time.time())
            return True, 0

    def fail(self, ip):
        with self._lock:
            e = self._d.setdefault(ip, {"fails": 0, "first": time.time(), "until": 0})
            if time.time() - e["first"] > self.window:
                e["fails"], e["first"] = 0, time.time()
            e["fails"] += 1
            if e["fails"] >= self.max_fail:
                e["until"] = time.time() + self.block
                e["fails"], e["first"] = 0, time.time()

    def ok(self, ip):
        with self._lock:
            self._d.pop(ip, None)


# ------------------------------------------------------------------- log helpers
_LOG_RX = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+) (\w+) \[([^\]]*)\] (.*)$")


def _tail(path, nbytes):
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            if sz > nbytes:
                f.seek(sz - nbytes)
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    if sz > nbytes:
        text = text.split("\n", 1)[-1]
    return text


def _parse_log(text):
    out = []
    for ln in text.splitlines():
        m = _LOG_RX.match(ln)
        if m:
            out.append({"ts": m.group(1), "level": m.group(2),
                        "thread": m.group(3), "msg": m.group(4)})
        elif out:
            out[-1]["msg"] += "\n" + ln
        else:
            out.append({"ts": "", "level": "", "thread": "", "msg": ln})
    return out


# --------------------------------------------------------------------- the server
class _Handler(BaseHTTPRequestHandler):
    server_version = "SigmaWebUI/" + VERSION

    def log_message(self, fmt, *args):  # тише стандартного вывода в stderr
        logging.debug("webui %s %s", self.address_string(), fmt % args)

    def do_GET(self):
        self.server.webui.dispatch(self, "GET")

    def do_POST(self):
        self.server.webui.dispatch(self, "POST")


class WebUI:
    def __init__(self, cfg, state, bot, wd):
        self.cfg = cfg
        self.state = state
        self.bot = bot
        self.wd = wd
        base = cfg.get("base_dir", common.BASE_DIR)
        self.auth = AuthStore(os.path.join(base, "webui_auth.json"))
        self.sessions = Sessions()
        self.throttle = Throttle()
        self._audit_path = os.path.join(base, "webui_audit.log")
        self._audit_lock = threading.Lock()
        self._srv = None
        self._thread = None
        self._srv_cache = None  # (ts, payload)
        self._players_cache = None  # (ts, payload)
        self._stats_cache = None  # (ts, payload)
        self._world_cache = None  # (ts, payload)
        self._health_cache = None  # (ts, payload)
        self._shot_lock = threading.Lock()
        self._shot_ts = 0.0
        self._shot_meta = ("", (0, 0))
        self._jobs = {}
        self._jobs_lock = threading.Lock()

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        w = self.cfg.get("webui", {}) or {}
        host = w.get("host", "0.0.0.0")
        port = int(w.get("port", 8080))
        self._srv = ThreadingHTTPServer((host, port), _Handler)
        self._srv.daemon_threads = True
        self._srv.webui = self
        self._thread = threading.Thread(target=self._srv.serve_forever, name="webui", daemon=True)
        self._thread.start()
        logging.info("webui: слушаю http://%s:%d/ — вход %s%s", host, port, self.auth.username,
                     "  (СМЕНИТЕ ПАРОЛЬ)" if self.auth.must_change else "")

    def stop(self):
        try:
            if self._srv:
                self._srv.shutdown()
                self._srv.server_close()
                logging.info("webui: остановлена")
        except Exception:  # noqa: BLE001
            logging.exception("webui: ошибка остановки")

    # ------------------------------------------------------------------- audit
    def audit(self, ip, user, msg):
        line = "%s\t%s\t%s\t%s\n" % (_now_iso(), ip, user, msg)
        try:
            with self._audit_lock, open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            logging.exception("webui: не удалось записать аудит")
        logging.info("webui audit: %s [%s] %s", user, ip, msg)

    # ------------------------------------------------------------- http helpers
    def _send(self, h, status, ctype, body, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            h.send_response(status)
            h.send_header("Content-Type", ctype)
            h.send_header("Content-Length", str(len(body)))
            h.send_header("X-Content-Type-Options", "nosniff")
            h.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                h.send_header(k, v)
            h.end_headers()
            if h.command != "HEAD":
                h.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, h, obj, status=200, set_cookie=None):
        extra = {"Cache-Control": "no-store"}
        if set_cookie:
            extra["Set-Cookie"] = set_cookie
        self._send(h, status, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False, default=str), extra)

    def _body(self, h):
        try:
            n = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = h.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            v = json.loads(raw.decode("utf-8"))
            return v if isinstance(v, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _session_of(self, h):
        c = http.cookies.SimpleCookie(h.headers.get("Cookie", ""))
        tok = c["sid"].value if "sid" in c else ""
        return (tok, self.sessions.get(tok)) if tok else ("", None)

    # ---------------------------------------------------------------- dispatch
    def dispatch(self, h, method):
        try:
            path, _, qs = h.path.partition("?")
            q = urllib.parse.parse_qs(qs)
            if path in ("/", "/index.html") and method == "GET":
                return self._send(h, 200, "text/html; charset=utf-8", PAGE,
                                  {"Cache-Control": "no-store"})
            if path == "/favicon.ico":
                return self._send(h, 204, "text/plain", b"")
            if not path.startswith("/api/"):
                return self._send(h, 404, "text/plain; charset=utf-8", b"not found")

            route = path[len("/api/"):].strip("/")
            if route == "session" and method == "GET":
                return self._api_session(h)
            if route == "login" and method == "POST":
                return self._api_login(h)

            tok, sess = self._session_of(h)
            if not sess:
                return self._json(h, {"error": "auth"}, 401)

            if method == "POST":
                given = h.headers.get("X-CSRF-Token", "")
                if not given or not secrets.compare_digest(given, sess["csrf"]):
                    return self._json(h, {"error": "csrf"}, 403)

            if route == "logout" and method == "POST":
                self.sessions.drop(tok)
                return self._json(h, {"ok": True}, set_cookie="sid=; Path=/; Max-Age=0")
            if route == "password" and method == "POST":
                return self._api_password(h, sess)

            if self.auth.must_change:
                return self._json(h, {"error": "must_change"}, 403)

            if route.startswith("players/"):
                parts = route.split("/")
                pid = parts[1] if len(parts) > 1 else ""
                sub = parts[2] if len(parts) > 2 else ""
                if len(parts) == 2 and method == "GET":
                    return self._api_player_detail(h, pid, sess)
                if sub == "chat" and method == "GET":
                    return self._api_player_chat(h, pid, q, sess)
                if sub == "secret" and method == "POST":
                    return self._api_player_secret(h, pid, sess)
                if sub == "sensitive" and method == "POST":
                    return self._api_player_sensitive(h, pid, sess)
                if sub == "inventory" and method == "POST":
                    return self._api_player_inventory(h, pid, sess)
                if sub == "moderate" and method == "POST":
                    return self._api_player_moderate(h, pid, sess)
                return self._json(h, {"error": "unknown"}, 404)

            fn = getattr(self, "_api_" + route.replace("-", "_"), None)
            if not fn:
                return self._json(h, {"error": "unknown"}, 404)
            return fn(h, method, q, sess)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: dispatch %s %s", method, getattr(h, "path", "?"))
            try:
                self._json(h, {"error": "internal", "detail": str(e)}, 500)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ public
    def _api_session(self, h):
        _, sess = self._session_of(h)
        out = {"app": "SigmaSteamBot", "version": VERSION, "authed": bool(sess),
               "langs": list(i18n.SUPPORTED)}
        if sess:
            out.update(username=sess["user"], csrf=sess["csrf"], must_change=self.auth.must_change)
        return self._json(h, out)

    def _api_login(self, h):
        ip = h.client_address[0]
        ok, wait = self.throttle.check(ip)
        if not ok:
            return self._json(h, {"error": "throttled", "retry": wait}, 429)
        b = self._body(h)
        user = (b.get("username") or "").strip()
        pw = b.get("password") or ""
        if user and pw and self.auth.verify(user, pw):
            self.throttle.ok(ip)
            tok, csrf = self.sessions.new(user, ip)
            self.audit(ip, user, "вход в панель")
            return self._json(
                h, {"ok": True, "username": user, "csrf": csrf, "must_change": self.auth.must_change},
                set_cookie="sid=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (tok, SESSION_TTL),
            )
        self.throttle.fail(ip)
        logging.warning("webui: неудачный вход user=%r ip=%s", user, ip)
        return self._json(h, {"error": "bad_credentials"}, 401)

    def _api_password(self, h, sess):
        b = self._body(h)
        old, new = b.get("old") or "", b.get("new") or ""
        if not self.auth.verify(sess["user"], old):
            return self._json(h, {"error": "bad_old"}, 403)
        if len(new) < 6:
            return self._json(h, {"error": "too_short"}, 400)
        if new.lower() in ("admin", "password", sess["user"].lower()):
            return self._json(h, {"error": "too_weak"}, 400)
        self.auth.set_password(new)
        self.audit(h.client_address[0], sess["user"], "смена пароля")
        return self._json(h, {"ok": True})

    # ------------------------------------------------------------------ state
    def _internals(self):
        now = time.time()
        st = self.state.data
        mon = st.get("monitor_astral") or {}
        snap_ts = (st.get("last_snapshot") or {}).get("ts")
        info = {
            "threads": threading.active_count(),
            "thread_names": sorted(t.name for t in threading.enumerate()),
            "outbox": self.bot._outbox.qsize(),
            "last_poll_ok_age": int(now - self.bot._last_poll_ok) if getattr(self.bot, "_last_poll_ok", 0) else None,
            "snapshot_age": int(now - snap_ts) if snap_ts else None,
            "monitor": {
                "name": getattr(self.bot, "_mon_name", None),
                "enabled": getattr(self.bot, "_mon_enabled", None),
                "present": mon.get("present"),
                "misses": mon.get("misses", 0),
                "alerted": bool(mon.get("alerted")),
            },
        }
        if psutil is not None:
            try:
                info["proc_uptime"] = int(now - psutil.Process().create_time())
            except Exception:  # noqa: BLE001
                info["proc_uptime"] = None
        return info

    def _api_state(self, h, method, q, sess):
        live = q.get("live", ["0"])[0] == "1"
        if live:
            try:
                snap = sysinfo.collect(self.cfg, self.state)
            except Exception as e:  # noqa: BLE001
                logging.exception("webui: live collect")
                snap, live = dict(self.state.data.get("last_snapshot") or {}), False
                snap["_live_error"] = str(e)
        else:
            snap = dict(self.state.data.get("last_snapshot") or {})
        tg = self.cfg.get("telegram", {})
        return self._json(h, {
            "snapshot": snap,
            "live": live,
            "watchdog_enabled": bool(self.wd.enabled) if self.wd else None,
            "login_state": self.state.data.get("login_state"),
            "internals": self._internals(),
            "roles": {
                "admins": len(tg.get("allowed_user_ids", [])),
                "mods": len(tg.get("moderator_user_ids", [])),
                "alerts_enabled": bool(tg.get("alerts_enabled", True)),
                "default_lang": tg.get("default_lang", "ru"),
            },
            "now": time.time(),
        })

    # ---------------------------------------------------------------- servers
    def _api_servers(self, h, method, q, sess):
        now = time.time()
        if not self._srv_cache or now - self._srv_cache[0] > SERVERS_CACHE_SEC:
            try:
                ok, res, src = serverlist.fetch(self.cfg)
            except Exception as e:  # noqa: BLE001
                logging.exception("webui: serverlist")
                ok, res, src = False, str(e), "none"
            self._srv_cache = (now, {"ok": ok, "servers": res if ok else [],
                                     "error": None if ok else str(res), "source": src})
        ts, payload = self._srv_cache
        out = dict(payload)
        out["cached_age"] = int(now - ts)
        return self._json(h, out)

    # ---------------------------------------------------------------- players
    def _api_players(self, h, method, q, sess):
        now = time.time()
        if not self._players_cache or now - self._players_cache[0] > PLAYERS_CACHE_SEC:
            try:
                payload = players.snapshot(self.cfg)
            except Exception as e:  # noqa: BLE001
                logging.exception("webui: players.snapshot")
                payload = {"ok": False, "error": str(e)}
            self._players_cache = (now, payload)
        ts, payload = self._players_cache
        out = dict(payload)
        out["cached_age"] = int(now - ts)
        return self._json(h, out)

    def _api_player_detail(self, h, pid, sess):
        try:
            d = players.player_detail(self.cfg, pid)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: player_detail %s", pid)
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _api_player_chat(self, h, pid, q, sess):
        try:
            n = min(300, max(10, int((q.get("limit") or ["60"])[0])))
        except ValueError:
            n = 60
        try:
            d = players.player_chat(self.cfg, pid, n)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: player_chat %s", pid)
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _reauth(self, h, sess, what, body=None):
        """Повторная проверка админ-пароля панели (для чувствительных операций).
        ``body`` — уже разобранный JSON тела (если None — читается здесь).
        -> (ok, body_dict_or_error_response)."""
        ip = h.client_address[0]
        okt, wait = self.throttle.check(ip)
        if not okt:
            return False, self._json(h, {"error": "throttled", "retry": wait}, 429)
        b = body if body is not None else self._body(h)
        if not self.auth.verify(sess["user"], (b.get("password") or "")):
            self.throttle.fail(ip)
            self.audit(ip, sess["user"], "НЕВЕРНЫЙ пароль: %s" % what)
            return False, self._json(h, {"error": "bad_password"}, 403)
        self.throttle.ok(ip)
        return True, b

    def _api_player_secret(self, h, pid, sess):
        """Пароль игрока — только после повторного ввода админского пароля панели."""
        ok, resp = self._reauth(h, sess, "показать код игрока #%s" % pid)
        if not ok:
            return resp
        try:
            code = players.player_code(self.cfg, pid)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: player_code %s", pid)
            return self._json(h, {"error": "internal", "detail": str(e)}, 500)
        self.audit(h.client_address[0], sess["user"], "ПОКАЗАН пароль игрока #%s" % pid)
        return self._json(h, {"ok": True, "code": code})

    def _api_player_sensitive(self, h, pid, sess):
        """Приватные сообщения игрока + история IP — тоже под админ-паролем."""
        ok, resp = self._reauth(h, sess, "показать приваты/IP игрока #%s" % pid)
        if not ok:
            return resp
        try:
            d = players.player_sensitive(self.cfg, pid)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: player_sensitive %s", pid)
            return self._json(h, {"error": "internal", "detail": str(e)}, 500)
        self.audit(h.client_address[0], sess["user"],
                   "ПОКАЗАНЫ приваты/IP игрока #%s (%d сообщ., %d IP)"
                   % (pid, len(d.get("private", [])), len(d.get("distinct_ips", []))))
        return self._json(h, d)

    def _api_items(self, h, method, q, sess):
        try:
            d = players.item_catalog(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: item_catalog")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 500)

    def _api_server_chat(self, h, method, q, sess):
        """GET — публичный чат сервера; POST {password} — приватные сообщения (gated)."""
        if method == "POST":
            b = self._body(h)
            ok, resp = self._reauth(h, sess, "приватный чат сервера", body=b)
            if not ok:
                return resp
            try:
                d = players.server_private_chat(self.cfg, b.get("limit") or 300, b.get("q"))
            except Exception as e:  # noqa: BLE001
                logging.exception("webui: server_private_chat")
                return self._json(h, {"error": "internal", "detail": str(e)}, 500)
            self.audit(h.client_address[0], sess["user"],
                       "ПРОСМОТР приватного чата сервера (%d сообщ.)" % len(d.get("messages", [])))
            return self._json(h, d)
        try:
            d = players.server_chat(self.cfg, (q.get("limit") or ["200"])[0],
                                    (q.get("channel") or [None])[0], (q.get("q") or [None])[0])
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: server_chat")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _api_stats(self, h, method, q, sess):
        now = time.time()
        if not self._stats_cache or now - self._stats_cache[0] > 30:
            try:
                payload = players.stats_bundle(self.cfg)
            except Exception as e:  # noqa: BLE001
                logging.exception("webui: stats_bundle")
                payload = {"ok": False, "error": str(e)}
            self._stats_cache = (now, payload)
        ts, payload = self._stats_cache
        out = dict(payload)
        out["cached_age"] = int(now - ts)
        return self._json(h, out, 200 if out.get("ok") else 500)

    def _api_world(self, h, method, q, sess):
        now = time.time()
        if not self._world_cache or now - self._world_cache[0] > 60:
            try:
                payload = players.world_map(self.cfg)
            except Exception as e:  # noqa: BLE001
                logging.exception("webui: world_map")
                payload = {"ok": False, "error": str(e)}
            self._world_cache = (now, payload)
        ts, payload = self._world_cache
        out = dict(payload)
        out["cached_age"] = int(now - ts)
        return self._json(h, out, 200 if out.get("ok") else 500)

    def _api_players_csv(self, h, method, q, sess):
        try:
            data, fn = players.players_csv(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: players_csv")
            return self._json(h, {"error": "internal", "detail": str(e)}, 500)
        if data is None:
            return self._json(h, {"error": fn}, 500)
        return self._send(h, 200, "text/csv; charset=utf-8", data,
                          {"Content-Disposition": 'attachment; filename="%s"' % fn,
                           "Cache-Control": "no-store"})

    def _api_world_backup(self, h, method, q, sess):
        """POST {password, scope}: zip каталога мира (в нём пароли игроков) — под админ-паролем."""
        b = self._body(h)
        ok, resp = self._reauth(h, sess, "бэкап мира", body=b)
        if not ok:
            return resp
        scope = "full" if (b.get("scope") == "full") else "state"
        try:
            path, err = players.make_world_backup(self.cfg, scope)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: make_world_backup")
            return self._json(h, {"error": "internal", "detail": str(e)}, 500)
        if not path:
            return self._json(h, {"error": err}, 500)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return self._json(h, {"error": "read", "detail": str(e)}, 500)
        self.audit(h.client_address[0], sess["user"],
                   "БЭКАП МИРА (%s, %d МБ) -> %s" % (scope, len(data) // 1048576, os.path.basename(path)))
        return self._send(h, 200, "application/zip", data,
                          {"Content-Disposition": 'attachment; filename="%s"' % os.path.basename(path),
                           "Cache-Control": "no-store"})

    def _api_health(self, h, method, q, sess):
        now = time.time()
        if not self._health_cache or now - self._health_cache[0] > 60:
            try:
                payload = players.server_health(self.cfg)
            except Exception as e:  # noqa: BLE001
                logging.exception("webui: server_health")
                payload = {"ok": False, "error": str(e)}
            self._health_cache = (now, payload)
        ts, payload = self._health_cache
        out = dict(payload)
        out["cached_age"] = int(now - ts)
        return self._json(h, out, 200 if out.get("ok") else 500)

    def _api_server_events(self, h, method, q, sess):
        kinds = (q.get("kinds") or [""])[0]
        kinds = [k for k in kinds.split(",") if k] or None
        try:
            d = players.server_events(self.cfg, (q.get("limit") or ["250"])[0], kinds)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: server_events")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _api_twinks(self, h, method, q, sess):
        """Твинк-детект по IP — под админ-паролем панели (IP + связывание аккаунтов)."""
        b = self._body(h)
        ok, resp = self._reauth(h, sess, "твинк-детект по IP", body=b)
        if not ok:
            return resp
        try:
            mn = max(2, min(20, int(b.get("min_accounts") or 2)))
        except (TypeError, ValueError):
            mn = 2
        try:
            d = players.twink_report(self.cfg, mn)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: twink_report")
            return self._json(h, {"error": "internal", "detail": str(e)}, 500)
        self.audit(h.client_address[0], sess["user"],
                   "ТВИНК-ДЕТЕКТ: %d IP с >=%d аккаунтами (из %d IP)"
                   % (d.get("flagged_ips", 0), mn, d.get("ip_count", 0)))
        return self._json(h, d)

    def _api_player_inventory(self, h, pid, sess):
        """Выдать на склад / изъять из склада или инвентаря — оффлайн, под админ-паролем."""
        body = self._body(h)
        op = (body.get("op") or "").strip()
        where = (body.get("where") or "stash").strip()
        item = body.get("item")
        try:
            count = int(body.get("count") or 0)
        except (TypeError, ValueError):
            return self._json(h, {"error": "bad_count"}, 400)
        if op not in ("give", "take"):
            return self._json(h, {"error": "bad_op"}, 400)
        ok, b = self._reauth(h, sess, "правка инвентаря игрока #%s" % pid, body=body)
        if not ok:
            return b
        try:
            if op == "give":
                d = players.give_stash_items(self.cfg, pid, item, count)
            else:
                d = players.take_items(self.cfg, pid, where, item, count)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: player_inventory %s %s", op, pid)
            return self._json(h, {"error": "internal", "detail": str(e)}, 500)
        if d.get("ok"):
            self.audit(h.client_address[0], sess["user"],
                       "ИНВЕНТАРЬ игрока #%s: %s %s×%s %s (бэкап %s)"
                       % (pid, op, d.get("name") or item,
                          d.get("count") if op == "give" else d.get("removed"),
                          d.get("where"), d.get("backup")))
        else:
            self.audit(h.client_address[0], sess["user"],
                       "ИНВЕНТАРЬ игрока #%s: %s отклонено — %s" % (pid, op, d.get("error")))
        return self._json(h, d, 200 if d.get("ok") else 400)

    def _api_player_moderate(self, h, pid, sess):
        """Бан/роль/телепорт/техи/статы/сброс пароля — оффлайн, под админ-паролем."""
        body = self._body(h)
        act = (body.get("action") or "").strip()
        ACTS = {"ban", "unban", "role", "position", "tech", "stat", "reset_code"}
        if act not in ACTS:
            return self._json(h, {"error": "bad_action"}, 400)
        ok, b = self._reauth(h, sess, "модерация игрока #%s (%s)" % (pid, act), body=body)
        if not ok:
            return b
        try:
            if act == "ban":
                d = players.player_set_ban(self.cfg, pid, True, b.get("hours") or 0)
            elif act == "unban":
                d = players.player_set_ban(self.cfg, pid, False, 0)
            elif act == "role":
                d = players.player_set_role(self.cfg, pid, b.get("role"), sess["user"])
            elif act == "position":
                d = players.player_set_position(self.cfg, pid, b.get("map"), b.get("x"),
                                                b.get("y"), bool(b.get("respawn")))
            elif act == "tech":
                d = players.player_add_tech(self.cfg, pid, b.get("tech") or "")
            elif act == "stat":
                d = players.player_set_stat(self.cfg, pid, b.get("field"), b.get("value"))
            else:  # reset_code
                d = players.player_reset_code(self.cfg, pid, b.get("code") or "")
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: moderate %s %s", act, pid)
            return self._json(h, {"error": "internal", "detail": str(e)}, 500)
        summ = {"ban": "бан hours=%s" % b.get("hours"), "unban": "разбан",
                "role": "роль=%s" % b.get("role"),
                "position": "телепорт %s @ %s,%s%s" % (b.get("map"), b.get("x"), b.get("y"),
                                                       " +респавн" if b.get("respawn") else ""),
                "tech": "техи %s" % b.get("tech"),
                "stat": "%s=%s" % (b.get("field"), b.get("value")),
                "reset_code": "сброс пароля"}.get(act, act)
        if d.get("ok"):
            self.audit(h.client_address[0], sess["user"],
                       "МОДЕРАЦИЯ игрока #%s: %s (бэкап %s)" % (pid, summ, d.get("backup")))
        else:
            self.audit(h.client_address[0], sess["user"],
                       "МОДЕРАЦИЯ игрока #%s: %s отклонено — %s" % (pid, summ, d.get("error")))
        return self._json(h, d, 200 if d.get("ok") else 400)

    # --------------------------------------------------------------- screenshot
    def _api_shot(self, h, method, q, sess):
        path = os.path.join(self.cfg.get("base_dir", common.BASE_DIR), "logs", "webshot.png")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._shot_lock:
            fresh = os.path.isfile(path) and (time.time() - self._shot_ts) < SHOT_MIN_INTERVAL
            if not fresh:
                try:
                    self._shot_meta = screenshot.capture_game(self.cfg, path)
                    self._shot_ts = time.time()
                except Exception as e:  # noqa: BLE001
                    logging.warning("webui: shot: %s", e)
                    return self._json(h, {"error": "capture", "detail": str(e)}, 503)
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError as e:
                return self._json(h, {"error": "read", "detail": str(e)}, 503)
        m, sz = self._shot_meta
        return self._send(h, 200, "image/png", data, {
            "Cache-Control": "no-store",
            "X-Shot-Method": str(m),
            "X-Shot-Size": "%sx%s" % (sz[0], sz[1]),
        })

    # -------------------------------------------------------------------- logs
    def _log_path(self):
        return os.path.join(self.cfg.get("base_dir", common.BASE_DIR), "logs", "supervisor.log")

    def _api_log(self, h, method, q, sess):
        try:
            n = min(2000, max(10, int((q.get("n") or ["300"])[0])))
        except ValueError:
            n = 300
        lvl = (q.get("level") or ["ALL"])[0].upper()
        text = _tail(self._log_path(), 512 * 1024)
        if (q.get("fmt") or [""])[0] == "txt":
            return self._send(h, 200, "text/plain; charset=utf-8", text, {
                "Content-Disposition": "attachment; filename=supervisor-tail.log",
            })
        rows = _parse_log(text)
        if lvl != "ALL":
            keep = {"WARN": ("WARNING", "ERROR", "CRITICAL")}.get(lvl, (lvl,))
            rows = [r for r in rows if r["level"] in keep]
        return self._json(h, {"lines": rows[-n:], "file": self._log_path()})

    def _api_audit(self, h, method, q, sess):
        rows = []
        for ln in _tail(self._audit_path, 128 * 1024).splitlines():
            parts = ln.split("\t", 3)
            if len(parts) == 4:
                rows.append({"ts": parts[0], "ip": parts[1], "user": parts[2], "msg": parts[3]})
        return self._json(h, {"lines": rows[-400:]})

    def _nav_dir(self):
        return os.path.realpath(os.path.join(self.cfg.get("base_dir", common.BASE_DIR), "logs", "nav"))

    def _api_nav_shots(self, h, method, q, sess):
        d = self._nav_dir()
        out = []
        try:
            for nm in os.listdir(d):
                if nm.lower().endswith(".png"):
                    fp = os.path.join(d, nm)
                    out.append({"name": nm, "age": int(time.time() - os.path.getmtime(fp)),
                                "size": os.path.getsize(fp)})
        except OSError:
            pass
        out.sort(key=lambda x: x["age"])
        return self._json(h, {"shots": out})

    def _api_nav_shot(self, h, method, q, sess):
        nm = (q.get("name") or [""])[0]
        if not re.match(r"^[\w.\-]{1,64}\.png$", nm):
            return self._json(h, {"error": "bad_name"}, 400)
        d = self._nav_dir()
        fp = os.path.realpath(os.path.join(d, nm))
        if not (fp == os.path.join(d, nm) and os.path.isfile(fp)):
            return self._json(h, {"error": "not_found"}, 404)
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except OSError:
            return self._json(h, {"error": "read"}, 404)
        return self._send(h, 200, "image/png", data, {"Cache-Control": "no-store"})

    # ------------------------------------------------------------------- roles
    def _api_roles(self, h, method, q, sess):
        tg = self.cfg.get("telegram", {}) or {}
        if method == "GET":
            return self._json(h, {
                "allowed_user_ids": tg.get("allowed_user_ids", []),
                "moderator_user_ids": tg.get("moderator_user_ids", []),
                "super_admin_id": tg.get("super_admin_id"),
                "default_lang": tg.get("default_lang", "ru"),
                "alerts_enabled": bool(tg.get("alerts_enabled", True)),
            })

        b = self._body(h)

        def ids(key):
            v = b.get(key, [])
            if isinstance(v, str):
                v = [x for x in re.split(r"[\s,]+", v) if x]
            elif not isinstance(v, list):
                raise _Bad("%s: ожидается список" % key)
            out, seen = [], set()
            for x in v:
                try:
                    n = int(str(x).strip())
                except (TypeError, ValueError):
                    raise _Bad("нечисловой Telegram ID в «%s»: %r" % (key, x))
                if n not in seen:
                    seen.add(n)
                    out.append(n)
            return out

        try:
            admins = ids("allowed_user_ids")
            mods = ids("moderator_user_ids")
            if not admins:
                raise _Bad("нужен хотя бы один администратор")
            sa = b.get("super_admin_id")
            sa = int(sa) if str(sa).strip() not in ("", "None", "null") else None
            if sa is not None and sa not in admins:
                raise _Bad("super_admin_id должен быть среди администраторов")
        except _Bad as e:
            return self._json(h, {"error": "invalid", "detail": str(e)}, 400)
        except (TypeError, ValueError):
            return self._json(h, {"error": "invalid", "detail": "super_admin_id должен быть числом"}, 400)

        lang = (b.get("default_lang") or "ru").lower()
        if lang not in i18n.SUPPORTED:
            lang = "ru"
        alerts = bool(b.get("alerts_enabled", True))

        tg = self.cfg.setdefault("telegram", {})
        tg.update(allowed_user_ids=admins, moderator_user_ids=mods,
                  super_admin_id=sa, default_lang=lang, alerts_enabled=alerts)
        try:
            common.save_config(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: save_config")
            return self._json(h, {"error": "save_failed", "detail": str(e)}, 500)
        self.bot.apply_roles(tg)
        self.audit(h.client_address[0], sess["user"],
                   "роли: админы=%s модераторы=%s super=%s lang=%s alerts=%s"
                   % (admins, mods, sa, lang, alerts))
        return self._json(h, {"ok": True, "allowed_user_ids": admins, "moderator_user_ids": mods,
                              "super_admin_id": sa, "default_lang": lang, "alerts_enabled": alerts})

    # ----------------------------------------------------------------- actions
    _OPS = {"startgame", "stopgame", "restartgame", "restartsteam", "login",
            "watchdog", "restartvm", "stopbot", "restarttask", "testalert"}
    _CONFIRM = {"restartvm", "stopbot", "restarttask"}

    def _api_action(self, h, method, q, sess):
        b = self._body(h)
        op = (b.get("op") or "").strip()
        if op not in self._OPS:
            return self._json(h, {"error": "bad_op"}, 400)
        if op in self._CONFIRM and not b.get("confirm"):
            return self._json(h, {"error": "need_confirm"}, 400)
        lang = i18n.norm(b.get("lang") or self.cfg.get("telegram", {}).get("default_lang", "ru"))
        jid = secrets.token_hex(8)
        job = {"id": jid, "op": op, "done": False, "ok": None, "text": "",
               "started": time.time(), "finished": None}
        with self._jobs_lock:
            self._jobs[jid] = job
            while len(self._jobs) > 30:
                self._jobs.pop(next(iter(self._jobs)))
        ip, user = h.client_address[0], sess["user"]
        threading.Thread(target=self._run_job, args=(job, b, lang, ip, user),
                         name="webjob", daemon=True).start()
        return self._json(h, {"job": jid})

    def _api_job(self, h, method, q, sess):
        job = self._jobs.get((q.get("id") or [""])[0])
        if not job:
            return self._json(h, {"error": "no_job"}, 404)
        return self._json(h, job)

    def _run_job(self, job, b, lang, ip, user):
        op = job["op"]
        try:
            ok, text = self._do_op(op, b, lang)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: job %s", op)
            ok, text = False, "ошибка: %s" % e
        job.update(ok=bool(ok), text=str(text), done=True, finished=time.time())
        self.audit(ip, user, "%s → %s: %s" % (op, "ok" if ok else "СБОЙ", str(text)[:200]))

    def _do_op(self, op, b, lang):
        if op == "startgame":
            return self.bot._tr(lang, gamectl.start_game(self.cfg))
        if op == "stopgame":
            return self.bot._tr(lang, gamectl.stop_game(self.cfg))
        if op == "restartsteam":
            return self.bot._tr(lang, gamectl.restart_steam(self.cfg))
        if op == "restartgame":
            return self.bot._do_restart_and_login(lang)
        if op == "login":
            return self.bot._do_login(lang)
        if op == "watchdog":
            if not self.wd:
                return False, "watchdog недоступен"
            self.wd.set_enabled(bool(b.get("on")))
            return True, "watchdog " + ("включён" if self.wd.enabled else "выключен")
        if op == "restartvm":
            self.bot.push_alert(i18n.t(self.bot._default_lang, "wait.vm"))
            ok, msg = gamectl.restart_vm()
            return ok, ("VM перезагружается" if ok else "не удалось: %s" % msg)
        if op == "stopbot":
            task = self.cfg.get("task_name", "SigmaSteamBot")
            ok, msg = gamectl.disable_bot_task(task)
            txt = ("задача %s отключена — супервизор останавливается; поднять обратно только с VM"
                   % task) if ok else ("процесс останавливаю, но задачу %s не отключить: %s" % (task, msg))
            threading.Timer(1.5, self._shutdown_supervisor).start()
            return ok, txt
        if op == "restarttask":
            self._detached_task_restart(self.cfg.get("task_name", "SigmaSteamBot"))
            return True, "задача перезапускается — панель оборвётся на ~10 секунд, обновите страницу"
        if op == "testalert":
            self.bot.push_alert("🔔 Тест-алерт из веб-панели • %s" % _now_iso())
            return True, "тест-алерт поставлен в очередь отправки администраторам"
        return False, "неизвестная операция"

    def _shutdown_supervisor(self):
        logging.info("webui: /stopbot — останавливаю watchdog и бота")
        try:
            if self.wd:
                self.wd.stop()
        except Exception:  # noqa: BLE001
            logging.exception("webui: stop watchdog")
        try:
            self.bot.stop()
        except Exception:  # noqa: BLE001
            logging.exception("webui: stop bot")

    def _detached_task_restart(self, task):
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
        cmd = ('timeout /t 2 /nobreak >nul & schtasks /End /TN "%s" & '
               'timeout /t 3 /nobreak >nul & schtasks /Change /TN "%s" /ENABLE & '
               'schtasks /Run /TN "%s"') % (task, task, task)
        logging.info("webui: перезапуск задачи %s отдельным процессом", task)
        subprocess.Popen(["cmd", "/c", cmd], creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------- SPA
PAGE = r"""<!doctype html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SigmaSteamBot</title>
<style>
:root{
  --bg:#0f1216; --panel:#171c22; --panel2:#1e252d; --line:#2b333d; --fg:#e7ecf1;
  --mut:#93a1b0; --acc:#4c8dff; --ok:#3fb950; --warn:#d29922; --err:#f85149;
  --radius:10px;
}
:root[data-theme="light"]{
  --bg:#f4f6f8; --panel:#ffffff; --panel2:#eef1f4; --line:#d7dde3; --fg:#1b2229;
  --mut:#5b6670; --acc:#1f6feb; --ok:#1a7f37; --warn:#9a6700; --err:#cf222e;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
a{color:var(--acc)}
header{display:flex;align-items:center;gap:12px;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5;flex-wrap:wrap}
header h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}
header .sp{flex:1}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut);display:inline-block;margin-right:6px}
.dot.ok{background:var(--ok)} .dot.err{background:var(--err)}
button,.btn{font:inherit;color:var(--fg);background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:7px 12px;cursor:pointer}
button:hover{border-color:var(--acc)}
button:disabled{opacity:.5;cursor:not-allowed}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}
button.danger{border-color:var(--err);color:var(--err)}
button.small{padding:4px 9px;font-size:12.5px}
nav{display:flex;gap:4px;padding:8px 16px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
nav button{background:transparent;border:0;border-bottom:2px solid transparent;border-radius:0;padding:6px 10px;color:var(--mut)}
nav button.active{color:var(--fg);border-bottom-color:var(--acc)}
main{padding:16px;max-width:1100px;margin:0 auto}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:14px}
.card h3{margin:0 0 10px;font-size:12.5px;text-transform:uppercase;letter-spacing:.6px;color:var(--mut)}
.kv{display:flex;justify-content:space-between;gap:10px;padding:3px 0;border-bottom:1px dashed var(--line)}
.kv:last-child{border-bottom:0}
.kv b{font-weight:600}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;border:1px solid var(--line)}
.pill.ok{color:var(--ok);border-color:var(--ok)} .pill.err{color:var(--err);border-color:var(--err)}
.pill.warn{color:var(--warn);border-color:var(--warn)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.actions{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.actions button{padding:12px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--mut);font-weight:600}
tr.hl td{background:rgba(76,141,255,.10)}
pre.log{background:#0b0e12;border:1px solid var(--line);border-radius:8px;padding:10px;max-height:60vh;overflow:auto;font:12px/1.4 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
:root[data-theme="light"] pre.log{background:#0b0e12;color:#e7ecf1}
.lg-INFO{color:#9aa7b3} .lg-WARNING{color:var(--warn)} .lg-ERROR,.lg-CRITICAL{color:var(--err)}
.lg-t{color:#5f6b78}
input,select,textarea{font:inherit;color:var(--fg);background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px}
textarea{width:100%;min-height:70px;resize:vertical;font:12.5px/1.4 ui-monospace,Consolas,monospace}
label.fld{display:block;margin:10px 0}
label.fld span{display:block;color:var(--mut);font-size:12.5px;margin-bottom:4px}
.center{min-height:70vh;display:flex;align-items:center;justify-content:center}
.box{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:22px;width:340px;max-width:92vw}
.box h2{margin:0 0 4px} .box p.mut{color:var(--mut);margin:.2em 0 1em}
.msg{margin:10px 0;padding:9px 11px;border-radius:8px;border:1px solid var(--line);font-size:13px;white-space:pre-wrap}
.msg.ok{border-color:var(--ok);color:var(--ok)} .msg.err{border-color:var(--err);color:var(--err)}
.msg.info{border-color:var(--acc)}
.shot{width:100%;border:1px solid var(--line);border-radius:8px;background:#000;min-height:120px;object-fit:contain}
.thumbs{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
.thumbs figure{margin:0}
.thumbs img{width:100%;border:1px solid var(--line);border-radius:6px;cursor:zoom-in}
.thumbs figcaption{color:var(--mut);font-size:11.5px;margin-top:3px}
.muted{color:var(--mut)} .mono{font-family:ui-monospace,Consolas,monospace}
.hide{display:none!important}
a.pl-link{color:var(--acc);cursor:pointer;text-decoration:none}
a.pl-link:hover{text-decoration:underline}
.ovl{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:flex-start;justify-content:center;padding:24px 12px;overflow:auto;z-index:20}
.dlg{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);width:1120px;max-width:100%;padding:0}
.dlg header{position:sticky;top:0;background:var(--panel);border-bottom:1px solid var(--line);border-radius:var(--radius) var(--radius) 0 0}
.dlg .bd{padding:14px}
.dlg .grid{grid-template-columns:repeat(3,minmax(0,1fr))}
@media(max-width:760px){.dlg .grid{grid-template-columns:1fr}}
.chips{display:flex;flex-wrap:wrap;gap:4px}
.chip{font-size:11.5px;padding:1px 7px;border:1px solid var(--line);border-radius:20px;color:var(--mut)}
.bar{position:relative;height:14px;background:var(--panel2);border:1px solid var(--line);border-radius:7px;overflow:hidden;min-width:90px}
.bar>span{position:absolute;inset:0 auto 0 0;background:var(--acc);opacity:.55}
.bar>b{position:absolute;inset:0;text-align:center;font-size:10.5px;font-weight:500;line-height:14px}
.spark{display:flex;align-items:flex-end;gap:1px;height:38px}
.spark i{flex:1;background:var(--acc);opacity:.5;min-height:1px}
@media(max-width:560px){main{padding:10px}}
</style>
</head>
<body>
<div id="app"></div>
<script>
"use strict";
var S = { authed:false, csrf:"", user:"", must_change:false, lang:localStorage.getItem("sw_lang")||"ru",
          tab:localStorage.getItem("sw_tab")||"dash", conn:null };
var T = {
 ru:{ title:"SigmaSteamBot", logout:"Выход", login:"Войти", user:"Пользователь", pass:"Пароль",
  dash:"Дашборд", act:"Действия", srv:"Серверы", chat:"Чат", stats:"Статы", players:"Игроки", twinks:"Твинки", roles:"Роли", logs:"Логи",
  sc_server:"Чат сервера", sc_events:"События", sc_private:"Приваты", sc_all:"все каналы",
  sc_search:"поиск", ev_join:"вошёл", ev_leave:"вышел", ev_register:"регистрация",
  ev_death:"смерть", ev_land:"снос земли", ev_kind:"тип", sc_priv_note:"Все приватные сообщения сервера — под паролем панели.",
  refresh:"Обновить", live:"Живой опрос", auto:"Авто",
  vm:"VM", steam:"Steam", game:"Игра", wd:"Watchdog", internals:"Внутренности бота", monitor:"Монитор сервера",
  uptime:"Аптайм", cpu:"CPU", ram:"RAM", disk:"Диск C:", session:"Сессия",
  running:"работает", stopped:"не запущен", account:"аккаунт", signedin:"вход в аккаунт",
  yes:"да", no:"нет", unknown:"?", pid:"PID", window:"Окно", loginstate:"Состояние входа",
  ls_done:"в игре", ls_running:"вход выполняется…", ls_idle:"в меню",
  restarts:"перезапусков (игра/Steam)", lastrestart:"последний перезапуск",
  threads:"потоков", outbox:"очередь отправки", lastpoll:"посл. опрос Telegram", snapage:"возраст снапшота",
  present:"в списке", misses:"промахов", console:"консоль активна", rdp:"RDP подключён", nosess:"нет сессии",
  screenshot:"Скриншот экрана VM",
  a_startgame:"Запустить игру", a_stopgame:"Остановить игру", a_restartgame:"Перезапуск игры + вход",
  a_restartsteam:"Перезапуск Steam", a_login:"Войти в игру", a_wd_on:"Watchdog включить",
  a_wd_off:"Watchdog выключить", a_restartvm:"Перезагрузить VM", a_stopbot:"Остановить бота",
  a_restarttask:"Перезапустить задачу бота", a_testalert:"Тест-алерт в Telegram",
  confirm:"Подтвердите действие", cancel:"Отмена", ok:"OK", working:"выполняется…",
  srv_head:"Публичные серверы (Steam-лобби)", srv_none:"Серверов сейчас нет", srv_src:"источник",
  col_name:"Сервер", col_players:"Игроки", col_map:"Мир", col_ver:"Версия", col_addr:"Адрес", col_mem:"В лобби",
  roles_admins:"Администраторы (Telegram ID)", roles_mods:"Модераторы (Telegram ID)",
  roles_super:"Главный админ (super_admin_id)", roles_lang:"Язык бота по умолчанию",
  roles_alerts:"Алерты в Telegram включены", roles_hint:"ID через запятую/пробел/с новой строки. ID из обоих списков считается администратором. Нужен ≥1 админ. Главный админ должен быть среди администраторов.",
  save:"Сохранить", saved:"Сохранено, роли применены на лету",
  log_sup:"Супервизор", log_audit:"Аудит панели", log_nav:"Вход в игру (скрины)",
  level:"Уровень", lines:"строк", download:"Скачать", navshots_none:"Скринов последовательности входа нет",
  chpass_title:"Смена пароля", chpass_note:"Вход по умолчанию admin/admin. Смените пароль сейчас — минимум 6 символов, не «admin».",
  chpass_old:"Текущий пароль", chpass_new:"Новый пароль", chpass_rep:"Повторите новый пароль",
  chpass_mismatch:"Пароли не совпадают", change:"Сменить пароль",
  err_bad_credentials:"Неверный логин или пароль", err_throttled:"Слишком много попыток, подождите",
  err_bad_old:"Текущий пароль неверный", err_too_short:"Минимум 6 символов", err_too_weak:"Слишком простой пароль",
  err_auth:"Сессия истекла — войдите заново", err_net:"Нет связи с сервером",
  pl_head:"Игроки локального сервера", pl_world:"Мир", pl_registered:"зарегистрировано",
  pl_online:"онлайн (по аналитике)", pl_online_gs:"онлайн (game_state)", pl_bymap:"По картам",
  pl_only_online:"Только онлайн", pl_search:"поиск по имени",
  pl_col_status:"Статус", pl_col_map:"Карта", pl_col_pos:"Коорд.",
  pl_col_enter:"Вход", pl_col_exit:"Выход", pl_col_sess:"Сессия",
  pl_col_hours:"Часов", pl_col_lvl:"Ур.", pl_col_role:"Роль", pl_col_ban:"Бан",
  pl_role_player:"игрок", pl_role_staff:"стафф", pl_role_mod:"модератор", pl_role_admin:"админ", pl_role_gm:"GM",
  pl_recent:"Последние события",
  pl_ev_register:"зарегистрировался", pl_ev_enter:"вошёл", pl_ev_exit:"вышел",
  pl_none:"Данных о игроках нет", pl_map:"карта",
  tw_intro:"Аккаунты, заходившие с одного IP (Logs\\log_net_ip.txt). Чувствительно — под паролем панели.",
  tw_prompt:"Подтвердите своим паролем от панели:", tw_min:"мин. аккаунтов на IP",
  tw_show:"Показать", tw_none:"Совпадений нет", tw_summary:"IP всего",
  tw_flagged:"помечено IP", tw_connects:"подкл.", tw_other_ips:"ещё IP", tw_ignored:"игнор",
  st_online:"Онлайн (7 дней)", st_now:"сейчас", st_peak:"пик 7д", st_growth:"Рост",
  st_reg:"рег.", st_dau:"актив/день", st_ret:"retention", st_toplvl:"Топ по уровню",
  st_toptime:"Топ по часам", st_clans:"Кланы", st_month:"Топ месяца", st_bans:"Бан-лист",
  st_staff:"Стафф", st_lvldist:"Уровни", st_countries:"Страны", st_hist:"история ролей",
  st_world:"Мир · карты", st_terr:"Территории", st_owner:"владелец", st_avatars:"аватары", st_terrfilter:"карта",
  hh_ready:"Сервер запущен", hh_startup:"старт, мс", hh_mem:"managed МБ", hh_clusters:"кластеры",
  hh_slowphase:"медленные фазы старта", hh_lag:"Лаг-события (медленные тики)", hh_lagday:"в день",
  hh_byfunc:"по функциям", hh_connerr:"Ошибки коннекта",
  ex_title:"Экспорт / бэкап", ex_csv:"Игроки → CSV", ex_bstate:"Бэкап мира (state)",
  ex_bfull:"Бэкап (всё, большой)", ex_wait:"собираю архив…",
  pd_profile:"Профиль", pd_research:"Исследования", pd_missions:"Миссии", pd_position:"Позиция",
  pd_avatar:"Аватар", pd_sessions:"Сессии", pd_clan:"Клан", pd_close:"Закрыть",
  pd_level:"Уровень", pd_country:"Страна", pd_video:"Видеокарта", pd_screen:"Экран",
  pd_rating:"Рейтинг", pd_playtime:"Всего часов", pd_first_seen:"Первый вход",
  pd_last_seen:"Последняя сессия", pd_ban_until:"Бан истекает через", pd_ban_perm:"заблокирован",
  pd_res_cur:"Изучает", pd_res_left:"осталось", pd_res_done:"изучено техов", pd_booster:"бустер",
  pd_mission_cur:"Текущая миссия", pd_mission_month:"месяц",
  pd_respawn:"Респавн", pd_territories:"Территории", pd_coords:"Коорд.",
  pd_species:"Вид", pd_gender:"Пол", pd_grown:"взрослый", pd_params:"Статы", pd_skills:"Навыки",
  pd_abilities:"Способности", pd_buffs:"Баффы", pd_stash:"Склад", pd_carry:"При себе", pd_items:"предм.",
  pd_sess_total:"Всего сессий", pd_sess_hours:"часов онлайн", pd_sess_avg:"средняя",
  pd_sess_max:"макс", pd_sess_byhour:"Активность по часам суток", pd_sess_recent:"Последние сессии",
  pd_show_code:"Показать пароль", pd_code_prompt:"Подтвердите своим паролем от панели:",
  pd_code_btn:"Показать", pd_code_bad:"Неверный пароль", pd_min:"мин", pd_h_ago:"ч назад",
  pd_activity:"История", pd_deaths:"Смерти / сбросы", pd_roles:"Смены ролей",
  pd_lands:"Снос земель", pd_rewards:"Награды (месяц)", pd_chat:"Чат игрока",
  pd_chat_none:"нет публичных сообщений", pd_sens_btn:"Приваты и IP",
  pd_priv:"Приватные сообщения", pd_ips:"История IP", pd_priv_none:"нет приватных сообщений",
  pd_dev_kill:"смерть", pd_dev_reset_position:"сброс позиции", pd_role_to:"→ роль", pd_role_by:"выдал",
  pd_friends:"Друзья", pd_clan_rating:"рейтинг клана", pd_clan_slots:"мест",
  pd_inv_edit:"Правка инвентаря (только оффлайн)", pd_inv_online:"игрок сейчас онлайн — правка недоступна",
  pd_inv_give:"Выдать на склад", pd_inv_take:"Изъять", pd_inv_item:"предмет: имя или id",
  pd_inv_count:"кол-во", pd_inv_from:"откуда", pd_inv_carry:"при себе",
  pd_mod:"Модерация (оффлайн)", pd_mod_ban:"Забанить", pd_mod_unban:"Разбанить",
  pd_mod_tp:"Телепорт", pd_mod_givetech:"Выдать техи", pd_mod_resetpw:"Сброс пароля игрока",
  pd_mod_newcode:"новый пароль игрока",
  pd_p0:"Энергия", pd_p1:"Сытость", pd_p2:"Здоровье", pd_p3:"Стамина", pd_lp0:"Очки иссл.", pd_lp1:"Уровень", pd_lp2:"",
  ago:"назад", never:"нет данных", n_a:"н/д" },
 en:{ title:"SigmaSteamBot", logout:"Log out", login:"Log in", user:"Username", pass:"Password",
  dash:"Dashboard", act:"Actions", srv:"Servers", chat:"Chat", stats:"Stats", players:"Players", twinks:"Twinks", roles:"Roles", logs:"Logs",
  sc_server:"Server chat", sc_events:"Events", sc_private:"DMs", sc_all:"all channels",
  sc_search:"search", ev_join:"joined", ev_leave:"left", ev_register:"registered",
  ev_death:"death", ev_land:"land removed", ev_kind:"type", sc_priv_note:"All server private messages — behind the panel password.",
  refresh:"Refresh", live:"Live poll", auto:"Auto",
  vm:"VM", steam:"Steam", game:"Game", wd:"Watchdog", internals:"Bot internals", monitor:"Server monitor",
  uptime:"Uptime", cpu:"CPU", ram:"RAM", disk:"Disk C:", session:"Session",
  running:"running", stopped:"not running", account:"account", signedin:"signed in",
  yes:"yes", no:"no", unknown:"?", pid:"PID", window:"Window", loginstate:"Login state",
  ls_done:"in game", ls_running:"logging in…", ls_idle:"at menu",
  restarts:"restarts (game/Steam)", lastrestart:"last restart",
  threads:"threads", outbox:"send queue", lastpoll:"last Telegram poll", snapage:"snapshot age",
  present:"in list", misses:"misses", console:"console active", rdp:"RDP connected", nosess:"no session",
  screenshot:"VM screen screenshot",
  a_startgame:"Start game", a_stopgame:"Stop game", a_restartgame:"Restart game + log in",
  a_restartsteam:"Restart Steam", a_login:"Log into game", a_wd_on:"Enable watchdog",
  a_wd_off:"Disable watchdog", a_restartvm:"Reboot VM", a_stopbot:"Stop bot",
  a_restarttask:"Restart bot task", a_testalert:"Test alert to Telegram",
  confirm:"Confirm action", cancel:"Cancel", ok:"OK", working:"working…",
  srv_head:"Public servers (Steam lobbies)", srv_none:"No servers right now", srv_src:"source",
  col_name:"Server", col_players:"Players", col_map:"World", col_ver:"Version", col_addr:"Address", col_mem:"In lobby",
  roles_admins:"Administrators (Telegram IDs)", roles_mods:"Moderators (Telegram IDs)",
  roles_super:"Super admin (super_admin_id)", roles_lang:"Default bot language",
  roles_alerts:"Telegram alerts enabled", roles_hint:"IDs separated by comma / space / newline. An ID in both lists counts as admin. At least one admin required. Super admin must be one of the admins.",
  save:"Save", saved:"Saved, roles applied live",
  log_sup:"Supervisor", log_audit:"Panel audit", log_nav:"In-game login (shots)",
  level:"Level", lines:"lines", download:"Download", navshots_none:"No login-sequence screenshots",
  chpass_title:"Change password", chpass_note:"Default login is admin/admin. Change it now — at least 6 characters, not \"admin\".",
  chpass_old:"Current password", chpass_new:"New password", chpass_rep:"Repeat new password",
  chpass_mismatch:"Passwords do not match", change:"Change password",
  err_bad_credentials:"Wrong username or password", err_throttled:"Too many attempts, wait a bit",
  err_bad_old:"Current password is wrong", err_too_short:"At least 6 characters", err_too_weak:"Password too weak",
  err_auth:"Session expired — log in again", err_net:"No connection to server",
  pl_head:"Local server players", pl_world:"World", pl_registered:"registered",
  pl_online:"online (analytics)", pl_online_gs:"online (game_state)", pl_bymap:"By map",
  pl_only_online:"Online only", pl_search:"search by name",
  pl_col_status:"Status", pl_col_map:"Map", pl_col_pos:"Coords",
  pl_col_enter:"Enter", pl_col_exit:"Exit", pl_col_sess:"Session",
  pl_col_hours:"Hours", pl_col_lvl:"Lvl", pl_col_role:"Role", pl_col_ban:"Ban",
  pl_role_player:"player", pl_role_staff:"staff", pl_role_mod:"moderator", pl_role_admin:"admin", pl_role_gm:"GM",
  pl_recent:"Recent events",
  pl_ev_register:"registered", pl_ev_enter:"entered", pl_ev_exit:"left",
  pl_none:"No player data", pl_map:"map",
  tw_intro:"Accounts that connected from the same IP (Logs\\log_net_ip.txt). Sensitive — behind the panel password.",
  tw_prompt:"Confirm with your panel password:", tw_min:"min accounts per IP",
  tw_show:"Show", tw_none:"No matches", tw_summary:"IPs total",
  tw_flagged:"flagged IPs", tw_connects:"conn.", tw_other_ips:"more IPs", tw_ignored:"ignored",
  st_online:"Online (7 days)", st_now:"now", st_peak:"7d peak", st_growth:"Growth",
  st_reg:"reg.", st_dau:"active/day", st_ret:"retention", st_toplvl:"Top by level",
  st_toptime:"Top by hours", st_clans:"Clans", st_month:"Month top", st_bans:"Ban list",
  st_staff:"Staff", st_lvldist:"Levels", st_countries:"Countries", st_hist:"role history",
  st_world:"World · maps", st_terr:"Territories", st_owner:"owner", st_avatars:"avatars", st_terrfilter:"map",
  hh_ready:"Server started", hh_startup:"startup ms", hh_mem:"managed MB", hh_clusters:"clusters",
  hh_slowphase:"slow startup phases", hh_lag:"Lag events (slow ticks)", hh_lagday:"per day",
  hh_byfunc:"by function", hh_connerr:"Connection errors",
  ex_title:"Export / backup", ex_csv:"Players → CSV", ex_bstate:"World backup (state)",
  ex_bfull:"Backup (full, large)", ex_wait:"building archive…",
  pd_profile:"Profile", pd_research:"Research", pd_missions:"Missions", pd_position:"Position",
  pd_avatar:"Avatar", pd_sessions:"Sessions", pd_clan:"Clan", pd_close:"Close",
  pd_level:"Level", pd_country:"Country", pd_video:"GPU", pd_screen:"Screen",
  pd_rating:"Rating", pd_playtime:"Total hours", pd_first_seen:"First seen",
  pd_last_seen:"Last session", pd_ban_until:"Ban expires in", pd_ban_perm:"blocked",
  pd_res_cur:"Researching", pd_res_left:"left", pd_res_done:"techs done", pd_booster:"booster",
  pd_mission_cur:"Current mission", pd_mission_month:"month",
  pd_respawn:"Respawn", pd_territories:"Territories", pd_coords:"Coords",
  pd_species:"Species", pd_gender:"Gender", pd_grown:"grown", pd_params:"Stats", pd_skills:"Skills",
  pd_abilities:"Abilities", pd_buffs:"Buffs", pd_stash:"Stash", pd_carry:"Carried", pd_items:"items",
  pd_sess_total:"Total sessions", pd_sess_hours:"hours online", pd_sess_avg:"avg",
  pd_sess_max:"max", pd_sess_byhour:"Activity by hour of day", pd_sess_recent:"Recent sessions",
  pd_show_code:"Show password", pd_code_prompt:"Confirm with your panel password:",
  pd_code_btn:"Show", pd_code_bad:"Wrong password", pd_min:"min", pd_h_ago:"h ago",
  pd_activity:"History", pd_deaths:"Deaths / resets", pd_roles:"Role changes",
  pd_lands:"Land removals", pd_rewards:"Rewards (month)", pd_chat:"Player chat",
  pd_chat_none:"no public messages", pd_sens_btn:"DMs & IP",
  pd_priv:"Private messages", pd_ips:"IP history", pd_priv_none:"no private messages",
  pd_dev_kill:"death", pd_dev_reset_position:"position reset", pd_role_to:"→ role", pd_role_by:"granted by",
  pd_friends:"Friends", pd_clan_rating:"clan rating", pd_clan_slots:"slots",
  pd_inv_edit:"Edit inventory (offline only)", pd_inv_online:"player is online — editing disabled",
  pd_inv_give:"Give to stash", pd_inv_take:"Take", pd_inv_item:"item: name or id",
  pd_inv_count:"qty", pd_inv_from:"from", pd_inv_carry:"carried",
  pd_mod:"Moderation (offline)", pd_mod_ban:"Ban", pd_mod_unban:"Unban",
  pd_mod_tp:"Teleport", pd_mod_givetech:"Grant tech", pd_mod_resetpw:"Reset player password",
  pd_mod_newcode:"new player password",
  pd_p0:"Energy", pd_p1:"Hunger", pd_p2:"Health", pd_p3:"Stamina", pd_lp0:"Research pts", pd_lp1:"Level", pd_lp2:"",
  ago:"ago", never:"no data", n_a:"n/a" }
};
function t(k){ return (T[S.lang]&&T[S.lang][k]) || (T.ru[k]) || k; }
var $=function(s,r){return (r||document).querySelector(s)};
function el(tag,attrs,kids){ var e=document.createElement(tag); attrs=attrs||{};
  for(var k in attrs){ if(k==="class")e.className=attrs[k]; else if(k==="html")e.innerHTML=attrs[k];
    else if(k.slice(0,2)==="on")e.addEventListener(k.slice(2),attrs[k]); else if(attrs[k]!=null)e.setAttribute(k,attrs[k]); }
  (kids||[]).forEach(function(c){ if(c==null)return; e.appendChild(typeof c==="string"?document.createTextNode(c):c); });
  return e; }

// ---- api ----
function api(path, opts){
  opts=opts||{}; opts.headers=opts.headers||{};
  if(opts.body!=null){ opts.headers["Content-Type"]="application/json"; opts.method=opts.method||"POST"; opts.body=JSON.stringify(opts.body); }
  if((opts.method||"GET")!=="GET") opts.headers["X-CSRF-Token"]=S.csrf;
  return fetch(path,opts).then(function(r){
    setConn(true);
    if(r.status===401){ S.authed=false; render(); throw {err:"auth"}; }
    var ct=r.headers.get("Content-Type")||"";
    if(ct.indexOf("application/json")<0) return r;
    return r.json().then(function(j){ if(!r.ok) throw j; return j; });
  }).catch(function(e){ if(e&&e.err==="auth") throw e; if(e instanceof TypeError){ setConn(false); throw {err:"net"}; } throw e; });
}
function setConn(ok){ S.conn=ok; var d=$("#conn"); if(d) d.className="dot "+(ok?"ok":"err"); }
function errText(e){ if(!e) return t("err_net"); if(e.err==="net") return t("err_net");
  var k="err_"+(e.error||e.err||""); return T[S.lang][k]||e.detail||e.error||t("err_net"); }

// ---- helpers ----
function fdur(s){ if(s==null) return t("n_a"); s=Math.floor(s); var d=Math.floor(s/86400);s%=86400;
  var h=Math.floor(s/3600);s%=3600; var m=Math.floor(s/60); s%=60;
  if(d) return d+"d "+h+"h "+m+"m"; if(h) return h+"h "+m+"m"; if(m) return m+"m "+s+"s"; return s+"s"; }
function fbytes(n){ if(n==null) return t("n_a"); var u=["B","KB","MB","GB","TB"],i=0; n=+n;
  while(n>=1024&&i<u.length-1){n/=1024;i++;} return (i?n.toFixed(1):n.toFixed(0))+" "+u[i]; }
function fago(s){ return s==null? t("never") : fdur(s)+" "+t("ago"); }
function pill(ok,txt,warn){ return el("span",{class:"pill "+(ok?"ok":(warn?"warn":"err"))},[txt]); }

// ---- shell ----
function render(){
  clearInterval(dashTimer); clearInterval(logTimer); clearInterval(plTimer); clearInterval(chTimer);
  var app=$("#app"); app.innerHTML="";
  if(!S.authed){ app.appendChild(viewLogin()); return; }
  if(S.must_change){ app.appendChild(viewChpass()); return; }
  app.appendChild(shell());
  routeTab();
}
function header(){
  var langBtn=el("button",{class:"small",onclick:function(){ S.lang=S.lang==="ru"?"en":"ru"; localStorage.setItem("sw_lang",S.lang); render(); }},[S.lang==="ru"?"EN":"RU"]);
  var thBtn=el("button",{class:"small",title:"theme",onclick:toggleTheme},["◐"]);
  var out=[ el("span",{id:"conn",class:"dot "+(S.conn===false?"err":(S.conn?"ok":""))}),
            el("h1",{},[t("title")]), el("span",{class:"sp"}),
            el("span",{class:"muted small"},[S.user||""]), langBtn, thBtn,
            el("button",{class:"small",onclick:doLogout},[t("logout")]) ];
  return el("header",{},out);
}
function shell(){
  var tabs=["dash","act","srv","chat","stats","players","twinks","roles","logs"];
  var nav=el("nav",{}, tabs.map(function(id){
    return el("button",{class:S.tab===id?"active":"",onclick:function(){ S.tab=id; localStorage.setItem("sw_tab",id); render(); }},[t(id)]);
  }));
  return el("div",{},[ header(), nav, el("main",{id:"view"},[]) ]);
}
function routeTab(){ var v=$("#view"); v.innerHTML="";
  ({dash:tabDash,act:tabAct,srv:tabSrv,chat:tabChat,stats:tabStats,players:tabPlayers,twinks:tabTwinks,roles:tabRoles,logs:tabLogs}[S.tab]||tabDash)(v); }
function toggleTheme(){ var r=document.documentElement; var cur=r.getAttribute("data-theme")==="light"?"dark":"light";
  r.setAttribute("data-theme",cur); localStorage.setItem("sw_theme",cur); }

// ---- login ----
function viewLogin(){
  var box=el("div",{class:"box"},[
    el("h2",{},[t("login")]), el("p",{class:"mut"},["SigmaSteamBot"]),
    el("label",{class:"fld"},[el("span",{},[t("user")]), el("input",{id:"lu",autocomplete:"username",value:"admin"})]),
    el("label",{class:"fld"},[el("span",{},[t("pass")]), el("input",{id:"lp",type:"password",autocomplete:"current-password"})]),
    el("div",{id:"lmsg"}),
    el("button",{class:"pri",style:"width:100%;margin-top:6px",onclick:doLogin},[t("login")])
  ]);
  box.addEventListener("keydown",function(e){ if(e.key==="Enter") doLogin(); });
  return el("div",{class:"center"},[box]);
}
function doLogin(){
  var u=$("#lu").value.trim(), p=$("#lp").value;
  api("/api/login",{body:{username:u,password:p}}).then(function(j){
    S.authed=true; S.user=j.username; S.csrf=j.csrf; S.must_change=!!j.must_change; render();
  }).catch(function(e){ var m=$("#lmsg"); if(m) m.innerHTML=""; if(m) m.appendChild(el("div",{class:"msg err"},[errText(e)])); });
}
function doLogout(){ api("/api/logout",{method:"POST"}).finally(function(){ S.authed=false; S.csrf=""; render(); }); }

// ---- change password ----
function viewChpass(){
  var box=el("div",{class:"box"},[
    el("h2",{},[t("chpass_title")]),
    el("p",{class:"mut"},[t("chpass_note")]),
    el("label",{class:"fld"},[el("span",{},[t("chpass_old")]), el("input",{id:"po",type:"password",value:"admin"})]),
    el("label",{class:"fld"},[el("span",{},[t("chpass_new")]), el("input",{id:"pn",type:"password"})]),
    el("label",{class:"fld"},[el("span",{},[t("chpass_rep")]), el("input",{id:"pr",type:"password"})]),
    el("div",{id:"pmsg"}),
    el("button",{class:"pri",style:"width:100%",onclick:doChpass},[t("change")]),
    el("div",{style:"text-align:center;margin-top:10px"},[el("a",{href:"#",onclick:function(e){e.preventDefault();doLogout();}},[t("logout")])])
  ]);
  return el("div",{class:"center"},[box]);
}
function doChpass(){
  var o=$("#po").value,n=$("#pn").value,r=$("#pr").value,m=$("#pmsg"); m.innerHTML="";
  if(n!==r){ m.appendChild(el("div",{class:"msg err"},[t("chpass_mismatch")])); return; }
  api("/api/password",{body:{old:o,new:n}}).then(function(){
    S.must_change=false; m.appendChild(el("div",{class:"msg ok"},["OK"])); setTimeout(render,500);
  }).catch(function(e){ m.appendChild(el("div",{class:"msg err"},[errText(e)])); });
}

// ---- dashboard ----
var dashTimer=null;
function tabDash(v){
  var wrap=el("div",{},[
    el("div",{class:"row",style:"margin-bottom:12px"},[
      el("button",{class:"small",onclick:function(){ loadState(true); }},[t("refresh")+" ("+t("live")+")"]),
      el("label",{class:"small"},[el("input",{type:"checkbox",id:"dauto",checked:"checked"})," "+t("auto")])
    ]),
    el("div",{id:"cards",class:"grid"},[]),
    el("div",{class:"card",style:"margin-top:12px"},[
      el("h3",{},[t("screenshot")]),
      el("div",{class:"row",style:"margin-bottom:8px"},[
        el("button",{class:"small",onclick:refreshShot},[t("refresh")]),
        el("label",{class:"small"},[el("input",{type:"checkbox",id:"sauto"})," "+t("auto")+" 20s"])
      ]),
      el("img",{class:"shot",id:"shot",alt:"screenshot"}),
      el("div",{id:"shoterr",class:"muted small"},[])
    ])
  ]);
  v.appendChild(wrap);
  loadState(false); refreshShot();
  clearInterval(dashTimer);
  dashTimer=setInterval(function(){
    if(document.hidden||S.tab!=="dash"){ return; }
    if($("#dauto")&&$("#dauto").checked) loadState(false);
    if($("#sauto")&&$("#sauto").checked) refreshShot();
  },5000);
}
function loadState(live){
  api("/api/state"+(live?"?live=1":"")).then(function(j){ drawCards(j); }).catch(function(){});
}
function drawCards(j){
  var c=$("#cards"); if(!c) return; c.innerHTML="";
  var s=j.snapshot||{}, g=s.game||{}, st=s.steam||{}, ic=j.internals||{}, mon=ic.monitor||{};
  function card(title,rows){ return el("div",{class:"card"},[el("h3",{},[title])].concat(rows.map(function(r){
    return el("div",{class:"kv"},[el("span",{},[r[0]]), (typeof r[1]==="string"?el("b",{},[r[1]]):r[1])]); }))); }
  var sess = s.rdp_connected? t("rdp") : (s.console_active? t("console") : t("nosess"));
  c.appendChild(card(t("vm"),[
    [t("uptime"), fdur(s.uptime_seconds)],
    [t("cpu"), (s.cpu_percent!=null?Math.round(s.cpu_percent):"?")+"%  ("+(s.cpu_count||"?")+")"],
    [t("ram"), (s.mem?Math.round(s.mem.percent)+"%  "+fbytes(s.mem.used)+" / "+fbytes(s.mem.total):"?")],
    [t("disk"), (s.disk_c?fbytes(s.disk_c.free)+" free ("+Math.round(s.disk_c.percent)+"%)":"?")],
    [t("session"), sess],
    [j.live?"live":(t("snapage")), j.live? pill(true,"live") : fago(ic.snapshot_age)]
  ]));
  c.appendChild(card(t("steam"),[
    ["", st.running? pill(true,t("running")) : pill(false,t("stopped"))],
    [t("account"), st.account||"?"],
    [t("signedin"), st.logged_in===true? t("yes") : (st.logged_in===false? t("no") : t("unknown"))]
  ]));
  var lsMap={done:t("ls_done"),running:t("ls_running"),idle:t("ls_idle")};
  var grows=[["", g.running? pill(true,t("running")) : pill(false,t("stopped"))]];
  if(g.running){
    grows.push([t("loginstate"), lsMap[j.login_state]||t("unknown")]);
    grows.push([t("pid"), String(g.pid||"?")+"  "+fdur(g.run_seconds)]);
    grows.push([t("ram")+" / "+t("cpu"), fbytes(g.rss)+"  "+(g.cpu!=null?Math.round(g.cpu):0)+"%"]);
    if(g.window_title) grows.push([t("window"), g.window_title]);
  }
  c.appendChild(card(t("game"),grows));
  var cn=(s.counters||{});
  c.appendChild(card(t("wd"),[
    ["", j.watchdog_enabled? pill(true,"on") : pill(false,"off",true)],
    [t("restarts"), (cn.game_restarts||0)+" / "+(cn.steam_restarts||0)],
    [t("lastrestart"), cn.last_restart_ts? new Date(cn.last_restart_ts*1000).toLocaleString() : "—"]
  ]));
  c.appendChild(card(t("internals"),[
    [t("uptime"), fdur(ic.proc_uptime)],
    [t("threads"), String(ic.threads||"?")],
    [t("outbox"), String(ic.outbox||0)],
    [t("lastpoll"), fago(ic.last_poll_ok_age)]
  ]));
  c.appendChild(card(t("monitor"),[
    ["", (mon.name||"?")],
    [t("present"), mon.present===true? pill(true,t("yes")) : (mon.present===false? pill(false,t("no"),true) : t("unknown"))],
    [t("misses"), String(mon.misses||0)+(mon.alerted?"  ⚠":"")]
  ]));
}
function refreshShot(){
  var img=$("#shot"); if(!img) return; var e=$("#shoterr"); if(e) e.textContent="";
  var url="/api/shot?_="+Date.now();
  fetch(url).then(function(r){
    if(r.ok) return r.blob().then(function(b){ img.src=URL.createObjectURL(b);
      var m=r.headers.get("X-Shot-Method"); if(e) e.textContent=(m||"")+" "+(r.headers.get("X-Shot-Size")||""); });
    return r.json().then(function(j){ if(e) e.textContent="⚠ "+(j.detail||j.error||"capture failed"); });
  }).catch(function(){ if(e) e.textContent=t("err_net"); });
}

// ---- actions ----
function tabAct(v){
  var defs=[
    ["startgame","a_startgame",0], ["stopgame","a_stopgame",0],
    ["restartgame","a_restartgame",0], ["restartsteam","a_restartsteam",0],
    ["login","a_login",0], ["watchdog","a_wd_on",0,{on:true}], ["watchdog","a_wd_off",0,{on:false}],
    ["testalert","a_testalert",0],
    ["restartvm","a_restartvm",1], ["restarttask","a_restarttask",1], ["stopbot","a_stopbot",1]
  ];
  var grid=el("div",{class:"actions"}, defs.map(function(d){
    var cls=d[2]?"danger":""; if(d[0]==="restartgame"||d[0]==="login") cls="pri";
    return el("button",{class:cls,onclick:function(){ runAction(d[0], d[3]||{}, d[2], t(d[1])); }},[t(d[1])]);
  }));
  var out=el("div",{id:"actout"},[]);
  v.appendChild(el("div",{},[grid, out]));
}
function runAction(op, extra, needConfirm, label){
  if(needConfirm && !window.confirm(t("confirm")+":\n"+label)) return;
  var body=Object.assign({op:op, lang:S.lang, confirm:needConfirm?true:undefined}, extra);
  var out=$("#actout"); out.innerHTML="";
  var m=el("div",{class:"msg info"},[label+" — "+t("working")]); out.appendChild(m);
  api("/api/action",{body:body}).then(function(j){ pollJob(j.job, m); })
    .catch(function(e){ m.className="msg err"; m.textContent=errText(e); });
}
function pollJob(id,m){
  var iv=setInterval(function(){
    api("/api/job?id="+id).then(function(j){
      if(!j.done){ return; }
      clearInterval(iv);
      m.className="msg "+(j.ok?"ok":"err");
      m.textContent=(j.ok?"✅ ":"🔴 ")+j.text;
      if(S.tab==="dash") loadState(false);
    }).catch(function(){ clearInterval(iv); m.className="msg err"; m.textContent=t("err_net"); });
  },1500);
}

// ---- servers ----
function tabSrv(v){
  var out=el("div",{id:"srvout"},[el("p",{class:"muted"},["…"])]);
  v.appendChild(el("div",{},[ el("div",{class:"row",style:"margin-bottom:10px"},[
    el("button",{class:"small",onclick:loadSrv},[t("refresh")]) ]), out ]));
  loadSrv();
}
function loadSrv(){
  api("/api/servers").then(function(j){
    var out=$("#srvout"); out.innerHTML="";
    if(!j.ok){ out.appendChild(el("div",{class:"msg err"},[j.error||"error"])); return; }
    if(!j.servers.length){ out.appendChild(el("p",{class:"muted"},[t("srv_none")])); return; }
    var rows=j.servers.slice().sort(function(a,b){
      var ah=/astralsigma/.test((a.name||"").toLowerCase().replace(/ /g,""));
      var bh=/astralsigma/.test((b.name||"").toLowerCase().replace(/ /g,""));
      if(ah!==bh) return ah?-1:1; return (b.players||0)-(a.players||0);
    });
    var tb=el("table",{},[ el("tr",{},[t("col_name"),t("col_players"),t("col_map"),t("col_ver"),t("col_addr"),t("col_mem")].map(function(x){return el("th",{},[x]);})) ]);
    rows.forEach(function(s){
      var hl=/astralsigma/.test((s.name||"").toLowerCase().replace(/ /g,""));
      tb.appendChild(el("tr",{class:hl?"hl":""},[
        el("td",{},[(hl?"👑 ":"")+(s.name||"?")]),
        el("td",{},[(s.players||0)+" / "+(s.max_players||0)]),
        el("td",{},[s.map||"—"]), el("td",{},[s.version?("v"+s.version):"—"]),
        el("td",{class:"mono"},[s.addr||"—"]), el("td",{},[s.members!=null?String(s.members):"—"])
      ]));
    });
    out.appendChild(tb);
    out.appendChild(el("p",{class:"muted small",style:"margin-top:8px"},[
      j.servers.length+" • "+t("srv_src")+": "+j.source+" • "+t("auto")+" "+j.cached_age+"s"]));
  }).catch(function(e){ var o=$("#srvout"); if(o){o.innerHTML="";o.appendChild(el("div",{class:"msg err"},[errText(e)]));} });
}

// ---- players ----
var plTimer=null, plData=null, plSort=null;
try{ plSort=JSON.parse(localStorage.getItem("sw_plsort")||"null"); }catch(e){}
function tsEpoch(ts){ if(!ts) return -1;
  var m=ts.match(/^(\d\d?)\.(\d\d?)\.(\d{4}) (\d\d?):(\d\d):(\d\d)/);
  return m? new Date(+m[3],+m[2]-1,+m[1],+m[4],+m[5],+m[6]).getTime() : -1; }
var PL_ACC={
  id:function(u){return u.id;}, name:function(u){return (u.name||"").toLowerCase();},
  status:function(u){return (u.online?1e13:0)+tsEpoch(u.last_enter);},
  map:function(u){return u.map==null?-1:u.map;},
  pos:function(u){return u.x==null?-1:(u.x*100000+(u.y||0));},
  enter:function(u){return tsEpoch(u.last_enter);}, exit:function(u){return tsEpoch(u.last_exit);},
  sess:function(u){return u.session_secs==null?-1:u.session_secs;},
  hours:function(u){return u.playtime_h==null?-1:u.playtime_h;},
  lvl:function(u){return u.level==null?-1:u.level;}, role:function(u){return u.role||0;},
  ban:function(u){return u.banned?1:0;}
};
function plSetSort(k){
  if(plSort && plSort.k===k) plSort.d=-plSort.d; else plSort={k:k,d:(k==="name"||k==="map"?1:-1)};
  try{ localStorage.setItem("sw_plsort",JSON.stringify(plSort)); }catch(e){}
  renderPlayers();
}
function tabPlayers(v){
  var wrap=el("div",{},[
    el("div",{class:"row",style:"margin-bottom:10px"},[
      el("button",{class:"small",onclick:loadPlayers},[t("refresh")]),
      el("label",{class:"small"},[el("input",{type:"checkbox",id:"plauto",checked:"checked"})," "+t("auto")]),
      el("input",{id:"plq",placeholder:t("pl_search"),style:"padding:5px 8px",oninput:renderPlayers}),
      el("label",{class:"small"},[el("input",{type:"checkbox",id:"plon",oninput:renderPlayers})," "+t("pl_only_online")]),
      el("select",{id:"plrole",oninput:renderPlayers,style:"padding:5px 8px"},
        [["","— "+t("pl_col_role")+" —"],["0",t("pl_role_player")],["1",t("pl_role_mod")],
         ["2",t("pl_role_admin")],["3",t("pl_role_gm")],["staff",t("pl_role_staff")]]
        .map(function(o){ return el("option",{value:o[0]},[o[1]]); }))
    ]),
    el("div",{id:"plsum",class:"grid",style:"margin-bottom:12px"},[]),
    el("div",{id:"plbody"},[el("p",{class:"muted"},["…"])]),
    el("div",{class:"card",style:"margin-top:12px"},[
      el("h3",{},[t("pl_recent")]), el("div",{id:"plrecent"},[])
    ])
  ]);
  v.appendChild(wrap);
  loadPlayers();
  clearInterval(plTimer);
  plTimer=setInterval(function(){ if(!document.hidden && S.tab==="players" && $("#plauto") && $("#plauto").checked) loadPlayers(); },15000);
}
function loadPlayers(){
  api("/api/players").then(function(j){ plData=j; renderPlayers(); }).catch(function(e){
    var b=$("#plbody"); if(b){ b.innerHTML=""; b.appendChild(el("div",{class:"msg err"},[errText(e)])); }
  });
}
function plEvLabel(k){ return t("pl_ev_"+k)||k; }
function renderPlayers(){
  var j=plData; if(!j) return;
  var sum=$("#plsum"), body=$("#plbody"), rec=$("#plrecent");
  if(!j.ok){ sum.innerHTML=""; body.innerHTML=""; body.appendChild(el("div",{class:"msg err"},[j.error||t("pl_none")]));
    if(j.root) body.appendChild(el("p",{class:"muted small mono"},[j.root])); rec.innerHTML=""; return; }
  var tt=j.totals||{};
  sum.innerHTML="";
  function card(title,rows){ return el("div",{class:"card"},[el("h3",{},[title])].concat(rows.map(function(r){
    return el("div",{class:"kv"},[el("span",{},[r[0]]),(typeof r[1]==="string"?el("b",{},[r[1]]):r[1])]); }))); }
  sum.appendChild(card(t("pl_world"),[
    ["", j.world||"?"],
    [t("pl_registered"), String(tt.registered||0)],
    [t("pl_online"), pill(true,String(tt.online_analytics||0))],
    [t("pl_online_gs"), String(tt.online_game_state||0)]
  ]));
  var bm=(j.by_map||[]);
  sum.appendChild(card(t("pl_bymap"), bm.length? bm.map(function(m){ return [t("pl_map")+" "+m.map, String(m.count)]; })
                                              : [["", t("dash")]]));

  var q=(($("#plq")||{}).value||"").toLowerCase().trim();
  var onlyOn=($("#plon")||{}).checked;
  var rsel=(($("#plrole")||{}).value||"");
  var rows=(j.users||[]).filter(function(u){
    if(onlyOn && !u.online) return false;
    if(rsel==="staff"){ if(!(u.role>0)) return false; }
    else if(rsel!=="" && String(u.role||0)!==rsel) return false;
    if(q && (u.name||"").toLowerCase().indexOf(q)<0 && String(u.id).indexOf(q)<0) return false;
    return true;
  });
  if(plSort && PL_ACC[plSort.k]){
    var acc=PL_ACC[plSort.k], dir=plSort.d;
    rows.sort(function(a,b){ var x=acc(a),y=acc(b);
      if(x<y) return -dir; if(x>y) return dir; return a.id-b.id; });
  } else {
    rows.sort(function(a,b){ if(a.online!==b.online) return a.online?-1:1;
      return tsEpoch(b.last_enter)-tsEpoch(a.last_enter); });
  }

  body.innerHTML="";
  var cols=[["id","ID"],["name",t("col_name")],["status",t("pl_col_status")],["map",t("pl_col_map")],
            ["pos",t("pl_col_pos")],["enter",t("pl_col_enter")],["exit",t("pl_col_exit")],["sess",t("pl_col_sess")],
            ["hours",t("pl_col_hours")],["lvl",t("pl_col_lvl")],["role",t("pl_col_role")],["ban",t("pl_col_ban")]];
  var tb=el("table",{},[el("tr",{}, cols.map(function(c){
    var arr=(plSort && plSort.k===c[0])? (plSort.d>0?" ▲":" ▼") : "";
    return el("th",{style:"cursor:pointer;user-select:none;white-space:nowrap",onclick:function(){ plSetSort(c[0]); }},[c[1]+arr]);
  }))]);
  rows.forEach(function(u){
    var st = u.online? pill(true,t("running")) : el("span",{class:"muted"},[fshort(u.last_exit)]);
    var roleName = ({0:"pl_role_player",1:"pl_role_mod",2:"pl_role_admin",3:"pl_role_gm"}[u.role]!=null)
      ? t({0:"pl_role_player",1:"pl_role_mod",2:"pl_role_admin",3:"pl_role_gm"}[u.role]) : (t("pl_role_staff")+" "+u.role);
    var role = (u.role>0)? el("span",{class:"pill warn"},[roleName]) : el("span",{class:"muted"},[roleName]);
    var coord = (u.x!=null && u.y!=null)? (u.x+", "+u.y) : "—";
    tb.appendChild(el("tr",{class:u.online?"hl":""},[
      el("td",{class:"mono"},[String(u.id)]),
      el("td",{},[plLink(u.id, u.name)]),
      el("td",{},[st]),
      el("td",{class:"mono"},[u.map!=null? String(u.map) : "—"]),
      el("td",{class:"mono"},[coord]),
      el("td",{class:"mono"},[fshort(u.last_enter)]),
      el("td",{class:"mono"},[fshort(u.last_exit)]),
      el("td",{},[u.session_secs!=null? fdur(u.session_secs) : "—"]),
      el("td",{},[u.playtime_h!=null? String(u.playtime_h) : "—"]),
      el("td",{},[u.level!=null? String(u.level) : "—"]),
      el("td",{},[role]),
      el("td",{},[u.banned? el("span",{class:"pill err"},["ban"]) : "—"])
    ]));
  });
  body.appendChild(tb);
  body.appendChild(el("p",{class:"muted small",style:"margin-top:8px"},[
    rows.length+" / "+(j.users||[]).length+" • "+t("auto")+" "+ (j.cached_age||0) +"s • "+(j.generated||"")]));

  rec.innerHTML="";
  var rl=el("div",{class:"small"},[]);
  (j.recent||[]).forEach(function(e){
    var extra = (e.kind==="exit" && e.secs!=null)? " ("+fdur(e.secs)+")" : "";
    rl.appendChild(el("div",{class:"mono"},[fshort(e.ts)+"  ", plLink(e.id, e.name||("id "+e.id)), " — "+plEvLabel(e.kind)+extra]));
  });
  rec.appendChild(rl);
}
function fshort(ts){ if(!ts) return "—"; var m=ts.match(/^(\d\d?)\.(\d\d?)\.\d{4} (\d\d?:\d\d)/);
  return m? (m[1].padStart(2,"0")+"."+m[2].padStart(2,"0")+" "+m[3]) : ts; }
function plLink(id,name){ return el("a",{class:"pl-link",onclick:function(){ openPlayer(id); }},[name||("id "+id)]); }

// ---- player detail modal ----
function openPlayer(id){
  var ovl=el("div",{class:"ovl",onclick:function(e){ if(e.target===ovl) closePlayer(); }},[
    el("div",{class:"dlg"},[
      el("header",{},[el("div",{class:"row",style:"padding:10px 14px"},[
        el("b",{id:"pd-name",style:"font-size:15px"},["#"+id]), el("span",{class:"sp"}),
        el("button",{class:"small",onclick:closePlayer},[t("pd_close")])
      ])]),
      el("div",{class:"bd",id:"pd-body"},[el("p",{class:"muted"},["…"])])
    ])
  ]);
  document.body.appendChild(ovl);
  document.addEventListener("keydown",pdEsc);
  api("/api/players/"+id).then(renderPlayerModal).catch(function(e){
    var b=$("#pd-body"); if(b){ b.innerHTML=""; b.appendChild(el("div",{class:"msg err"},[errText(e)])); }
  });
}
function pdEsc(e){ if(e.key==="Escape") closePlayer(); }
function closePlayer(){ var o=$(".ovl"); if(o) o.remove(); document.removeEventListener("keydown",pdEsc); }
function kvcard(title,rows){ return el("div",{class:"card"},[el("h3",{},[title])].concat(
  rows.filter(function(r){return r;}).map(function(r){
    return el("div",{class:"kv"},[el("span",{},[r[0]]),(typeof r[1]==="string"||typeof r[1]==="number")?el("b",{},[String(r[1])]):r[1]]); }))); }
function renderPlayerModal(d){
  var b=$("#pd-body"); if(!b) return; b.innerHTML="";
  if(!d.ok){ b.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
  var nm=$("#pd-name"); if(nm) nm.textContent="#"+d.id+"  "+d.name;
  var p=d.profile||{}, r=d.research||{}, po=d.position||{}, av=d.avatar||{}, s=d.sessions||{}, mi=d.missions||{};
  var roleKey={0:"pl_role_player",1:"pl_role_mod",2:"pl_role_admin",3:"pl_role_gm"}[d.role];
  var g=el("div",{class:"grid"},[]);

  g.appendChild(kvcard(t("pd_profile"),[
    [t("pl_col_role"), roleKey? t(roleKey) : ("role "+d.role)],
    [t("pd_level"), p.level],
    [t("pd_rating"), p.rating],
    [t("pd_playtime"), p.playtime_h],
    [t("pd_first_seen"), fshort(p.first_seen)],
    p.last_session_ago_h!=null? [t("pd_last_seen"), p.last_session_ago_h+" "+t("pd_h_ago")] : null,
    [t("pd_country"), p.country||"—"],
    [t("pd_video"), p.video_card||"—"],
    p.screen? [t("pd_screen"), p.screen.x+"×"+p.screen.y] : null,
    p.banned? [t("pd_ban_until"), p.ban_expires_in_h!=null? (p.ban_expires_in_h+" "+(S.lang==="ru"?"ч":"h")) : t("pd_ban_perm")] : null
  ]));

  g.appendChild(kvcard(t("pd_research"),[
    [t("pd_res_cur"), r.current||"—"],
    r.remaining_min!=null? [t("pd_res_left"), r.remaining_min+" "+t("pd_min")] : null,
    [t("pd_res_done"), r.done_count],
    r.booster!=null? [t("pd_booster"), r.booster] : null
  ].concat([ [t("pd_missions"), (mi.current!=null? mi.current : "—")+(mi.month!=null? " ("+t("pd_mission_month")+" "+mi.month+")":"")] ])));

  var terr=(po.territories||[]);
  g.appendChild(kvcard(t("pd_position"),[
    [t("pl_col_map"), po.map!=null? po.map : "—"],
    [t("pd_coords"), (po.x!=null? po.x+", "+po.y : "—")],
    po.respawn? [t("pd_respawn"), po.respawn.map+" @ "+po.respawn.x+", "+po.respawn.y] : null,
    [t("pd_territories"), terr.length? el("div",{class:"chips"}, terr.map(function(tt){
      return el("span",{class:"chip"},[tt.map+": "+tt.x+","+tt.y]); })) : "—"]
  ]));

  var PBL={0:"pd_p0",1:"pd_p1",2:"pd_p2",3:"pd_p3"}, LPL={0:"pd_lp0",1:"pd_lp1",2:"pd_lp2"};
  var params=(av.params||[]).filter(function(pp){ return pp.max>1; }).map(function(pp){
    var pct=Math.max(0,Math.min(100, 100*pp.val/pp.max));
    var lbl=PBL[pp.type]? t(PBL[pp.type]) : ("тип "+pp.type);
    return [lbl, el("div",{class:"bar",title:pp.val+" / "+pp.max},[
      el("span",{style:"width:"+pct+"%"},[]), el("b",{},[Math.round(pp.val)+" / "+Math.round(pp.max)])])];
  });
  var lps=(av.long_params||[]).map(function(pp){
    var l=(LPL[pp.type] && t(LPL[pp.type])) || ("L"+pp.type); return [l, String(pp.val)]; });
  g.appendChild(kvcard(t("pd_avatar"), params.concat(lps).concat([
    [t("pd_skills"), (av.skills&&av.skills.length)? el("div",{class:"chips"}, av.skills.map(function(sk){
      return el("span",{class:"chip"},["#"+sk.type+": "+sk.val]); })) : "—"],
    [t("pd_abilities"), (av.abilities&&av.abilities.length)? el("div",{class:"chips"}, av.abilities.map(function(a){
      return el("span",{class:"chip"},[a]); })) : "—"],
    [t("pd_buffs"), av.buffs||0]
  ])));

  var canEdit = d.online===false;
  var pdEditPw=el("input",{type:"password",placeholder:t("pass"),style:"padding:5px 8px;width:130px"});
  function pdWrite(url, extra, msgEl){
    if(msgEl) msgEl.textContent="…";
    var body=Object.assign({password:pdEditPw.value}, extra);
    return api(url,{body:body})
      .then(function(res){ if(msgEl) msgEl.textContent="✅"; api("/api/players/"+d.id).then(renderPlayerModal); return res; })
      .catch(function(e){ if(msgEl) msgEl.textContent=(e&&e.error==="bad_password")? t("pd_code_bad") : errText(e); throw e; });
  }
  function pdInvOp(op, where, item, count, msgEl){
    return pdWrite("/api/players/"+d.id+"/inventory", {op:op,where:where,item:item,count:count}, msgEl);
  }
  function pdMod(action, params, msgEl){
    return pdWrite("/api/players/"+d.id+"/moderate", Object.assign({action:action}, params||{}), msgEl);
  }
  function invCard(title, list, where, cnt){
    var head=el("h3",{},[title+" · "+(list?list.length:(cnt||0))]);
    if(!list || !list.length) return el("div",{class:"card"},[head, el("div",{class:"muted small"},[(cnt||0)+" "+t("pd_items")])]);
    var rows=list.slice(0,80).map(function(it){
      var tds=[el("td",{},[it.name]), el("td",{class:"mono"},[String(it.count!=null?it.count:"")]),
               el("td",{class:"mono muted"},[it.durability!=null? String(it.durability):"—"])];
      if(canEdit) tds.push(el("td",{},[el("button",{class:"small danger",title:t("pd_inv_take"),onclick:function(){
        var n=parseInt(window.prompt(t("pd_inv_take")+" "+it.name+" ×", String(it.count||1)),10);
        if(n>0) pdInvOp("take", where, it.id, n, null);
      }},["–"])]));
      return el("tr",{},tds);
    });
    var hd=[t("col_name"),"×","dur"]; if(canEdit) hd.push("");
    var tbl=el("table",{}, [el("tr",{},hd.map(function(x){return el("th",{},[x]);}))].concat(rows));
    return el("div",{class:"card"},[head, tbl]);
  }
  g.appendChild(invCard(t("pd_stash"), av.stash, "stash", av.stash_count));
  g.appendChild(invCard(t("pd_carry"), av.carry, "carry", av.carry_count));

  if(canEdit){
    var giveItem=el("input",{list:"pd-itemlist",placeholder:t("pd_inv_item"),style:"padding:5px 8px"});
    var giveCnt=el("input",{type:"number",value:"1",min:"1",style:"padding:5px 8px;width:90px"});
    var giveMsg=el("span",{class:"muted small"},[]);
    if(!$("#pd-itemlist")){
      var dl=el("datalist",{id:"pd-itemlist"},[]);
      document.body.appendChild(dl);
      api("/api/items").then(function(ij){ if(ij.ok) (ij.items||[]).forEach(function(it){
        dl.appendChild(el("option",{value:it.name},[])); }); }).catch(function(){});
    }
    g.appendChild(el("div",{class:"card"},[
      el("h3",{},[t("pd_inv_edit")]),
      el("div",{class:"row"},[el("span",{class:"muted small"},[t("pd_code_prompt")]), pdEditPw]),
      el("div",{class:"row",style:"margin-top:8px"},[
        giveItem, giveCnt,
        el("button",{class:"small pri",onclick:function(){
          pdInvOp("give","stash",giveItem.value.trim(),parseInt(giveCnt.value,10)||1,giveMsg);
        }},["＋ "+t("pd_inv_give")]),
        giveMsg
      ])
    ]));

    // --- модерация ---
    var mMsg=el("span",{class:"muted small"},[]);
    var banH=el("input",{type:"number",value:"0",min:"0",title:"0 = навсегда",style:"padding:4px 7px;width:80px"});
    var roleS=el("select",{}, [0,1,2,3].map(function(r){ return el("option",{value:r,selected:d.role===r?"selected":null},
      [t({0:"pl_role_player",1:"pl_role_mod",2:"pl_role_admin",3:"pl_role_gm"}[r])]); }));
    var mMap=el("input",{type:"number",placeholder:"map",style:"padding:4px 7px;width:70px",value:(po.map!=null?po.map:"")});
    var mX=el("input",{type:"number",placeholder:"x",style:"padding:4px 7px;width:70px",value:(po.x!=null?po.x:"")});
    var mY=el("input",{type:"number",placeholder:"y",style:"padding:4px 7px;width:70px",value:(po.y!=null?po.y:"")});
    var mResp=el("input",{type:"checkbox"});
    var mTech=el("input",{placeholder:"b5, e7 …",style:"padding:4px 7px"});
    var mStatF=el("select",{}, [["unitLevel",t("pd_level")],["addRating",t("pd_rating")]].map(function(o){return el("option",{value:o[0]},[o[1]]);}));
    var mStatV=el("input",{type:"number",style:"padding:4px 7px;width:90px"});
    var mCode=el("input",{placeholder:t("pd_mod_newcode"),style:"padding:4px 7px"});
    function mrow(label, kids){ return el("div",{class:"row",style:"margin:5px 0"},[el("span",{class:"muted small",style:"min-width:90px"},[label])].concat(kids)); }
    g.appendChild(el("div",{class:"card"},[
      el("h3",{},[t("pd_mod")]),
      mrow(t("pd_ban_until"),[banH, el("span",{class:"muted small"},["ч, 0=∞"]),
        el("button",{class:"small danger",onclick:function(){ pdMod("ban",{hours:parseFloat(banH.value)||0},mMsg); }},[t("pd_mod_ban")]),
        el("button",{class:"small",onclick:function(){ pdMod("unban",{},mMsg); }},[t("pd_mod_unban")])]),
      mrow(t("pl_col_role"),[roleS, el("button",{class:"small",onclick:function(){ pdMod("role",{role:parseInt(roleS.value,10)},mMsg); }},[t("save")])]),
      mrow(t("pd_position"),[mMap,mX,mY, el("label",{class:"small"},[mResp," respawn"]),
        el("button",{class:"small",onclick:function(){ pdMod("position",{map:mMap.value,x:mX.value,y:mY.value,respawn:mResp.checked},mMsg); }},[t("pd_mod_tp")])]),
      mrow(t("pd_research"),[mTech, el("button",{class:"small",onclick:function(){ pdMod("tech",{tech:mTech.value},mMsg); }},[t("pd_mod_givetech")])]),
      mrow(t("pd_params"),[mStatF,mStatV, el("button",{class:"small",onclick:function(){ pdMod("stat",{field:mStatF.value,value:parseInt(mStatV.value,10)},mMsg); }},[t("save")])]),
      mrow(t("pd_mod_resetpw"),[mCode, el("button",{class:"small danger",onclick:function(){ if(mCode.value) pdMod("reset_code",{code:mCode.value},mMsg); }},[t("save")])]),
      mMsg
    ]));
  } else {
    g.appendChild(el("div",{class:"card"},[el("div",{class:"muted small"},["🔒 "+t("pd_inv_online")])]));
  }

  var sp=el("div",{class:"spark"}, (s.by_hour||[]).map(function(n){
    var mx=Math.max.apply(null,(s.by_hour||[1])); return el("i",{style:"height:"+(mx? Math.round(100*n/mx):0)+"%",title:n},[]); }));
  var recent=el("div",{class:"mono small"}, (s.recent||[]).map(function(x){
    return el("div",{},[fshort(x.enter)+" → "+(x.exit? fshort(x.exit):"…")+"  "+(x.secs? fdur(x.secs):"")]); }));
  g.appendChild(el("div",{class:"card"},[
    el("h3",{},[t("pd_sessions")]),
    el("div",{class:"kv"},[el("span",{},[t("pd_sess_total")]),el("b",{},[String(s.total||0)])]),
    el("div",{class:"kv"},[el("span",{},[t("pd_sess_hours")]),el("b",{},[String(s.total_h||0)])]),
    el("div",{class:"kv"},[el("span",{},[t("pd_sess_avg")+" / "+t("pd_sess_max")]),el("b",{},[(s.avg_min||0)+" / "+(s.max_min||0)+" "+t("pd_min")])]),
    el("div",{class:"muted small",style:"margin:8px 0 3px"},[t("pd_sess_byhour")]), sp,
    el("div",{class:"muted small",style:"margin:8px 0 3px"},[t("pd_sess_recent")]), recent
  ]));

  if(d.clan && d.clan_members && d.clan_members.length){
    var cl=d.clan;
    var crows=[
      [t("pd_clan_rating"), (cl.rating!=null? cl.rating : "—")+(cl.clan_point!=null? " · "+cl.clan_point+"cp":"")],
      [t("pd_clan_slots"), d.clan_members.length+" / "+(cl.max_users!=null? cl.max_users : "?")]
    ].concat(d.clan_members.map(function(m){
      return [(m.role===0?"👑 ":"")+"#"+m.id, el("span",{},[plLink(m.id, m.name), " · r"+(m.rating||0)+" · "+(m.clan_point||0)+"cp"])]; }));
    g.appendChild(kvcard(t("pd_clan")+(cl.name? " · "+cl.name:""), crows));
  }
  if(d.friends && d.friends.length){
    g.appendChild(kvcard(t("pd_friends")+" · "+d.friends.length,
      d.friends.map(function(f){ return ["#"+f.id, el("span",{},[plLink(f.id, f.name), " · "+f.accesses+" acc"])]; })));
  }

  var ac=d.activity||{};
  var acRows=[];
  (ac.role_grants||[]).forEach(function(x){
    acRows.push([t("pd_roles"), x.as_target
      ? (t("pd_role_to")+" "+x.role+" ("+t("pd_role_by")+" "+x.by+")")
      : (x.target+" "+t("pd_role_to")+" "+x.role)]);
  });
  if((ac.deaths||[]).length) acRows.push([t("pd_deaths"), el("span",{},[String(ac.deaths.length)+" · "+ac.deaths.slice(0,6).map(function(x){return fshort(x.ts)+" "+(t("pd_dev_"+x.event)||x.event);}).join("  ")])]);
  if((ac.land_deletions||[]).length) acRows.push([t("pd_lands"), ac.land_deletions.slice(0,8).map(function(x){return "["+x.map+"] "+x.x+","+x.y;}).join("  ")]);
  if((ac.rewards||[]).length) acRows.push([t("pd_rewards"), ac.rewards.map(function(x){return fshort(x.ts).slice(0,5)+":"+x.reward;}).join("  ")]);
  if(acRows.length) g.appendChild(kvcard(t("pd_activity"), acRows));

  var chatCard=el("div",{class:"card"},[el("h3",{},[t("pd_chat")]), el("div",{class:"muted small"},["…"])]);
  g.appendChild(chatCard);
  api("/api/players/"+d.id+"/chat?limit=80").then(function(cj){
    chatCard.innerHTML=""; chatCard.appendChild(el("h3",{},[t("pd_chat")+(cj.count!=null? " · "+cj.count : "")]));
    if(!cj.ok || !cj.messages || !cj.messages.length){ chatCard.appendChild(el("div",{class:"muted small"},[t("pd_chat_none")])); return; }
    var box=el("div",{class:"small",style:"max-height:220px;overflow:auto"},[]);
    cj.messages.forEach(function(m){ box.appendChild(el("div",{},[
      el("span",{class:"lg-t mono"},[fshort(m.ts)+" "]), el("span",{class:"chip"},[m.channel]), " "+m.text ])); });
    chatCard.appendChild(box);
  }).catch(function(){ chatCard.querySelector(".muted").textContent=t("err_net"); });

  b.appendChild(g);

  // sensitive blocks (each behind admin password)
  function gate(box, url, promptKey, render){
    box.innerHTML="";
    var pw=el("input",{type:"password",placeholder:t("pass"),style:"padding:6px 8px"});
    var msg=el("span",{class:"muted small"},[]);
    var go=el("button",{class:"small",onclick:function(){
      msg.textContent="…";
      api(url,{body:{password:pw.value}}).then(function(res){ box.innerHTML=""; render(box,res); })
        .catch(function(e){ msg.textContent=(e&&e.error==="bad_password")? t("pd_code_bad") : errText(e); });
    }},[t("pd_code_btn")]);
    box.appendChild(el("div",{class:"row"},[el("span",{class:"muted small"},[t(promptKey)]), pw, go, msg]));
    pw.focus();
  }
  var secBox=el("div",{class:"card",style:"margin-top:12px"},[]);
  var btnRow=el("div",{class:"row"},[
    el("button",{class:"small danger",onclick:function(){
      gate(secBox, "/api/players/"+d.id+"/secret", "pd_code_prompt", function(box,res){
        box.appendChild(el("div",{class:"kv"},[el("span",{},["code"]),el("b",{class:"mono"},[res.code||"—"])]));
        box.appendChild(btnRow);
      });
    }},[t("pd_show_code")]),
    el("button",{class:"small danger",onclick:function(){
      gate(secBox, "/api/players/"+d.id+"/sensitive", "pd_code_prompt", function(box,res){
        box.appendChild(el("h3",{},[t("pd_priv")+" · "+(res.private||[]).length]));
        if(!(res.private||[]).length) box.appendChild(el("div",{class:"muted small"},[t("pd_priv_none")]));
        var pb=el("div",{class:"small",style:"max-height:200px;overflow:auto"},[]);
        (res.private||[]).forEach(function(m){ pb.appendChild(el("div",{},[
          el("span",{class:"lg-t mono"},[fshort(m.ts)+" "]),
          el("b",{},[m.outgoing? (d.name+" → "+m.to) : (m.from+" → "+d.name)]), ": "+m.text ])); });
        box.appendChild(pb);
        box.appendChild(el("h3",{style:"margin-top:10px"},[t("pd_ips")+" · "+(res.distinct_ips||[]).length]));
        var ib=el("div",{class:"mono small"},[]);
        (res.ips||[]).forEach(function(x){ ib.appendChild(el("div",{},[fshort(x.ts)+"  "+x.ip+":"+x.port+(x.new?"  ●":"")])); });
        box.appendChild(ib);
        box.appendChild(btnRow);
      });
    }},[t("pd_sens_btn")])
  ]);
  secBox.appendChild(btnRow);
  b.appendChild(secBox);
}

// ---- stats (server dashboards) ----
function svgLine(pts, w, hh){
  if(!pts.length) return el("div",{class:"muted small"},["—"]);
  var mx=Math.max.apply(null,pts.concat([1])), n=pts.length;
  var d=pts.map(function(y,i){ return (i? "L":"M")+(i/(n-1)*w).toFixed(1)+" "+(hh-y/mx*hh).toFixed(1); }).join(" ");
  return el("div",{html:'<svg viewBox="0 0 '+w+' '+hh+'" preserveAspectRatio="none" style="width:100%;height:'+hh+'px;display:block">'
    +'<polyline points="0,'+hh+' '+pts.map(function(y,i){return (i/(n-1)*w).toFixed(1)+","+(hh-y/mx*hh).toFixed(1);}).join(" ")+' '+w+','+hh+'" fill="rgba(76,141,255,.15)" stroke="none"/>'
    +'<path d="'+d+'" fill="none" stroke="var(--acc)" stroke-width="1.5"/></svg>'});
}
function tabStats(v){
  var wrap=el("div",{},[el("div",{class:"row",style:"margin-bottom:10px"},[el("button",{class:"small",onclick:function(){loadStats(true);}},[t("refresh")])]), el("div",{id:"stbody"},[el("p",{class:"muted"},["…"])])]);
  v.appendChild(wrap); loadStats(false);
}
function loadStats(force){
  api("/api/stats"+(force?"?_="+Date.now():"")).then(drawStats).catch(function(e){
    var b=$("#stbody"); if(b){b.innerHTML="";b.appendChild(el("div",{class:"msg err"},[errText(e)]));}
  });
}
function ltable(head, rows, mk){
  var tb=el("table",{},[el("tr",{},head.map(function(x){return el("th",{},[x]);}))]);
  rows.forEach(function(r){ tb.appendChild(el("tr",{},mk(r).map(function(c){return el("td",{},[c]);}))); });
  return tb;
}
function backupDownload(scope, pw, msg){
  msg.textContent=t("ex_wait");
  fetch("/api/world-backup",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":S.csrf},body:JSON.stringify({password:pw,scope:scope})})
    .then(function(r){ if(!r.ok) return r.json().then(function(j){ throw j; });
      var fn=(r.headers.get("Content-Disposition")||"").match(/filename="?([^"]+)"?/); fn=fn?fn[1]:"world_backup.zip";
      return r.blob().then(function(bl){ var a=document.createElement("a"); a.href=URL.createObjectURL(bl); a.download=fn; a.click();
        setTimeout(function(){URL.revokeObjectURL(a.href);},4000); msg.textContent="✅ "+fn; }); })
    .catch(function(e){ msg.textContent=(e&&e.error==="bad_password")? t("pd_code_bad") : (e&&(e.error||e.detail))||t("err_net"); });
}
function drawStats(j){
  var b=$("#stbody"); if(!b) return; b.innerHTML="";
  if(!j.ok){ b.appendChild(el("div",{class:"msg err"},[j.error||"error"])); return; }
  var expPw=el("input",{type:"password",placeholder:t("pass"),style:"padding:5px 8px;width:120px"});
  var expMsg=el("span",{class:"muted small"},[]);
  b.appendChild(el("div",{class:"card",style:"margin-bottom:12px"},[
    el("h3",{},[t("ex_title")]),
    el("div",{class:"row"},[
      el("button",{class:"small",onclick:function(){ window.open("/api/players-csv","_blank"); }},[t("ex_csv")]),
      expPw,
      el("button",{class:"small",onclick:function(){ backupDownload("state",expPw.value,expMsg); }},[t("ex_bstate")]),
      el("button",{class:"small danger",onclick:function(){ if(window.confirm(t("ex_bfull")+"?")) backupDownload("full",expPw.value,expMsg); }},[t("ex_bfull")]),
      expMsg
    ])
  ]));
  var g=el("div",{class:"grid",style:"grid-template-columns:repeat(auto-fit,minmax(300px,1fr))"},[]);
  var on=j.online||{};
  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_online")]),
    el("div",{class:"row",style:"margin-bottom:6px"},[pill(true,t("st_now")+" "+on.now), el("span",{class:"pill"},[t("st_peak")+" "+on.peak7])]),
    svgLine((on.series||[]).map(function(p){return p.n;}), 300, 70)]));

  var gr=j.growth||{}, days=(gr.days||[]);
  var rt=gr.retention||{d1:[0,0],d7:[0,0]};
  var maxd=Math.max.apply(null,days.map(function(x){return Math.max(x.reg,x.dau);}).concat([1]));
  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_growth")]),
    el("div",{class:"spark",style:"height:60px"}, days.map(function(x){
      return el("i",{style:"height:"+Math.round(100*x.dau/maxd)+"%",title:x.d+": "+t("st_dau")+" "+x.dau+", "+t("st_reg")+" "+x.reg},[]); })),
    el("div",{class:"kv"},[el("span",{},[t("st_reg")+" ("+days.length+"д)"]),el("b",{},[String(days.reduce(function(a,x){return a+x.reg;},0))])]),
    el("div",{class:"kv"},[el("span",{},[t("st_ret")+" D1 / D7"]),el("b",{},[
      (rt.d1[1]? Math.round(100*rt.d1[0]/rt.d1[1]):0)+"% / "+(rt.d7[1]? Math.round(100*rt.d7[0]/rt.d7[1]):0)+"%  ("+rt.d1[1]+")"])])]));

  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_toplvl")]),
    ltable(["#",t("col_name"),t("pd_level")], (j.top_level||[]).slice(0,15), function(r){ return [String(r.id), plLink(r.id,r.name), String(r.level)]; })]));
  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_toptime")]),
    ltable(["#",t("col_name"),"h"], (j.top_time||[]).slice(0,15), function(r){ return [String(r.id), plLink(r.id,r.name), String(r.playtime_h)]; })]));

  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_clans")+" · "+(j.clan_board||[]).length]),
    ltable([t("col_name"),"rating","size"], (j.clan_board||[]).slice(0,15), function(c){
      return [c.name+"", String(c.rating), c.size+"/"+(c.max||"?")]; })]));

  var bans=j.banned||[];
  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_bans")+" · "+bans.length]),
    bans.length? ltable(["#",t("col_name")], bans.slice(0,30), function(r){ return [String(r.id), plLink(r.id,r.name)]; }) : el("div",{class:"muted small"},["—"])]));

  var staff=j.staff||[];
  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_staff")+" · "+staff.length]),
    ltable(["#",t("col_name"),t("pl_col_role")], staff, function(r){
      return [String(r.id), plLink(r.id,r.name), t({0:"pl_role_player",1:"pl_role_mod",2:"pl_role_admin",3:"pl_role_gm"}[r.role]||"pl_role_staff")]; }),
    (j.role_history&&j.role_history.length)? el("div",{class:"muted small",style:"margin-top:8px"},[t("st_hist")+": "+j.role_history.map(function(x){return x.target+"→"+x.role;}).join(", ")]) : null
  ].filter(Boolean)));

  var months=j.months||[];
  if(months.length) g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_month")]),
    el("div",{}, months.map(function(mo){ return el("div",{style:"margin-bottom:6px"},[
      el("b",{},[mo.date]), " — ",
      el("span",{class:"small"},[(mo.rewards||[]).map(function(r){return r.name+"("+r.reward+")";}).join(", ")])]); }))]));

  var lh=j.level_hist||[];
  var lmax=Math.max.apply(null,lh.map(function(x){return x.n;}).concat([1]));
  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_lvldist")]),
    el("div",{class:"spark",style:"height:56px"}, lh.map(function(x){ return el("i",{style:"height:"+Math.round(100*x.n/lmax)+"%",title:x.bucket+"+: "+x.n},[]); })),
    el("div",{class:"muted small"},[lh.map(function(x){return x.bucket;}).join("  ")])]));
  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_countries")]),
    ltable([t("st_countries"),"n"], (j.country_hist||[]).slice(0,12), function(r){ return [r.country, String(r.n)]; })]));

  b.appendChild(g);
  b.appendChild(el("p",{class:"muted small",style:"margin-top:8px"},[
    "рег "+j.totals.registered+" • профилей "+j.totals.with_profile+" • кланов "+j.totals.clans+" • "+(j.cached_age||0)+"s • "+j.generated]));

  var wbox=el("div",{style:"margin-top:14px"},[el("p",{class:"muted"},["…"])]);
  b.appendChild(wbox);
  api("/api/world").then(function(w){
    wbox.innerHTML="";
    if(!w.ok){ wbox.appendChild(el("div",{class:"msg err"},[w.error||"error"])); return; }
    var wg=el("div",{class:"grid",style:"grid-template-columns:repeat(auto-fit,minmax(300px,1fr))"},[]);
    wg.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_world")+" · "+w.totals.maps]),
      el("div",{class:"muted small",style:"margin-bottom:6px"},[t("st_avatars")+" "+w.totals.avatars+" • bots "+w.totals.bots+" • "+t("st_terr")+" "+w.totals.territories]),
      ltable([t("pl_map"),t("pl_online"),t("st_avatars"),t("st_terr")], (w.maps||[]).slice(0,40),
        function(r){ return [String(r.map), String(r.online), String(r.avatars), String(r.territories)]; })]));
    var mf=el("input",{type:"number",placeholder:t("st_terrfilter"),style:"padding:5px 8px;width:90px"});
    var tt=el("div",{id:"terrtab"},[]);
    function drawTerr(){
      var f=mf.value.trim();
      var rows=(w.territories||[]).filter(function(x){ return !f || String(x.map)===f; });
      tt.innerHTML="";
      tt.appendChild(ltable([t("pl_map"),t("pd_coords"),t("st_owner")], rows.slice(0,300), function(x){
        return [String(x.map), x.x+","+x.y, plLink(x.owner_id, x.owner)]; }));
      tt.appendChild(el("p",{class:"muted small"},[rows.length+" / "+(w.territories||[]).length]));
    }
    mf.oninput=drawTerr;
    var tc=el("div",{class:"card"},[el("h3",{},[t("st_terr")]), el("div",{class:"row",style:"margin-bottom:6px"},[mf]), tt]);
    wg.appendChild(tc);
    wbox.appendChild(wg); drawTerr();
  }).catch(function(){ wbox.innerHTML=""; });

  var hbox=el("div",{style:"margin-top:14px"},[]);
  b.appendChild(hbox);
  api("/api/health").then(function(hj){
    hbox.innerHTML="";
    if(!hj.ok) return;
    var hg=el("div",{class:"grid",style:"grid-template-columns:repeat(auto-fit,minmax(300px,1fr))"},[]);
    var rd=hj.ready||{};
    hg.appendChild(el("div",{class:"card"},[el("h3",{},[t("hh_ready")]),
      el("div",{class:"kv"},[el("span",{},[t("hh_startup")]),el("b",{},[String(rd.startup_ms||"—")])]),
      el("div",{class:"kv"},[el("span",{},[t("hh_mem")]),el("b",{},[String(rd.managed_mb||"—")])]),
      el("div",{class:"kv"},[el("span",{},[t("hh_clusters")]),el("b",{},[String(rd.clusters||"—")])]),
      el("div",{class:"kv"},[el("span",{},["ts"]),el("b",{},[rd.ts||"—"])]),
      el("div",{class:"muted small",style:"margin-top:6px"},[t("hh_slowphase")+": "+(hj.slow_phases||[]).map(function(p){return p.phase+" "+p.ms+"ms";}).join(", ")])]));
    var lg=hj.lag||{};
    var lmax=Math.max.apply(null,(lg.per_day||[]).map(function(x){return x.n;}).concat([1]));
    hg.appendChild(el("div",{class:"card"},[el("h3",{},[t("hh_lag")+" · "+(lg.total||0)]),
      el("div",{class:"spark",style:"height:52px"}, (lg.per_day||[]).map(function(x){
        return el("i",{style:"height:"+Math.round(100*x.n/lmax)+"%",title:x.d+": "+x.n},[]); })),
      el("div",{class:"muted small"},[(lg.per_day||[]).map(function(x){return x.d.slice(0,5);}).join("  ")]),
      el("div",{class:"muted small",style:"margin-top:6px"},[t("hh_byfunc")+": "+(lg.by_func||[]).slice(0,8).map(function(f){return f.func+"×"+f.n+"(max "+f.max+")";}).join(", ")])]));
    var ce=hj.conn_errors||{};
    hg.appendChild(el("div",{class:"card"},[el("h3",{},[t("hh_connerr")+" · "+(ce.total||0)]),
      (ce.by_player&&ce.by_player.length)? ltable(["#",t("col_name"),"n"], ce.by_player.slice(0,15), function(r){ return [String(r.id), plLink(r.id,r.name), String(r.n)]; }) : el("div",{class:"muted small"},["—"])]));
    hbox.appendChild(hg);
  }).catch(function(){});
}

// ---- server chat / events ----
var chTimer=null;
function tabChat(v){
  var sub=localStorage.getItem("sw_chatsub")||"server";
  var bar=el("nav",{style:"padding:0;border:0;background:transparent;margin-bottom:10px"},
    [["server","sc_server"],["events","sc_events"],["private","sc_private"]].map(function(x){
      return el("button",{class:sub===x[0]?"active":"",onclick:function(){ localStorage.setItem("sw_chatsub",x[0]); render(); }},[t(x[1])]);
    }));
  var body=el("div",{id:"chbody"},[]);
  v.appendChild(el("div",{},[bar,body]));
  clearInterval(chTimer);
  if(sub==="server") chServer(body);
  else if(sub==="events") chEvents(body);
  else chPrivate(body);
}
function chMsgLine(m){
  return el("div",{class:"mono small"},[
    el("span",{class:"lg-t"},[fshort(m.ts)+" "]),
    el("span",{class:"chip"},[m.channel||"?"]), " ",
    m.id!=null? plLink(m.id, m.nick) : el("b",{},[m.nick||"?"]),
    ": "+m.text ]);
}
function chServer(body){
  body.innerHTML="";
  var ch=el("select",{id:"chch"}, [["","— "+t("sc_all")+" —"],["global","global"],["global2","global2"],["ru","ru"],["clan","clan"]]
    .map(function(o){return el("option",{value:o[0]},[o[1]]);}));
  var qq=el("input",{id:"chq",placeholder:t("sc_search"),style:"padding:5px 8px"});
  var nn=el("select",{id:"chn"},["150","300","600","1500"].map(function(x){return el("option",{value:x},[x]);})); nn.value="300";
  var auto=el("input",{type:"checkbox",id:"chauto",checked:"checked"});
  var box=el("pre",{class:"log",id:"chpre"},["…"]);
  body.appendChild(el("div",{class:"row",style:"margin-bottom:8px"},[
    ch, qq, el("label",{class:"small"},[t("lines")+" ",nn]),
    el("label",{class:"small"},[auto," "+t("auto")]),
    el("button",{class:"small",onclick:pullChat},[t("refresh")])
  ]));
  body.appendChild(box);
  ch.onchange=nn.onchange=pullChat; qq.oninput=function(){ clearTimeout(qq._t); qq._t=setTimeout(pullChat,400); };
  pullChat();
  chTimer=setInterval(function(){ if(!document.hidden && $("#chauto") && $("#chauto").checked) pullChat(); },5000);
}
function pullChat(){
  var c=($("#chch")||{}).value||"", q=encodeURIComponent(($("#chq")||{}).value||""), n=($("#chn")||{}).value||"300";
  api("/api/server-chat?limit="+n+"&channel="+c+"&q="+q).then(function(j){
    var pre=$("#chpre"); if(!pre) return; pre.innerHTML="";
    if(!j.ok){ pre.textContent=j.error||"error"; return; }
    j.messages.slice().reverse().forEach(function(m){ pre.appendChild(chMsgLine(m)); });
    pre.scrollTop=pre.scrollHeight;
  }).catch(function(){});
}
function chEvents(body){
  body.innerHTML="";
  var kinds=["","join","leave","register","death","land"];
  var ksel=el("select",{id:"evk"}, kinds.map(function(k){return el("option",{value:k},[k? t("ev_"+k) : ("— "+t("ev_kind")+" —")]);}));
  var out=el("div",{id:"evout"},["…"]);
  body.appendChild(el("div",{class:"row",style:"margin-bottom:8px"},[ksel, el("button",{class:"small",onclick:pullEvents},[t("refresh")])]));
  body.appendChild(out);
  ksel.onchange=pullEvents; pullEvents();
  chTimer=setInterval(function(){ if(!document.hidden && S.tab==="chat") pullEvents(); },8000);
}
function pullEvents(){
  var k=($("#evk")||{}).value||"";
  api("/api/server-events?limit=300"+(k?"&kinds="+k:"")).then(function(j){
    var out=$("#evout"); if(!out) return; out.innerHTML="";
    if(!j.ok){ out.appendChild(el("div",{class:"msg err"},[j.error||"error"])); return; }
    var box=el("div",{class:"mono small",style:"max-height:60vh;overflow:auto"},[]);
    j.events.forEach(function(e){ box.appendChild(el("div",{},[
      el("span",{class:"lg-t"},[fshort(e.ts)+" "]),
      el("span",{class:"chip"},[t("ev_"+e.kind)||e.kind]), " ",
      e.id!=null? plLink(e.id, e.actor) : (e.actor||"?"),
      e.detail? ("  "+e.detail) : "" ])); });
    out.appendChild(box);
    if(j.role_grants && j.role_grants.length){
      out.appendChild(el("h3",{style:"margin-top:12px"},[t("pd_roles")+" · "+j.role_grants.length]));
      var rb=el("div",{class:"mono small"},[]);
      j.role_grants.forEach(function(r){ rb.appendChild(el("div",{},[
        plLink(r.target_id, r.target), " → "+r.role+"  ("+t("pd_role_by")+" ", plLink(r.by_id, r.by), ")" ])); });
      out.appendChild(rb);
    }
  }).catch(function(){});
}
function chPrivate(body){
  body.innerHTML="";
  var pw=el("input",{type:"password",placeholder:t("pass"),style:"padding:6px 8px"});
  var qq=el("input",{placeholder:t("sc_search"),style:"padding:6px 8px"});
  var out=el("div",{id:"pvout",style:"margin-top:10px"},[]);
  function run(){
    out.innerHTML=""; out.appendChild(el("p",{class:"muted"},["…"]));
    api("/api/server-chat",{body:{password:pw.value, q:qq.value, limit:800}}).then(function(j){
      out.innerHTML="";
      var box=el("div",{class:"mono small",style:"max-height:62vh;overflow:auto"},[]);
      (j.messages||[]).forEach(function(m){ box.appendChild(el("div",{},[
        el("span",{class:"lg-t"},[fshort(m.ts)+" "]),
        m.from_id!=null? plLink(m.from_id,m.from):el("b",{},[m.from]), " → ",
        m.to_id!=null? plLink(m.to_id,m.to):el("b",{},[m.to]), ": "+m.text ])); });
      out.appendChild(el("p",{class:"muted small"},[String(j.total||0)])); out.appendChild(box);
    }).catch(function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[(e&&e.error==="bad_password")? t("pd_code_bad") : errText(e)])); });
  }
  body.appendChild(el("p",{class:"muted small"},[t("sc_priv_note")]));
  body.appendChild(el("div",{class:"row"},[pw, qq, el("button",{class:"small pri",onclick:run},[t("tw_show")])]));
  body.appendChild(out);
}

// ---- twinks (same-IP account detector) ----
function tabTwinks(v){
  var pw=el("input",{type:"password",placeholder:t("pass"),style:"padding:6px 8px"});
  var mn=el("input",{type:"number",value:"2",min:"2",max:"20",style:"padding:6px 8px;width:80px"});
  var out=el("div",{id:"twout",style:"margin-top:12px"},[]);
  function run(){
    out.innerHTML=""; out.appendChild(el("p",{class:"muted"},["…"]));
    api("/api/twinks",{body:{password:pw.value, min_accounts:parseInt(mn.value,10)||2}}).then(function(j){
      out.innerHTML="";
      out.appendChild(el("p",{class:"muted small"},[
        t("tw_summary")+": "+j.ip_count+" • "+t("tw_flagged")+": "+j.flagged_ips+
        (j.ignored&&j.ignored.length? " • "+t("tw_ignored")+": "+j.ignored.join(", "):"")+" • "+j.generated]));
      if(!j.groups.length){ out.appendChild(el("p",{class:"muted"},[t("tw_none")])); return; }
      j.groups.forEach(function(gr){
        var tb=el("table",{},[el("tr",{},["ID",t("col_name"),t("tw_connects"),t("pl_col_enter"),t("pl_col_exit"),t("tw_other_ips")].map(function(x){return el("th",{},[x]);}))]);
        gr.accounts.forEach(function(a){
          tb.appendChild(el("tr",{},[
            el("td",{class:"mono"},[a.id!=null? String(a.id):"—"]),
            el("td",{},[a.id!=null? plLink(a.id, a.name) : a.name]),
            el("td",{class:"mono"},[String(a.connects)]),
            el("td",{class:"mono muted"},[fshort(a.first_seen)]),
            el("td",{class:"mono muted"},[fshort(a.last_seen)]),
            el("td",{class:"mono muted"},[a.other_ips&&a.other_ips.length? a.other_ips.join(" "):"—"])
          ]));
        });
        out.appendChild(el("div",{class:"card",style:"margin-bottom:10px"},[
          el("h3",{},["🌐 "+gr.ip+" · "+gr.count]), tb ]));
      });
    }).catch(function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[(e&&e.error==="bad_password")? t("pd_code_bad") : errText(e)])); });
  }
  v.appendChild(el("div",{},[
    el("p",{class:"muted small"},[t("tw_intro")]),
    el("div",{class:"row"},[
      el("span",{class:"muted small"},[t("tw_prompt")]), pw,
      el("label",{class:"small"},[t("tw_min")+" ", mn]),
      el("button",{class:"small pri",onclick:run},[t("tw_show")])
    ]),
    out
  ]));
}

// ---- roles ----
function tabRoles(v){
  var out=el("div",{id:"rout"},[el("p",{class:"muted"},["…"])]);
  v.appendChild(out);
  api("/api/roles").then(function(j){ drawRoles(out,j); })
    .catch(function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
}
function drawRoles(out,j){
  out.innerHTML="";
  var fAdm=el("textarea",{id:"radm"},[ (j.allowed_user_ids||[]).join(", ") ]);
  var fMod=el("textarea",{id:"rmod"},[ (j.moderator_user_ids||[]).join(", ") ]);
  var fSup=el("input",{id:"rsup",value:j.super_admin_id!=null?j.super_admin_id:"",style:"width:220px"});
  var fLang=el("select",{id:"rlang"}, ["ru","en"].map(function(l){ return el("option",{value:l,selected:j.default_lang===l?"selected":null},[l]); }));
  var fAlerts=el("input",{id:"ralerts",type:"checkbox"}); if(j.alerts_enabled) fAlerts.checked=true;
  var card=el("div",{class:"card"},[
    el("label",{class:"fld"},[el("span",{},[t("roles_admins")]),fAdm]),
    el("label",{class:"fld"},[el("span",{},[t("roles_mods")]),fMod]),
    el("label",{class:"fld"},[el("span",{},[t("roles_super")]),fSup]),
    el("label",{class:"fld"},[el("span",{},[t("roles_lang")]),fLang]),
    el("label",{class:"fld"},[el("span",{},[t("roles_alerts")]),fAlerts]),
    el("p",{class:"muted small"},[t("roles_hint")]),
    el("div",{id:"rmsg"}),
    el("button",{class:"pri",onclick:saveRoles},[t("save")])
  ]);
  out.appendChild(card);
}
function saveRoles(){
  var m=$("#rmsg"); m.innerHTML="";
  var body={ allowed_user_ids:$("#radm").value, moderator_user_ids:$("#rmod").value,
    super_admin_id:$("#rsup").value.trim(), default_lang:$("#rlang").value, alerts_enabled:$("#ralerts").checked };
  api("/api/roles",{body:body}).then(function(){ m.appendChild(el("div",{class:"msg ok"},[t("saved")])); })
    .catch(function(e){ m.appendChild(el("div",{class:"msg err"},[errText(e)])); });
}

// ---- logs ----
var logTimer=null;
function tabLogs(v){
  var sub=localStorage.getItem("sw_logsub")||"sup";
  var bar=el("nav",{style:"padding:0;border:0;background:transparent;margin-bottom:10px"},
    [["sup","log_sup"],["audit","log_audit"],["nav","log_nav"]].map(function(x){
      return el("button",{class:sub===x[0]?"active":"",onclick:function(){ localStorage.setItem("sw_logsub",x[0]); render(); }},[t(x[1])]);
    }));
  var body=el("div",{id:"logbody"},[]);
  v.appendChild(el("div",{},[bar,body]));
  clearInterval(logTimer);
  if(sub==="sup") logSup(body);
  else if(sub==="audit") logAudit(body);
  else logNav(body);
}
function logSup(body){
  body.innerHTML="";
  var lvl=el("select",{id:"lglvl"}, ["ALL","INFO","WARNING","ERROR"].map(function(l){return el("option",{value:l},[l]);}));
  var nSel=el("select",{id:"lgn"}, ["150","300","600","1200"].map(function(l){return el("option",{value:l},[l]);}));
  nSel.value="300";
  var pre=el("pre",{class:"log",id:"lgpre"},["…"]);
  var auto=el("input",{type:"checkbox",id:"lgauto",checked:"checked"});
  body.appendChild(el("div",{class:"row",style:"margin-bottom:8px"},[
    el("label",{class:"small"},[t("level")+" ",lvl]),
    el("label",{class:"small"},[t("lines")+" ",nSel]),
    el("label",{class:"small"},[auto," "+t("auto")]),
    el("button",{class:"small",onclick:pullSup},[t("refresh")]),
    el("button",{class:"small",onclick:function(){ window.open("/api/log?fmt=txt&n=2000","_blank"); }},[t("download")])
  ]));
  body.appendChild(pre);
  lvl.onchange=nSel.onchange=pullSup;
  pullSup();
  logTimer=setInterval(function(){ if(!document.hidden && $("#lgauto") && $("#lgauto").checked) pullSup(); },4000);
}
function pullSup(){
  var lvl=($("#lglvl")||{}).value||"ALL", n=($("#lgn")||{}).value||"300";
  api("/api/log?level="+lvl+"&n="+n).then(function(j){
    var pre=$("#lgpre"); if(!pre) return; pre.innerHTML="";
    j.lines.forEach(function(r){
      pre.appendChild(el("span",{class:"lg-t"},[(r.ts||"").slice(5,19)+" "]));
      pre.appendChild(el("span",{class:"lg-"+(r.level||"INFO")},[(r.level?r.level[0]:" ")+" "+(r.thread?"["+r.thread+"] ":"")+r.msg+"\n"]));
    });
    pre.scrollTop=pre.scrollHeight;
  }).catch(function(){});
}
function logAudit(body){
  body.innerHTML=""; var tb=el("table",{id:"autb"},[]);
  body.appendChild(el("div",{class:"row",style:"margin-bottom:8px"},[el("button",{class:"small",onclick:pullAudit},[t("refresh")])]));
  body.appendChild(tb); pullAudit();
}
function pullAudit(){
  api("/api/audit").then(function(j){
    var tb=$("#autb"); if(!tb) return; tb.innerHTML="";
    tb.appendChild(el("tr",{},["ts","IP","user","msg"].map(function(x){return el("th",{},[x]);})));
    j.lines.slice().reverse().forEach(function(r){
      tb.appendChild(el("tr",{},[el("td",{class:"mono"},[r.ts]),el("td",{class:"mono"},[r.ip]),el("td",{},[r.user]),el("td",{},[r.msg])]));
    });
  }).catch(function(){});
}
function logNav(body){
  body.innerHTML="";
  api("/api/nav-shots").then(function(j){
    if(!j.shots.length){ body.appendChild(el("p",{class:"muted"},[t("navshots_none")])); return; }
    var g=el("div",{class:"thumbs"}, j.shots.map(function(s){
      return el("figure",{},[
        el("img",{src:"/api/nav-shot?name="+encodeURIComponent(s.name),onclick:function(e){ window.open(e.target.src,"_blank"); }}),
        el("figcaption",{},[s.name+" • "+fago(s.age)])
      ]);
    }));
    body.appendChild(g);
  }).catch(function(e){ body.appendChild(el("div",{class:"msg err"},[errText(e)])); });
}

// ---- boot ----
(function(){
  var th=localStorage.getItem("sw_theme"); if(th) document.documentElement.setAttribute("data-theme",th);
  document.addEventListener("visibilitychange",function(){ if(!document.hidden && S.authed && !S.must_change){
    if(S.tab==="dash") loadState(false); } });
  api("/api/session").then(function(j){
    S.authed=!!j.authed; S.user=j.username||""; S.csrf=j.csrf||""; S.must_change=!!j.must_change; render();
  }).catch(function(){ S.authed=false; render(); });
})();
</script>
</body>
</html>
"""
