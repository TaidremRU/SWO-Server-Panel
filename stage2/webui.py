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
import base64
import collections
import copy
import hashlib
import http.cookies
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import activity
import authguard
import common
import gamectl
import i18n
import metrics
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
REAUTH_ELEVATE_SECONDS = 60  # окно после ввода пароля, когда чувствительные действия его не переспрашивают


def _now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class _Bad(Exception):
    """Ошибка валидации ввода — отдаётся клиенту как 400."""


# ------------------------------------------------------------------ настройки бота
# (section, title_ru, title_en, [(path, label_ru, label_en, type, hint_ru)])
# type: bool int float str secret strlist intlist intn (int|None) json
SETTINGS_SCHEMA = [
    ("general", "Общее", "General", [
        ("poll_seconds", "Интервал опроса, с", "Poll interval, s", "int", "как часто супервизор снимает статус"),
        ("initial_delay_seconds", "Задержка старта, с", "Startup delay, s", "int", ""),
        ("task_name", "Имя задачи планировщика", "Scheduled task name", "str", "менять только вместе с самой задачей"),
        ("game_server_host", "Хост игрового сервера", "Game server host", "str", "для будущего пакетного слоя"),
    ]),
    ("webui", "Веб-панель", "Web panel", [
        ("webui.host", "Хост", "Host", "str", "0.0.0.0 = все интерфейсы; нужен перезапуск"),
        ("webui.port", "Порт", "Port", "int", "нужен перезапуск + правило фаервола"),
        ("webui.enabled", "Включена", "Enabled", "bool", ""),
        ("webui.allowed_nets", "Разрешённые адреса", "Allowed addresses", "strlist",
         "IP или подсети через запятую (192.168.0.0/24, 10.1.2.3); пусто = любые локальные сети "
         "(10.*, 172.16–31.*, 192.168.*, link-local); 127.0.0.1 разрешён всегда"),
        ("webui.title", "Название панели", "Panel title", "str",
         "шапка и вкладка браузера; пусто = SigmaSteamBot. Иконка — ниже, «Иконка панели»"),
    ]),
    ("playerweb", "Панель игроков", "Player panel", [
        ("playerweb.enabled", "Включена", "Enabled", "bool",
         "вход ником + игровым паролем, только своё; нужен перезапуск задачи"),
        ("playerweb.host", "Хост", "Host", "str", "0.0.0.0 = все интерфейсы; нужен перезапуск"),
        ("playerweb.port", "Порт", "Port", "int", "по умолчанию 80; нужен перезапуск + правило фаервола (setup.bat)"),
        ("playerweb.allowed_nets", "Разрешённые адреса", "Allowed addresses", "strlist",
         "пусто = пускать всех; для интернета — только через прокси с HTTPS"),
        ("playerweb.title", "Название", "Title", "str", "пусто = как у админ-панели"),
        ("playerweb.admin_proxy", "Админка через панель игроков", "Admin panel via player panel", "bool",
         "кнопка «Админка» у игроков с ролью в игре (модератор/админ/мастер) и /admin/ на порту панели игроков; вход — по отдельному админскому паролю"),
    ]),
    ("watchdog", "Watchdog", "Watchdog", [
        ("watchdog.enabled", "Включён", "Enabled", "bool", ""),
        ("watchdog.auto_start_steam", "Авто-запуск Steam", "Auto-start Steam", "bool", ""),
        ("watchdog.auto_start_game", "Авто-запуск игры", "Auto-start game", "bool", ""),
        ("watchdog.auto_login", "Авто-вход в игру", "Auto-login", "bool", ""),
        ("watchdog.grace_after_launch_seconds", "Пауза после запуска, с", "Grace after launch, s", "int", ""),
        ("watchdog.max_restarts_per_hour", "Макс. перезапусков/час", "Max restarts/hour", "int", ""),
        ("watchdog.login_settle_seconds", "Пауза перед авто-входом, с", "Login settle, s", "int", ""),
        ("watchdog.login_retry_seconds", "Повтор входа, с", "Login retry, s", "int", ""),
    ]),
    ("monitor", "Монитор сервера", "Server monitor", [
        ("monitor.enabled", "Включён", "Enabled", "bool", ""),
        ("monitor.server_name", "Имя сервера", "Server name", "str", "подсветка в списке лобби и алерты о пропаже"),
        ("monitor.interval_seconds", "Интервал, с", "Interval, s", "int", ""),
        ("monitor.misses_before_alert", "Промахов до алерта", "Misses before alert", "int", ""),
        ("monitor.repeat_alert_seconds", "Повтор алерта, с", "Repeat alert, s", "int", "0 = без напоминаний"),
    ]),
    ("discord", "Discord-стата", "Discord stats", [
        ("discord.enabled", "Включена", "Enabled", "bool", ""),
        ("discord.webhook_url", "Webhook URL", "Webhook URL", "secret", "вебхук канала Discord (Настройки канала → Интеграции → Вебхуки); ходит через тот же прокси, что и Telegram"),
        ("discord.interval_seconds", "Интервал, с", "Interval, s", "int", "мин. 60; одно сообщение правится на месте, не спамит"),
    ]),
    ("players", "Данные локального сервера", "Local server data", [
        ("players.enabled", "Читать файлы сервера", "Read server files", "bool", ""),
        ("players.world", "Имя мира", "World name", "str", "пусто = автовыбор по свежести analytics.txt"),
        ("players.world_dir", "Путь к миру", "World dir", "str", "пусто = по localserver_root + world"),
        ("players.localserver_root", "Корень LocalServer", "LocalServer root", "str", "пусто = стандартный AppData-путь"),
        ("players.twink_ignore_ips", "Игнор-IP для твинков", "Twink ignore IPs", "strlist", "через запятую; на релее это 127.0.0.1, 127.0.0.2"),
        ("players.tech_track.enabled", "Трекинг техов/бустеров", "Tech tracking", "bool", ""),
        ("players.tech_track.interval_seconds", "Интервал трекинга, с", "Tracking interval, s", "int", ""),
        ("players.backup_rotation.enabled", "Ротация бэкапов мира", "World backup rotation", "bool",
         "игра пишет <мир>\\backup\\arhN.zip раз в timeBackupServer и не удаляет старые; панель хранит свежие + по одному в день"),
        ("players.backup_rotation.keep_recent", "Хранить свежих архивов", "Keep recent archives", "int", "48 = сутки при бэкапе раз в 30 мин"),
        ("players.backup_rotation.keep_days", "Хранить по одному в день, дней", "Keep one per day, days", "int", ""),
    ]),
    ("telegram", "Telegram", "Telegram", [
        ("telegram.allowed_user_ids", "Админы (ID)", "Admins (IDs)", "intlist", "полный доступ; нужен ≥1"),
        ("telegram.moderator_user_ids", "Модераторы (ID)", "Moderators (IDs)", "intlist", "ограниченный набор команд"),
        ("telegram.super_admin_id", "Главный админ (ID)", "Super admin (ID)", "intn", "получатель аудита; должен быть среди админов"),
        ("telegram.default_lang", "Язык по умолчанию", "Default language", "str", "ru или en"),
        ("telegram.alerts_enabled", "Слать алерты", "Send alerts", "bool", ""),
        ("telegram.poll_timeout", "Long-poll таймаут, с", "Long-poll timeout, s", "int", ""),
        ("telegram.proxy", "Прокси", "Proxy", "str", "socks5h://host:port (с VM Telegram заблокирован)"),
        ("telegram.token", "Токен бота", "Bot token", "secret", "пусто = не менять"),
    ]),
    ("serverlist", "Steam / список серверов", "Steam / server list", [
        ("steam_web_api_key", "Steam Web API key", "Steam Web API key", "secret", "пусто = не менять"),
        ("steam_api_dll", "Путь steam_api64.dll", "steam_api64.dll path", "str", ""),
        ("python_exe", "python.exe для subprocess", "python.exe for subprocess", "str", "пусто = авто"),
    ]),
]
_SETTINGS_FIELDS = {p: (typ, lr) for _, _, _, fs in SETTINGS_SCHEMA for (p, lr, le, typ, hint) in fs}


def _cfg_get_path(d, path):
    cur = d
    for seg in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
    return cur


def _cfg_set_path(d, path, val):
    segs = path.split(".")
    cur = d
    for seg in segs[:-1]:
        cur = cur.setdefault(seg, {})
        if not isinstance(cur, dict):
            raise _Bad("путь %s занят не-объектом" % path)
    cur[segs[-1]] = val


def _coerce_setting(path, typ, raw):
    """Привести значение к типу поля; бросает _Bad."""
    try:
        if typ == "bool":
            return bool(raw) if not isinstance(raw, str) else raw.strip().lower() in ("1", "true", "on", "yes", "да")
        if typ == "int":
            return int(str(raw).strip())
        if typ == "float":
            return float(str(raw).strip())
        if typ == "intn":
            s = str(raw).strip()
            return None if s in ("", "none", "null", "-") else int(s)
        if typ in ("str", "secret"):
            return str(raw)
        if typ == "strlist":
            if isinstance(raw, list):
                return [str(x).strip() for x in raw if str(x).strip()]
            return [x.strip() for x in re.split(r"[\s,;]+", str(raw)) if x.strip()]
        if typ == "intlist":
            if isinstance(raw, list):
                src = raw
            else:
                src = [x for x in re.split(r"[\s,;]+", str(raw)) if x]
            out, seen = [], set()
            for x in src:
                n = int(str(x).strip())
                if n not in seen:
                    seen.add(n)
                    out.append(n)
            return out
        if typ == "json":
            return json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as e:
        raise _Bad("поле «%s»: не привести к %s (%s)" % (path, typ, e))
    raise _Bad("неизвестный тип поля %s" % typ)


# --------------------------------------------------------------------------- auth
# gm — главный уровень: всё, что у админа, + журнал активности/аудит/блокировки входа
ROLES = ("gm", "admin", "moderator", "viewer")
ROLE_LEVEL = {"viewer": 1, "moderator": 2, "admin": 3, "gm": 4}
_NAME_RX = re.compile(r"^[A-Za-z0-9_.\-]{2,32}$")


class AuthStore:
    """Пользователи веб-панели в ``webui_auth.json`` (PBKDF2-HMAC-SHA256):
    ``{"users": {name: {hash, salt, iterations, role, must_change, updated}}}``.

    Роли — как у Telegram-бота: **gm** (всё + журнал активности, аудит,
    блокировки входа), **admin** (всё остальное), **moderator** (просмотр +
    перезапуск игры/вход, скриншот, бан/разбан, нарушения/твинки), **viewer**
    (только просмотр, без паролей/приватов/IP игроков). Старый формат с одним
    пользователем (username/salt/hash) при загрузке становится админом."""

    ITERS = 200_000

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._load_or_init()

    def _load_or_init(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if "users" not in d:                       # старый формат — один админ
                if not {"username", "salt", "hash"} <= set(d):
                    raise ValueError("неполный файл")
                u = {k: d[k] for k in ("algo", "iterations", "salt", "hash", "must_change", "updated") if k in d}
                u["role"] = "admin"
                d = {"users": {d["username"]: u}}
                self.d = d
                self._save()
                logging.info("webui: %s переведён на несколько пользователей", self.path)
            if not d.get("users"):
                raise ValueError("нет пользователей")
            self.d = d
            self._ensure_gm()
        except FileNotFoundError:
            self.d = {"users": {"admin": self._make("admin", "gm", True)}}
            self._save()
            logging.warning("webui: создан %s — вход admin/admin, СМЕНИТЕ ПАРОЛЬ при первом входе", self.path)
        except Exception:  # noqa: BLE001
            logging.exception("webui: %s повреждён — пересоздаю admin/admin", self.path)
            self.d = {"users": {"admin": self._make("admin", "gm", True)}}
            self._save()

    def _ensure_gm(self):
        """Появление роли gm: если GM ещё нет — им становится локальный «admin»
        (учётка восстановления через /webui reset), иначе первый локальный админ.
        Игроки с ролью «Мастер» в игре получают gm сами (см. WebUI._effective_role)."""
        users = self.d["users"]
        if any(u.get("role") == "gm" for u in users.values()):
            return
        local = [n for n, u in users.items() if u.get("role", "admin") == "admin" and u.get("game_uid") is None]
        if local:
            name = "admin" if "admin" in local else local[0]
            users[name]["role"] = "gm"
            self._save()
            logging.warning("webui: %s получил роль gm (журнал активности)", name)

    def _make(self, pw, role, must_change):
        salt = secrets.token_bytes(16)
        return {"algo": "pbkdf2_sha256", "iterations": self.ITERS, "salt": salt.hex(),
                "hash": self._hash(pw, salt, self.ITERS), "role": role,
                "must_change": bool(must_change), "updated": _now_iso()}

    @staticmethod
    def _hash(pw, salt, iters):
        return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iters).hex()

    def _u(self, user):
        return (self.d.get("users") or {}).get(user)

    @property
    def username(self):
        """Первый админ (для строки в логе при старте)."""
        return next((n for n, u in self.d["users"].items() if u.get("role") in ("gm", "admin")), "admin")

    def role_of(self, user):
        u = self._u(user)
        return (u.get("role") if u.get("role") in ROLES else "admin") if u else None

    def must_change_of(self, user):
        u = self._u(user)
        return bool(u and u.get("must_change"))

    @property
    def must_change(self):
        """Хотя бы у одного админа пароль по умолчанию (для лога при старте)."""
        return any(u.get("must_change") for u in self.d["users"].values() if u.get("role") in ("gm", "admin"))

    def verify(self, user, pw):
        u = self._u(user)
        if not u:
            # время ответа не должно выдавать, есть ли такой пользователь
            self._hash(pw, b"0" * 16, self.ITERS)
            return False
        got = self._hash(pw, bytes.fromhex(u["salt"]), int(u.get("iterations", self.ITERS)))
        return secrets.compare_digest(got, u.get("hash", ""))

    def users(self):
        return [{"name": n, "role": self.role_of(n), "must_change": bool(u.get("must_change")),
                 "updated": u.get("updated"), "game_uid": u.get("game_uid"), "nick": u.get("nick")}
                for n, u in sorted(self.d["users"].items())]

    def _admins(self):
        return [n for n in self.d["users"] if self.role_of(n) in ("gm", "admin")]

    def _gms(self):
        return [n for n in self.d["users"] if self.role_of(n) == "gm"]

    def set_password(self, user, newpw, newuser=None):
        """Смена своего пароля (и, по желанию, логина) — снимает must_change."""
        with self._lock:
            u = self.d["users"].pop(user)
            name = newuser or user
            if name != user and name in self.d["users"]:
                self.d["users"][user] = u
                raise _Bad("пользователь %s уже есть" % name)
            self.d["users"][name] = self._make(newpw, u.get("role", "admin"), False)
            self._save()

    def add_user(self, name, pw, role):
        if not _NAME_RX.match(name or ""):
            raise _Bad("логин: 2–32 символа, латиница, цифры, _ . -")
        if role not in ROLES:
            raise _Bad("неизвестная роль")
        with self._lock:
            if name in self.d["users"]:
                raise _Bad("пользователь %s уже есть" % name)
            self.d["users"][name] = self._make(pw, role, True)   # сменит пароль при первом входе
            self._save()

    def set_role(self, name, role):
        if role not in ROLES:
            raise _Bad("неизвестная роль")
        with self._lock:
            u = self._u(name)
            if not u:
                raise _Bad("нет такого пользователя")
            if self.role_of(name) == "gm" and role != "gm" and len(self._gms()) <= 1:
                raise _Bad("нельзя оставить панель без GM")
            if self.role_of(name) in ("gm", "admin") and role not in ("gm", "admin") and len(self._admins()) <= 1:
                raise _Bad("нельзя оставить панель без админа")
            u["role"] = role
            u["updated"] = _now_iso()
            self._save()

    def reset_password(self, name, pw):
        with self._lock:
            u = self._u(name)
            if not u:
                raise _Bad("нет такого пользователя")
            self.d["users"][name] = self._make(pw, u.get("role", "viewer"), True)
            self._save()

    def delete_user(self, name):
        with self._lock:
            if not self._u(name):
                raise _Bad("нет такого пользователя")
            if self.role_of(name) == "gm" and len(self._gms()) <= 1:
                raise _Bad("нельзя удалить последнего GM")
            if self.role_of(name) in ("gm", "admin") and len(self._admins()) <= 1:
                raise _Bad("нельзя удалить последнего админа")
            self.d["users"].pop(name)
            self._save()

    def game_uid_of(self, user):
        u = self._u(user)
        return u.get("game_uid") if u else None

    def user_for_game(self, uid):
        return next((n for n, u in self.d["users"].items() if u.get("game_uid") == int(uid)), None)

    def add_game_user(self, uid, nick, pw, role):
        """Пользователь панели, привязанный к игроку (вход — из панели игроков).
        Логин — ник, если подходит, иначе player<uid>. -> логин."""
        with self._lock:
            name = nick if _NAME_RX.match(nick or "") and nick not in self.d["users"] else "player%d" % int(uid)
            if name in self.d["users"]:
                name = "player%d_%s" % (int(uid), secrets.token_hex(2))
            u = self._make(pw, role, False)
            u.update(game_uid=int(uid), nick=nick)
            self.d["users"][name] = u
            self._save()
            return name

    def reset(self):
        """Сброс входа admin/admin + must_change (забытый пароль, без доступа к RDP) —
        дергается командой /webui reset из Telegram. Остальные пользователи остаются."""
        with self._lock:
            self.d["users"]["admin"] = self._make("admin", "gm", True)
            self._save()
        logging.warning("webui: вход admin сброшен на admin/admin через /webui reset")

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
    """Сессии в памяти: token -> {user, ip, csrf, born, seen, elevated_until}."""

    def __init__(self):
        self._d = {}
        self._lock = threading.Lock()

    def new(self, user, ip):
        tok = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with self._lock:
            self._d[tok] = {"user": user, "ip": ip, "csrf": csrf,
                            "born": time.time(), "seen": time.time(), "elevated_until": 0}
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
            d = dict(s)
            d["tok"] = tok
            return d

    def drop(self, tok):
        with self._lock:
            self._d.pop(tok, None)

    def drop_user(self, user):
        """Выкинуть все сессии пользователя (сброс пароля, удаление)."""
        with self._lock:
            for t in [t for t, s in self._d.items() if s["user"] == user]:
                self._d.pop(t, None)

    def elevate(self, tok, seconds):
        """Отметить сессию как «подтверждённую паролем» на ``seconds`` вперёд —
        чувствительные операции (см. ``WebUI._reauth``) в этом окне пароль не
        переспрашивают."""
        with self._lock:
            s = self._d.get(tok)
            if s is not None:
                s["elevated_until"] = time.time() + seconds

    def is_elevated(self, tok):
        with self._lock:
            s = self._d.get(tok)
            return bool(s and s["elevated_until"] > time.time())


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
def _parse_nets(items):
    """Список «IP / подсеть» -> [ip_network]. ValueError на мусоре."""
    out = []
    for x in items or []:
        x = str(x).strip()
        if x:
            out.append(ipaddress.ip_network(x, strict=False))
    return out


def _ip_allowed(ip, nets):
    """Пускать ли адрес. Loopback — всегда (зайти с самой VM, если список
    настроен неудачно). Пустой список — любые частные/локальные сети."""
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.is_loopback:
        return True
    if not nets:
        return a.is_private or a.is_link_local
    return any(a.version == n.version and a in n for n in nets)


def _eff_super(tg):
    """Кто реально главный админ бота: super_admin_id или первый из админов (как в bot.apply_roles)."""
    return tg.get("super_admin_id") or ((tg.get("allowed_user_ids") or [None])[0])


def _cip(h):
    """Адрес клиента: с учётом доверенного прокси (см. dispatch), иначе — сокет."""
    return getattr(h, "real_ip", None) or h.client_address[0]


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
        ac = cfg.get("activity") or {}
        self.act = activity.ActivityLog(os.path.join(base, "logs", "activity.jsonl"),
                                        max_mb=ac.get("max_mb", 20), keep=ac.get("keep", 10))
        self.guard = authguard.Guard(os.path.join(base, "auth_guard.json"),
                                     alert=getattr(bot, "push_super", None) if bot else None,
                                     on_event=lambda r: self.act.write(dict(r, src="guard")))
        players.GAME_EXE = os.path.basename(cfg.get("game_exe") or "SigmaWorld.exe").lower()
        mc = cfg.get("metrics") or {}
        self.metrics = metrics.Metrics(cfg, base, interval=mc.get("interval_seconds", 30), max_mb=mc.get("max_mb", 30))
        self._audit_path = os.path.join(base, "webui_audit.log")
        self._audit_lock = threading.Lock()
        self._tt_state = os.path.join(base, "tech_track_state.json")
        self._tt_log = os.path.join(base, "logs", "tech_track.jsonl")
        self._ct_state = os.path.join(base, "clan_track_state.json")
        self._ct_events = os.path.join(base, "logs", "clan_events.jsonl")
        self._ct_points = os.path.join(base, "logs", "clan_points.jsonl")
        self._econ_hist = os.path.join(base, "logs", "economy_daily.jsonl")
        self._inv_state = os.path.join(base, "inv_track_state.json")
        self._susp_log = os.path.join(base, "logs", "suspicious.jsonl")
        self._heavy = {}  # name -> {"cache": (ts, payload)|None, "job": job|None}
        self._buff_path = os.path.join(base, "buff_notepad.json")
        self._stop = threading.Event()
        self._srv = None
        self._thread = None
        self._srv_cache = None  # (ts, payload)
        self._players_cache = None  # (ts, payload)
        self._stats_cache = None  # (ts, payload)
        self._world_cache = None  # (ts, payload)
        self._health_cache = None  # (ts, payload)
        self._space_cache = None  # (ts, payload)
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
        tt = (self.cfg.get("players", {}) or {}).get("tech_track", {}) or {}
        if tt.get("enabled", True):
            threading.Thread(target=self._tech_track_loop, name="techtrack", daemon=True).start()
        if (self.cfg.get("metrics") or {}).get("enabled", True):
            self.metrics.start()
        logging.info("webui: слушаю http://%s:%d/ — вход %s%s", host, port, self.auth.username,
                     "  (СМЕНИТЕ ПАРОЛЬ)" if self.auth.must_change else "")

    def stop(self):
        self._stop.set()
        self.metrics.stop()
        try:
            if self._srv:
                self._srv.shutdown()
                self._srv.server_close()
                logging.info("webui: остановлена")
        except Exception:  # noqa: BLE001
            logging.exception("webui: ошибка остановки")

    def _tech_track_loop(self):
        """Раз в N минут снимает techList/techBooster/researchTech всех игроков и
        дописывает изменения в logs\\tech_track.jsonl (у игры такого лога нет)."""
        tt = (self.cfg.get("players", {}) or {}).get("tech_track", {}) or {}
        iv = max(120, int(tt.get("interval_seconds", 600)))
        if self._stop.wait(min(iv, 120)):
            return
        last_scan = None
        while not self._stop.is_set():
            try:
                prev_research = {k: v.get("research") for k, v in
                                 ((players._read_json(self._tt_state) or {}).get("users") or {}).items()}
                now = time.time()
                ev = players.tech_track_scan(self.cfg, self._tt_state, self._tt_log)
                if ev:
                    logging.info("techtrack: %d изменений", len(ev))
                    if last_scan:
                        sus = players.research_check(self.cfg, ev, prev_research, now - last_scan, self._susp_log)
                        if sus:
                            logging.warning("susp: быстрые исследования — %d", len(sus))
                last_scan = now
            except Exception:  # noqa: BLE001
                logging.exception("techtrack: ошибка прохода")
            try:
                sus = players.inv_track_scan(self.cfg, self._inv_state, self._susp_log)
                if sus:
                    logging.warning("susp: всплески инвентаря — %d", len(sus))
            except Exception:  # noqa: BLE001
                logging.exception("susp: ошибка прохода инвентарей")
            try:
                if players.economy_snapshot_due(self._econ_hist):
                    d = players.economy_report(self.cfg, self._econ_hist)
                    if d.get("ok"):
                        self._heavy.setdefault("economy", {})["cache"] = (time.time(), d)
                        logging.info("economy: суточный снимок (%s предметов, %.0f c)",
                                     len(d.get("items") or []), d.get("scan_sec") or 0)
            except Exception:  # noqa: BLE001
                logging.exception("economy: суточный снимок")
            try:
                self._backup_rotation()
            except Exception:  # noqa: BLE001
                logging.exception("backup-rotate: ошибка")
            try:
                ev = players.clan_track_scan(self.cfg, self._ct_state, self._ct_events, self._ct_points)
                if ev:
                    logging.info("clantrack: %d событий", len(ev))
            except Exception:  # noqa: BLE001
                logging.exception("clantrack: ошибка прохода")
            if self._stop.wait(iv):
                return

    def _backup_rotation(self):
        """Ротация архивов игры (players.rotate_world_backups) — не чаще раза в час и
        только в «тихом окне» 2–20 мин после свежего архива, чтобы не столкнуться
        с игрой, пишущей следующий."""
        br = ((self.cfg.get("players") or {}).get("backup_rotation") or {})
        if not br.get("enabled") or time.time() - getattr(self, "_rot_last", 0) < 3600:
            return
        wd = players.find_world_dir(self.cfg)
        bdir = os.path.join(wd, "backup") if wd else ""
        try:
            newest = max(os.path.getmtime(os.path.join(bdir, f)) for f in os.listdir(bdir) if players._ARH_RX.match(f))
        except (OSError, ValueError):
            return
        if not 120 <= time.time() - newest <= 1200:
            return
        r = players.rotate_world_backups(self.cfg, int(br.get("keep_recent", 48)), int(br.get("keep_days", 14)))
        self._rot_last = time.time()
        if r.get("ok") and r.get("deleted"):
            logging.info("backup-rotate: удалено %d архивов (%.0f МБ), осталось %d (%.0f МБ), перенумеровано %d",
                         r["deleted"], r["freed_mb"], r["kept"], r["kept_mb"], r["renamed"])
            self.audit("-", "system", "РОТАЦИЯ бэкапов мира: удалено %d (%.0f МБ), осталось %d"
                       % (r["deleted"], r["freed_mb"], r["kept"]))
        elif not r.get("ok"):
            logging.info("backup-rotate: %s", r.get("error"))

    # ------------------------------------------------------------------- audit
    _AUDIT_MAX_BYTES = 5 * 1024 * 1024  # ротация: .log -> .log.1 (один бэкап)

    def audit(self, ip, user, msg):
        line = "%s\t%s\t%s\t%s\n" % (_now_iso(), ip, user, msg)
        try:
            with self._audit_lock:
                try:
                    if os.path.getsize(self._audit_path) > self._AUDIT_MAX_BYTES:
                        old = self._audit_path + ".1"
                        try:
                            os.remove(old)
                        except OSError:
                            pass
                        os.rename(self._audit_path, old)
                except OSError:
                    pass  # файла ещё нет — обычное дело при первом запуске
                with open(self._audit_path, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            logging.exception("webui: не удалось записать аудит")
        logging.info("webui audit: %s [%s] %s", user, ip, msg)
        self.act.write({"src": "admin", "ev": "audit", "user": user, "ip": ip, "d": msg})

    # ------------------------------------------------------------- http helpers
    def _send(self, h, status, ctype, body, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        h._st = status
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
        except ConnectionError:  # разрыв, сброс, обрыв (WinError 10053) — клиент ушёл
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
            v = v if isinstance(v, dict) else {}
        except Exception:  # noqa: BLE001
            v = {}
        h._req_body = v
        return v

    def _session_of(self, h):
        c = http.cookies.SimpleCookie(h.headers.get("Cookie", ""))
        tok = c["sid"].value if "sid" in c else ""
        return (tok, self.sessions.get(tok)) if tok else ("", None)

    # ---------------------------------------------------------------- dispatch
    def _allowed_nets(self):
        raw = tuple((self.cfg.get("webui") or {}).get("allowed_nets") or [])
        if getattr(self, "_nets_key", None) != raw:
            try:
                self._nets = _parse_nets(raw)
            except ValueError:
                logging.error("webui: allowed_nets с ошибкой (%s) — пускаю только локальные сети", raw)
                self._nets = []
            self._nets_key = raw
        return self._nets

    # ------------------------------------------------- вход игроков-стаффа (порт 80)
    # роль в игре (user<N>.json "role": 0 игрок, 1 модератор, 2 админ, 3 мастер) -> роль панели
    GAME_ROLE_PANEL = {1: "moderator", 2: "admin", 3: "gm"}

    def game_panel_role(self, uid):
        """Какая роль панели положена игроку по его роли в игре (None — никакая)."""
        wd = players.find_world_dir(self.cfg)
        raw = players._read_json(players._user_file(wd, int(uid))) if wd else {}
        try:
            return self.GAME_ROLE_PANEL.get(int(raw.get("role") or 0))
        except (TypeError, ValueError):
            return None

    def _effective_role(self, user):
        """Роль пользователя панели; у привязанных к игроку — не выше роли в игре
        (перепроверка раз в минуту), без роли в игре — None."""
        role = self.auth.role_of(user)
        uid = self.auth.game_uid_of(user)
        if role is None or uid is None:
            return role
        cache = getattr(self, "_grole", {})
        hit = cache.get(uid)
        if not hit or time.time() - hit[0] > 60:
            hit = (time.time(), self.game_panel_role(uid))
            cache[uid] = hit
            self._grole = cache
        g = hit[1]
        if not g:
            return None
        if g == "gm" and role == "admin":   # привязан как админ, в игре стал Мастером
            return "gm"
        return role if ROLE_LEVEL[role] <= ROLE_LEVEL[g] else g

    def session_ok(self, h):
        """Есть ли у запроса действующая сессия админки с ролью (для /admin/ на порту
        панели игроков: без неё — 403, даже формы входа не видно)."""
        tok, sess = self._session_of(h)
        return bool(sess and self._effective_role(sess["user"]))

    def enter_as_game_user(self, h, uid, nick, password, new_password, ip):
        """Вход в админку из панели игроков. Первый раз — задать отдельный админский
        пароль (не игровой), дальше — вход по нему. -> (status, dict, set_cookie|None)."""
        role = self.game_panel_role(uid)
        if not role:
            return 403, {"error": "forbidden"}, None
        user = self.auth.user_for_game(uid)
        if user is None:
            if not new_password:
                return 200, {"ok": False, "need_setup": True}, None
            code = players.player_code(self.cfg, uid) or ""
            if len(new_password) < 8 or new_password == code or new_password.lower() in (nick.lower(), "admin", "password"):
                return 400, {"error": "weak", "detail": "не короче 8 символов, не игровой пароль и не ник"}, None
            user = self.auth.add_game_user(uid, nick, new_password, role)
            self.audit(ip, user, "ПРИВЯЗКА игрока #%s (%s) к панели, роль %s" % (uid, nick, role))
        elif not self.auth.verify(user, password or ""):
            self.act.write({"src": "admin", "ev": "login_fail", "user": user, "uid": uid, "ip": ip, "via": "/admin",
                            "d": "неверный админский пароль (кнопка «Админка» в панели игрока %s)" % nick})
            self.audit(ip, user, "НЕВЕРНЫЙ админский пароль (вход из панели игроков)")
            return 403, {"error": "bad_password"}, None
        tok, csrf = self.sessions.new(user, ip)
        self.act.write({"src": "admin", "ev": "login_ok", "user": user, "uid": uid, "ip": ip, "via": "/admin",
                        "role": self._effective_role(user), "sid": hashlib.sha256(tok.encode()).hexdigest()[:10],
                        "ua": (h.headers.get("User-Agent") or "")[:200], "d": "из панели игрока %s" % nick})
        self.audit(ip, user, "вход в панель из панели игроков")
        return 200, {"ok": True, "url": "/admin/"}, "sid=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (tok, SESSION_TTL)

    # ------------------------------------------------------------ права по ролям
    # viewer — просмотр; moderator — + как в Telegram (скриншот, перезапуск игры,
    # вход) и бан/разбан, нарушения, твинки; admin — всё. Не указанный POST —
    # только админу, не указанный GET — всем вошедшим.
    _MOD_ROUTES = {"shot", "suspicious", "twinks"}
    _MOD_OPS = {"restartgame", "login"}
    _MOD_MODERATE = {"ban", "unban"}
    _ADMIN_ROUTES = {"audit", "log", "settings", "roles", "login-flow", "nav-shots", "nav-shot", "admin-tools",
                     "world-backup", "players-csv", "users", "space-gen",
                     # рецепты микстур/кулинарии — знание только для админа
                     "buff-notepad", "buff-ingredients", "buff-optimize", "food-ingredients", "food-lib", "food-optimize"}
    _POST_VIEW = {"job", "track"}
    # журнал активности, аудит, блокировки входа — только GM
    _GM_ROUTES = {"activity-log", "guard", "audit"}

    def _need_level(self, route, method, q):
        if route.startswith("players/"):
            parts = route.split("/")
            sub = parts[2] if len(parts) > 2 else ""
            if sub in ("secret", "sensitive", "inventory"):
                return 3
            if sub == "view-as":
                return 4        # «глазами игрока» — только GM
            if sub in ("where", "journal"):
                return 2
            if sub == "moderate":
                return 2        # точнее — в _api_player_moderate (бан/разбан модератору, остальное админу)
            return 1
        if route in self._GM_ROUTES:
            return 4
        if route in self._ADMIN_ROUTES:
            return 3
        if route == "action":
            return 2            # конкретная операция проверяется в _api_action
        if route in self._MOD_ROUTES:
            return 2
        if route == "favicon" and method == "POST":
            return 3
        if route == "server-chat" and method == "POST":
            return 3            # приватный чат сервера
        if method == "POST" and route not in self._POST_VIEW:
            return 3
        return 1

    def _proxied_page(self):
        """Та же страница, но с адресами под /admin/ (панель игроков отдаёт админку
        на своём порту, см. playerweb.admin_proxy)."""
        if getattr(self, "_ppage", None) is None:
            self._ppage = (PAGE.replace('"/api/', '"/admin/api/').replace('"/favicon.ico', '"/admin/favicon.ico'))
        return self._ppage

    def _trusted(self):
        raw = tuple((self.cfg.get("webui") or {}).get("trusted_proxies") or [])
        if getattr(self, "_tp_key", None) != raw:
            self._tp, self._tp_key = authguard.parse_nets(raw), raw
        return self._tp

    def dispatch(self, h, method, prefix=""):
        """Обработка запроса + строка в журнал активности (для GM)."""
        t0 = time.time()
        if not getattr(h, "real_ip", None):      # через панель игроков — адрес уже определила она
            h.real_ip = authguard.client_ip(h, self._trusted())
        h._via = prefix or None
        try:
            return self._dispatch(h, method, prefix)
        finally:
            self.metrics.http("admin", (time.time() - t0) * 1000)
            try:
                self._log_request(h, method, t0)
            except Exception:  # noqa: BLE001
                logging.exception("webui: журнал активности")

    _NOLOG_PATHS = {"/favicon.ico"}

    def _log_request(self, h, method, t0):
        if getattr(h, "_nolog", False):
            return
        path, _, qs = h.path.partition("?")
        if path in self._NOLOG_PATHS:
            return
        st = getattr(h, "_st", None)
        if path.startswith("/api/"):
            ev = "denied" if st in (401, 403, 429) and path != "/api/session" else "req"
        elif path in ("/", "/index.html"):
            ev = "page"
        elif path == "/healthz":
            ev = "req"
        else:
            ev = "probe"            # чужие адреса — сканеры, подбор путей
        ip = _cip(h)
        sid = getattr(h, "_sid", None)
        rep = 0
        if method == "GET" and ev in ("req", "page"):
            rep = self.act.dedup(("a", sid or ip, h.path))
            if rep is True:
                return
        q = {k: (v[0] if len(v) == 1 else v) for k, v in urllib.parse.parse_qs(qs).items()}
        rec = {"src": "admin", "ev": ev, "user": getattr(h, "_who", None), "role": getattr(h, "_role", None),
               "ip": ip, "sid": sid, "m": method, "path": path, "q": activity.redact(q) if q else None,
               "st": st, "ms": int((time.time() - t0) * 1000), "rep": rep or None, "via": getattr(h, "_via", None)}
        if method == "POST":
            rec["body"] = activity.redact(getattr(h, "_req_body", None))
        if ev in ("page", "probe") or (ev == "denied" and not sid):
            rec["ua"] = (h.headers.get("User-Agent") or "")[:200]
        self.act.write(rec)

    def _dispatch(self, h, method, prefix=""):
        """``prefix="/admin"`` — запрос пришёл через панель игроков (порт 80): путь
        уже без префикса, webui.allowed_nets не применяется (адреса отсекает сама
        панель игроков по playerweb.allowed_nets), вход — как обычно, по паролю."""
        ip = _cip(h)
        if not prefix and not _ip_allowed(ip, self._allowed_nets()):
            now = time.time()
            seen = getattr(self, "_denied_log", {})
            if now - seen.get(ip, 0) > 600:          # не спамить лог на каждый запрос
                logging.warning("webui: отказ в доступе с %s (не в webui.allowed_nets)", ip)
                seen[ip] = now
                self._denied_log = seen
            try:
                return self._send(h, 403, "text/plain; charset=utf-8",
                                  "403: доступ к панели с этого адреса запрещён".encode("utf-8"))
            except Exception:  # noqa: BLE001
                return None
        try:
            path, _, qs = h.path.partition("?")
            q = urllib.parse.parse_qs(qs)
            if path in ("/", "/index.html") and method == "GET":
                return self._send(h, 200, "text/html; charset=utf-8", self._proxied_page() if prefix else PAGE,
                                  {"Cache-Control": "no-store"})
            if path == "/favicon.ico":
                return self._send_favicon(h)
            if path == "/healthz":
                return self._api_healthz(h)
            if not path.startswith("/api/"):
                return self._send(h, 404, "text/plain; charset=utf-8", b"not found")

            route = path[len("/api/"):].strip("/")
            if route == "session" and method == "GET":
                return self._api_session(h)
            if route == "login" and method == "POST":
                return self._api_login(h)

            tok, sess = self._session_of(h)
            if sess and not self.auth.role_of(sess["user"]):     # пользователя удалили
                self.sessions.drop(tok)
                sess = None
            if not sess:
                return self._json(h, {"error": "auth"}, 401)
            h._who, h._sid = sess["user"], hashlib.sha256(tok.encode()).hexdigest()[:10]

            if method == "POST":
                given = h.headers.get("X-CSRF-Token", "")
                if not given or not secrets.compare_digest(given, sess["csrf"]):
                    return self._json(h, {"error": "csrf"}, 403)

            if route == "logout" and method == "POST":
                self._login_event(h, "logout", sess["user"], sid=h._sid)
                self.sessions.drop(tok)
                return self._json(h, {"ok": True}, set_cookie="sid=; Path=/; Max-Age=0")
            if route == "password" and method == "POST":
                return self._api_password(h, sess)

            if self.auth.must_change_of(sess["user"]):
                return self._json(h, {"error": "must_change"}, 403)

            role = self._effective_role(sess["user"])
            if not role:        # игроку сняли роль в игре — админка больше недоступна
                self.sessions.drop(tok)
                return self._json(h, {"error": "auth"}, 401)
            need = self._need_level(route, method, q)
            if ROLE_LEVEL.get(role, 0) < need:
                return self._json(h, {"error": "forbidden", "role": role}, 403)
            sess["role"] = h._role = role

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
                if sub in ("where", "journal", "history") and method == "GET":
                    return self._api_player_extra(h, pid, sub, q, sess)
                if sub == "view-as" and method == "POST":
                    return self._api_player_view_as(h, pid, sess)
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
    def _api_healthz(self, h):
        """Без авторизации, для внешнего аптайм-мониторинга: отдаёт только
        неконфиденциальные булевы флаги из уже готового снапшота (без live-collect,
        чтобы неавторизованный эндпоинт не давал повод гонять тяжёлую работу)."""
        snap = self.state.data.get("last_snapshot") or {}
        steam = snap.get("steam") or {}
        game = snap.get("game") or {}
        body = json.dumps({
            "ok": True,
            "supervisor_up": True,
            "steam_up": bool(steam.get("running")),
            "game_up": bool(game.get("running")),
            "snapshot_age": int(time.time() - snap["ts"]) if snap.get("ts") else None,
        }).encode("utf-8")
        return self._send(h, 200, "application/json", body, {"Cache-Control": "no-store"})

    # ---------------------------------------------------- название и иконка
    _FAVICON_TYPES = {b"\x89PNG": "image/png", b"\x00\x00\x01\x00": "image/x-icon",
                      b"\xff\xd8\xff": "image/jpeg", b"GIF8": "image/gif", b"RIFF": "image/webp"}

    def _title(self):
        return ((self.cfg.get("webui") or {}).get("title") or "").strip() or "SigmaSteamBot"

    def _favicon_path(self):
        return os.path.join(self.cfg.get("base_dir", common.BASE_DIR), "favicon.img")

    def _favicon_ver(self):
        try:
            return int(os.path.getmtime(self._favicon_path()))
        except OSError:
            return 0

    def _favicon_type(self, data):
        for sig, ct in self._FAVICON_TYPES.items():
            if data.startswith(sig):
                return ct
        return None

    def _send_favicon(self, h):
        try:
            with open(self._favicon_path(), "rb") as f:
                data = f.read()
        except OSError:
            return self._send(h, 204, "text/plain", b"")
        return self._send(h, 200, self._favicon_type(data) or "application/octet-stream", data,
                          {"Cache-Control": "max-age=300"})

    def _api_favicon(self, h, method, q, sess):
        """POST {data:"data:image/...;base64,..."} — поставить иконку панели
        (PNG/ICO/JPEG/GIF/WebP, до 512 КБ); {data:null} — убрать."""
        if method != "POST":
            return self._json(h, {"ok": True, "v": self._favicon_ver()})
        b = self._body(h)
        raw = b.get("data")
        path = self._favicon_path()
        if not raw:
            try:
                os.remove(path)
            except OSError:
                pass
            self.audit(_cip(h), sess["user"], "иконка панели: убрана")
            return self._json(h, {"ok": True, "v": 0})
        try:
            data = base64.b64decode(str(raw).split(",", 1)[-1], validate=False)
        except (ValueError, TypeError):
            return self._json(h, {"error": "invalid", "detail": "не base64"}, 400)
        if len(data) > 512 * 1024:
            return self._json(h, {"error": "invalid", "detail": "больше 512 КБ"}, 400)
        if not self._favicon_type(data):
            return self._json(h, {"error": "invalid", "detail": "нужен PNG/ICO/JPEG/GIF/WebP"}, 400)
        tmp = path + ".swtmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        self.audit(_cip(h), sess["user"], "иконка панели: загружена (%d байт)" % len(data))
        return self._json(h, {"ok": True, "v": self._favicon_ver()})

    def _api_session(self, h):
        _, sess = self._session_of(h)
        out = {"app": "SigmaSteamBot", "version": VERSION, "authed": bool(sess),
               "title": self._title(), "favicon_v": self._favicon_ver(),
               "langs": list(i18n.SUPPORTED)}
        if sess:
            out.update(username=sess["user"], csrf=sess["csrf"], must_change=self.auth.must_change_of(sess["user"]),
                       role=self._effective_role(sess["user"]) or self.auth.role_of(sess["user"]))
        return self._json(h, out)

    def _login_event(self, h, ev, user, **kw):
        h._nolog = True     # запрос уже описан этим событием
        rec = {"src": "admin", "ev": ev, "user": (user or "")[:64], "ip": _cip(h),
               "ua": (h.headers.get("User-Agent") or "")[:200], "via": getattr(h, "_via", None)}
        rec.update(kw)
        self.act.write(rec)

    def _api_login(self, h):
        ip = _cip(h)
        b = self._body(h)
        user = (b.get("username") or "").strip()[:64]
        pw = b.get("password") or ""
        acct = "adm:" + user.lower()
        ok, wait, what = self.guard.check(ip, acct)
        if not ok:
            self._login_event(h, "login_blocked", user, d="заблокировано %s ещё %d с" % (what, wait))
            return self._json(h, {"error": "throttled", "retry": wait}, 429)
        if user and pw and self.auth.verify(user, pw):
            self.guard.ok(ip, acct, user, "админка")
            tok, csrf = self.sessions.new(user, ip)
            self._login_event(h, "login_ok", user, sid=hashlib.sha256(tok.encode()).hexdigest()[:10],
                              role=self._effective_role(user) or self.auth.role_of(user))
            self.audit(ip, user, "вход в панель")
            return self._json(
                h, {"ok": True, "username": user, "csrf": csrf, "must_change": self.auth.must_change_of(user),
                    "role": self.auth.role_of(user)},
                set_cookie="sid=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (tok, SESSION_TTL),
            )
        self._login_event(h, "login_fail", user, d="нет такого логина" if self.auth.role_of(user) is None
                          else "неверный пароль")
        logging.warning("webui: неудачный вход user=%r ip=%s", user, ip)
        self.guard.fail(ip, acct, user, "админка")
        return self._json(h, {"error": "bad_credentials"}, 401)

    def _api_users(self, h, method, q, sess):
        """Пользователи панели (только админ). POST {op: add|role|reset|delete, name,
        role, password} — под повторным вводом своего пароля, всё в аудит."""
        if method == "GET":
            return self._json(h, {"ok": True, "users": self.auth.users(), "roles": list(ROLES), "me": sess["user"]})
        b = self._body(h)
        op, name = (b.get("op") or "").strip(), (b.get("name") or "").strip()
        if sess.get("role") != "gm" and (self.auth.role_of(name) == "gm" or (b.get("role") or "").strip() == "gm"):
            return self._json(h, {"error": "forbidden", "detail": "роль GM выдаёт и меняет только GM"}, 403)
        ok, resp = self._reauth(h, sess, "пользователи панели: %s %s" % (op, name), body=b)
        if not ok:
            return resp
        try:
            if op in ("add", "reset"):
                pw = b.get("new_password") or ""
                if len(pw) < 6 or pw.lower() in ("admin", "password", name.lower()):
                    raise _Bad("пароль: не короче 6 символов и не совпадает с логином")
                if op == "add":
                    self.auth.add_user(name, pw, (b.get("role") or "viewer").strip())
                else:
                    self.auth.reset_password(name, pw)
                    self.sessions.drop_user(name)
            elif op == "role":
                if name == sess["user"]:
                    raise _Bad("свою роль менять нельзя")
                self.auth.set_role(name, (b.get("role") or "").strip())
            elif op == "delete":
                if name == sess["user"]:
                    raise _Bad("себя удалить нельзя")
                self.auth.delete_user(name)
                self.sessions.drop_user(name)
            else:
                return self._json(h, {"error": "bad_op"}, 400)
        except _Bad as e:
            return self._json(h, {"error": "bad", "detail": str(e)}, 400)
        self.audit(_cip(h), sess["user"], "ПОЛЬЗОВАТЕЛИ панели: %s %s%s" % (
            op, name, (" → " + b.get("role")) if op in ("add", "role") else ""))
        return self._json(h, {"ok": True, "users": self.auth.users()})

    def _api_password(self, h, sess):
        b = self._body(h)
        old, new = b.get("old") or "", b.get("new") or ""
        if not self.auth.verify(sess["user"], old):
            return self._json(h, {"error": "bad_old"}, 403)
        if len(new) < 6:
            return self._json(h, {"error": "too_short"}, 400)
        if new.lower() in ("admin", "password", sess["user"].lower()):
            return self._json(h, {"error": "too_weak"}, 400)
        self.auth.set_password(sess["user"], new)
        self.audit(_cip(h), sess["user"], "смена пароля")
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
        mon_name = (self.cfg.get("monitor", {}) or {}).get("server_name", "AstralSigma")
        out["highlight"] = mon_name.lower().replace(" ", "")
        return self._json(h, out)

    # ---------------------------------------------------------------- players
    def _api_disk(self, h, method, q, sess):
        """Место на диске, где лежит мир игры (для шапки панели)."""
        path = players.find_world_dir(self.cfg) or self.cfg.get("base_dir", common.BASE_DIR)
        try:
            u = shutil.disk_usage(path)
        except OSError as e:
            return self._json(h, {"ok": False, "error": str(e)}, 500)
        drive = os.path.splitdrive(os.path.abspath(path))[0] or "/"
        return self._json(h, {"ok": True, "drive": drive, "total_gb": round(u.total / 1e9, 1),
                              "used_gb": round(u.used / 1e9, 1), "free_gb": round(u.free / 1e9, 1),
                              "used_pct": round(100.0 * u.used / u.total, 1) if u.total else None,
                              "free_pct": round(100.0 * u.free / u.total, 1) if u.total else None})

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
            for key in ("stash", "carry"):          # русские названия предметов рядом с внутренними
                for it in ((d.get("avatar") or {}).get(key) or []):
                    it["label"] = players.item_label(it.get("name")) or it.get("name")
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
        -> (ok, body_dict_or_error_response).

        Сессия, недавно подтверждённая паролем, освобождается от повторного
        ввода на ``REAUTH_ELEVATE_SECONDS`` (диалог на фронте сам решает,
        когда его показывать, по ответу ``password_required``/``bad_password``)."""
        b = body if body is not None else self._body(h)
        if self.sessions.is_elevated(sess.get("tok")):
            return True, b
        ip = _cip(h)
        pw = b.get("password") or ""
        if not pw:
            # 403, не 401 — 401 в api() трактуется фронтом как "сессия истекла" и разлогинивает
            return False, self._json(h, {"error": "password_required"}, 403)
        acct = "adm:" + sess["user"].lower()
        okt, wait, _ = self.guard.check(ip, acct)
        if not okt:
            return False, self._json(h, {"error": "throttled", "retry": wait}, 429)
        if not self.auth.verify(sess["user"], pw):
            self.audit(ip, sess["user"], "НЕВЕРНЫЙ пароль: %s" % what)
            self.guard.fail(ip, acct, sess["user"], "админка (подтверждение пароля)")
            return False, self._json(h, {"error": "bad_password"}, 403)
        self.guard.ok(ip, acct, sess["user"], "админка")
        self.sessions.elevate(sess.get("tok"), REAUTH_ELEVATE_SECONDS)
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
        self.audit(_cip(h), sess["user"], "ПОКАЗАН пароль игрока #%s" % pid)
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
        self.audit(_cip(h), sess["user"],
                   "ПОКАЗАНЫ приваты/IP игрока #%s (%d сообщ., %d IP)"
                   % (pid, len(d.get("private", [])), len(d.get("distinct_ips", []))))
        return self._json(h, d)

    # ---------------------------------------- инструменты из панели игрока (для админки)
    def _pw(self):
        """Панель игроков в этом же процессе (её кэши сундуков/рынка и снимки цен). Если
        она выключена — свой экземпляр без веб-сервера (данные считаются по запросу)."""
        pw = getattr(self, "pweb", None)
        if pw is None:
            from playerweb import PlayerWeb
            pw = PlayerWeb(self.cfg, self.state, web=self)
        if not pw._market["data"] and not pw._market["running"]:
            pw._market["running"] = True
            threading.Thread(target=pw._market_build, name="pw-market", daemon=True).start()
        return pw

    def _api_player_extra(self, h, pid, sub, q, sess):
        try:
            uid = int(pid)
        except ValueError:
            return self._json(h, {"error": "bad"}, 400)
        pw = self._pw()
        if sub == "where":
            d = pw._api_where(uid, q)
        elif sub == "history":
            d = pw._api_history(uid, q)
        else:
            d = dict(pw._api_journal(uid, q))
            if d.get("ok") and sess.get("role") == "gm":
                d["events"] = sorted(d["events"] + self._panel_events(uid), key=lambda e: -e["t"])[:2000]
                d["kinds"] = dict(collections.Counter(e["kind"] for e in d["events"]))
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _panel_events(self, uid, days=30):
        """Действия игрока в панели игроков и (если он стафф) в админке — из журнала активности."""
        since = time.time() - days * 86400
        rows = [r for r in self.act.query(src="player", user=str(uid), since=since, limit=1500)["rows"]
                if r.get("uid") == uid]
        linked = self.auth.user_for_game(uid)
        if linked:
            rows += [r for r in self.act.query(src="admin", user=linked, since=since, limit=1500)["rows"]
                     if r.get("user") == linked]
        out = []
        for r in rows:
            where = "Админка" if r.get("src") == "admin" else "Панель игрока"
            ev = r.get("ev")
            if ev == "ui":
                det = "[%s] %s: %s" % (r.get("tab", ""), r.get("a", ""), r.get("d", ""))
            elif ev in ("req", "page", "denied", "probe"):
                qs = "&".join("%s=%s" % kv for kv in (r.get("q") or {}).items())
                det = "%s %s%s → %s" % (r.get("m", ""), r.get("path", ""), ("?" + qs) if qs else "", r.get("st"))
            else:
                det = " · ".join(str(x) for x in (ev, r.get("d"), r.get("ip")) if x)
            if "[GM " in str(r.get("user") or ""):       # это GM смотрел «глазами игрока», а не сам игрок
                where += " · " + str(r["user"])[str(r["user"]).index("[GM "):]
            out.append({"t": int(r.get("t", 0)), "kind": "panel", "label": where, "detail": det})
        return out

    def _api_player_view_as(self, h, pid, sess):
        ok, b = self._reauth(h, sess, "глазами игрока #%s" % pid)
        if not ok:
            return b
        pwc = self.cfg.get("playerweb") or {}
        if not pwc.get("enabled") or getattr(self, "pweb", None) is None:
            return self._json(h, {"error": "bad", "detail": "панель игроков выключена"}, 400)
        try:
            uid = int(pid)
        except ValueError:
            return self._json(h, {"error": "bad"}, 400)
        wd = players.find_world_dir(self.cfg)
        nick = (players.load_user_list(wd) if wd else {}).get(uid)
        if not nick:
            return self._json(h, {"error": "bad", "detail": "нет такого игрока"}, 404)
        t = self.pweb.make_view_link(uid, nick, sess["user"])
        self.audit(_cip(h), sess["user"], "ГЛАЗАМИ ИГРОКА #%s (%s) — выдана ссылка" % (uid, nick))
        return self._json(h, {"ok": True, "port": int(pwc.get("port", 80)), "path": "/view-as?t=" + t})

    def _api_price_history(self, h, method, q, sess):
        return self._json(h, self._pw()._api_price_history(None, q))

    _METRIC_RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}

    def _api_metrics(self, h, method, q, sess):
        """Нагрузка сервера и панели: ?range=1h|6h|24h|7d|30d -> корзины [t, среднее, макс]."""
        rng = self._METRIC_RANGES.get((q.get("range") or ["6h"])[0], 6 * 3600)
        d = self.metrics.series(rng)
        d.update(ok=True, now=self.metrics.last or self.metrics.last_saved(), cores=self.metrics.cores, interval=self.metrics.interval,
                 range=rng, enabled=metrics.psutil is not None)
        return self._json(h, d)

    def _api_map_names(self, h, method, q, sess):
        """Справочник названий карт для всей админки: {id: {name, kind, star, star_name}}."""
        idx = os.path.join(self.cfg.get("base_dir", common.BASE_DIR), "space_index.json")
        return self._json(h, self._cached("map-names", lambda: {"ok": True, "maps": players.map_labels(self.cfg, idx)}, ttl=600))

    def _space_index_path(self):
        return os.path.join(self.cfg.get("base_dir", common.BASE_DIR), "space_index.json")

    def _api_space_galaxy(self, h, method, q, sess):
        return self._json(h, self._cached("space-galaxy", lambda: players.admin_space_galaxy(self.cfg, self._space_index_path()), ttl=120))

    def _api_space_system(self, h, method, q, sess):
        star = (q.get("star") or ["1"])[0]
        return self._json(h, self._cached("space-system-%s" % star,
                                          lambda: players.admin_space_system(self.cfg, self._space_index_path(), star), ttl=60))

    def _api_season_rating(self, h, method, q, sess):
        return self._json(h, self._cached("season-rating", lambda: players.season_rating(self.cfg), ttl=60))

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
            self.audit(_cip(h), sess["user"],
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
        self.audit(_cip(h), sess["user"],
                   "БЭКАП МИРА (%s, %d МБ) -> %s" % (scope, len(data) // 1048576, os.path.basename(path)))
        return self._send(h, 200, "application/zip", data,
                          {"Content-Disposition": 'attachment; filename="%s"' % os.path.basename(path),
                           "Cache-Control": "no-store"})

    def _api_space(self, h, method, q, sess):
        now = time.time()
        if not self._space_cache or now - self._space_cache[0] > 60:
            try:
                payload = players.space_report(self.cfg)
            except Exception as e:  # noqa: BLE001
                logging.exception("webui: space_report")
                payload = {"ok": False, "error": str(e)}
            self._space_cache = (now, payload)
        ts, payload = self._space_cache
        out = dict(payload)
        out["cached_age"] = int(now - ts)
        return self._json(h, out, 200 if out.get("ok") else 500)

    def _api_space_units(self, h, method, q, sess):
        """Позиции кораблей в космосе (снимок из space\\units.dt). ?star=N —
        только эта звёздная система (пусто/all = все разом)."""
        star = (q.get("star") or [""])[0]
        star = None if star in ("", "all", "*") else star
        try:
            d = players.space_units(self.cfg, star_id=star)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: space_units")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 500)

    def _api_space_clusters(self, h, method, q, sess):
        """Кластеры и звёздные системы галактики (Data\\world\\cluster<N>.json)."""
        try:
            d = players.galaxy_clusters(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: galaxy_clusters")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 500)

    def _api_space_map_image(self, h, method, q, sess):
        """PNG-диаграмма звёздной системы (планеты/спутники/астероиды/метеориты/
        поды/корабли). ?show=planet,ship,... — фильтр видов (пусто/нет = все)."""
        size = (q.get("size") or ["760"])[0]
        star = (q.get("star") or ["1"])[0]
        # отсутствие ?show= = все виды (обратная совместимость); ?show= (пусто) = ничего
        show = {s for s in (q.get("show") or [""])[0].split(",") if s} if "show" in q else None
        try:
            png, fn, meta = players.space_map_image(self.cfg, size=size, star_id=star, show=show)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: space_map_image")
            return self._json(h, {"ok": False, "error": str(e)}, 500)
        if not isinstance(png, (bytes, bytearray)):
            return self._json(h, png if isinstance(png, dict) else {"ok": False}, 500)
        return self._send(h, 200, "image/png", bytes(png), {
            "Cache-Control": "max-age=60",
            "Content-Disposition": 'inline; filename="%s"' % (fn or "space_map.png"),
        })

    def _api_space_map_data(self, h, method, q, sess):
        """Точки звёздной системы для наведения (id/координаты/детали)."""
        star = (q.get("star") or ["1"])[0]
        try:
            d = players.space_map_points(self.cfg, star_id=star)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: space_map_points")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 500)

    def _api_space_object_find(self, h, method, q, sess):
        """Поиск объекта (планеты/астероида) по имени в звёздной системе."""
        query = (q.get("q") or [""])[0]
        star = (q.get("star") or ["1"])[0]
        try:
            d = players.space_object_search(self.cfg, query, star_id=star)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: space_object_search")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 500)

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

    def _api_tech_track(self, h, method, q, sess):
        try:
            d = players.tech_track_read(self.cfg, self._tt_log,
                                        uid=(q.get("uid") or [None])[0],
                                        kind=(q.get("kind") or [None])[0],
                                        limit=(q.get("limit") or ["400"])[0])
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: tech_track_read")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 500)

    def _api_buff_notepad(self, h, method, q, sess):
        """Вкладка «Микстуры». GET — сохранённый buff_notepad с именами
        ингредиентов; POST {data:{...}} — сохранить новый (файл с машины
        игрока, панель сама его прочитать не может)."""
        if method == "POST":
            b = self._body(h)
            try:
                d = players.buff_notepad_save(self._buff_path, b.get("data"))
            except Exception as e:  # noqa: BLE001
                logging.exception("webui: buff_notepad_save")
                d = {"ok": False, "error": str(e)}
            if d.get("ok"):
                self.audit(_cip(h), sess["user"],
                           "buff_notepad: загружен (%s записей)" % d.get("count"))
            return self._json(h, d, 200 if d.get("ok") else 400)
        try:
            d = players.buff_notepad_read(self.cfg, self._buff_path)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: buff_notepad_read")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 500)

    def _api_buff_ingredients(self, h, method, q, sess):
        """Список из 24 ингредиентов микстур (Data\\product\\buff_balance.json) —
        для чекбоксов оптимизатора во вкладке «Микстуры»."""
        try:
            d = players.buff_balance_read(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: buff_balance_read")
            d = {"ok": False, "error": str(e)}
        out = {"ok": d.get("ok"), "ingredients": d.get("ingredients", []), "error": d.get("error")}
        return self._json(h, out, 200 if out.get("ok") else 404)

    def _api_buff_optimize(self, h, method, q, sess):
        """POST {available:[слаги], target:BuffType, top:5} — асинхронный перебор
        (может занять секунды при большом available) через тот же job-механизм,
        что и /api/action. Прогресс — GET /api/job?id=..."""
        b = self._body(h)
        jid = secrets.token_hex(8)
        job = {"id": jid, "done": False, "ok": None, "result": None,
               "started": time.time(), "finished": None}
        with self._jobs_lock:
            self._jobs[jid] = job
            while len(self._jobs) > 30:
                self._jobs.pop(next(iter(self._jobs)))

        def run():
            try:
                d = players.buff_optimize(self.cfg, b.get("available"), b.get("target"), b.get("top", 5))
            except Exception as e:  # noqa: BLE001
                logging.exception("webui: buff_optimize")
                d = {"ok": False, "error": str(e)}
            job.update(ok=bool(d.get("ok")), result=d, done=True, finished=time.time())

        threading.Thread(target=run, name="buffopt", daemon=True).start()
        return self._json(h, {"job": jid})

    def _api_tech_tree(self, h, method, q, sess):
        """Дерево технологий (tech.json) для схемы изучения игрока/клана."""
        try:
            d = players.tech_tree(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: tech_tree")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _api_clans(self, h, method, q, sess):
        """Вкладка «Кланы»: без ?id — список, с ?id=N — клан целиком."""
        cid = (q.get("id") or [""])[0]
        try:
            d = players.clan_detail(self.cfg, cid) if cid else players.clans_list(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: clans %s", cid)
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _api_craft_catalog(self, h, method, q, sess):
        """Всё, что можно скрафтить или получить станком (craft.json + machines.json)."""
        try:
            d = players.craft_catalog(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: craft_catalog")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _api_craft_plan(self, h, method, q, sess):
        """GET ?item=<slug|имя>&qty=N[&uid=N|&clan=N] — раскладка до сырья."""
        g = lambda k: (q.get(k) or [""])[0]
        try:
            d = players.craft_plan(self.cfg, g("item"), g("qty") or 1, g("uid") or None, g("clan") or None)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: craft_plan")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _api_clan_history(self, h, method, q, sess):
        """GET ?id=N[&days=60] — графики и события клана (ведёт сама панель)."""
        try:
            d = players.clan_history(self.cfg, (q.get("id") or [""])[0], self._ct_events, self._ct_points,
                                     (q.get("days") or ["60"])[0])
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: clan_history")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _heavy_job(self, h, q, name, fn, ttl=120):
        """Тяжёлый отчёт (проход по всем картам — десятки секунд): свежий кэш —
        сразу {ready, data}; иначе один общий фоновый job — {job}, клиент
        опрашивает /api/job. ?force=1 — пересчитать."""
        now = time.time()
        slot = self._heavy.setdefault(name, {"cache": None, "job": None})
        force = (q.get("force") or [""])[0] == "1"
        if slot.get("cache") and not force and now - slot["cache"][0] < ttl:
            return self._json(h, {"ready": True, "data": slot["cache"][1]})
        with self._jobs_lock:
            job = slot.get("job")
            if job and not job["done"] and job["id"] in self._jobs:
                return self._json(h, {"job": job["id"]})
            jid = secrets.token_hex(8)
            job = {"id": jid, "done": False, "ok": None, "result": None, "started": now, "finished": None}
            self._jobs[jid] = job
            slot["job"] = job
            while len(self._jobs) > 30:
                self._jobs.pop(next(iter(self._jobs)))

        def run():
            try:
                d = fn()
            except Exception as e:  # noqa: BLE001
                logging.exception("webui: %s", name)
                d = {"ok": False, "error": str(e)}
            if d.get("ok"):
                slot["cache"] = (time.time(), d)
            job.update(ok=bool(d.get("ok")), result=d, done=True, finished=time.time())

        threading.Thread(target=run, name=name, daemon=True).start()
        return self._json(h, {"job": jid})

    def _api_trade(self, h, method, q, sess):
        """Сводка торговли (терминалы + магазины на картах)."""
        return self._heavy_job(h, q, "trade", lambda: players.trade_report(self.cfg))

    def _api_economy(self, h, method, q, sess):
        """Экономика: сколько чего в мире + динамика по суточным снимкам."""
        return self._heavy_job(h, q, "economy", lambda: players.economy_report(self.cfg, self._econ_hist), ttl=300)

    def _api_economy_item(self, h, method, q, sess):
        """GET ?id=N — история общего количества предмета по суточным снимкам."""
        return self._json(h, players.economy_item_history(self._econ_hist, (q.get("id") or [""])[0]))

    def _api_suspicious(self, h, method, q, sess):
        """Журнал подозрений — под админ-паролем (связывает аккаунты)."""
        b = self._body(h)
        ok, resp = self._reauth(h, sess, "мониторинг нарушений", body=b)
        if not ok:
            return resp
        tr = (self._heavy.get("trade") or {}).get("cache")
        try:
            d = players.suspicious_read(self.cfg, self._susp_log, tr[1] if tr else None)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: suspicious_read")
            return self._json(h, {"error": "internal", "detail": str(e)}, 500)
        self.audit(_cip(h), sess["user"], "НАРУШЕНИЯ: просмотр журнала (%d)" % d.get("total", 0))
        return self._json(h, d)

    def _cached(self, name, fn, ttl=60):
        now = time.time()
        slot = self._heavy.setdefault(name, {"cache": None, "job": None})
        if slot.get("cache") and now - slot["cache"][0] < ttl:
            return slot["cache"][1]
        d = fn()
        if d.get("ok"):
            slot["cache"] = (now, d)
        return d

    def _api_space_gen(self, h, method, q, sess):
        """Анализ генерации космоса: что создано/изменено после генерации мира (звёзды,
        кластеры, стартовая карта) + история по бэкапам. ?force=1 — без кэша."""
        if (q.get("force") or [""])[0]:
            self._heavy.pop("space-gen", None)
        return self._json(h, self._cached("space-gen", lambda: players.space_generation(self.cfg), ttl=300))

    def _api_activity(self, h, method, q, sess):
        """Активность и удержание (analytics.txt)."""
        try:
            d = self._cached("activity", lambda: players.activity_report(self.cfg))
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: activity_report")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _api_leaderboards(self, h, method, q, sess):
        """Рейтинги: богачи, торговцы, исследователи, рост кланов."""
        try:
            d = self._cached("leaderboards", lambda: players.leaderboards(self.cfg, self._tt_log, self._ct_points))
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: leaderboards")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _api_fleet(self, h, method, q, sess):
        """Корабли игроков в космосе + станции."""
        try:
            d = self._cached("fleet", lambda: players.space_fleet(self.cfg))
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: space_fleet")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _api_admin_tools(self, h, method, q, sess):
        """Инструменты админа — под паролем панели, всё в аудит.
        op: mass_give {targets,item,count} | clan_tech {clan,techs} |
        backups {uid} | restore {uid,dir,file}. Правки — только оффлайн-игрокам."""
        b = self._body(h)
        op = (b.get("op") or "").strip()
        ok, resp = self._reauth(h, sess, "инструменты админа: %s" % op, body=b)
        if not ok:
            return resp
        ip, user = _cip(h), sess["user"]
        try:
            if op == "mass_give":
                d = players.mass_give(self.cfg, b.get("targets"), b.get("item"), b.get("count"))
                if d.get("ok"):
                    self.audit(ip, user, "АДМИН: массовая выдача %s ×%s → [%s]: выдано %d/%d" % (
                        b.get("item"), b.get("count"), b.get("targets"), d["done"], d["total"]))
            elif op == "clan_tech":
                d = players.clan_give_tech(self.cfg, b.get("clan"), b.get("techs"))
                if d.get("ok"):
                    self.audit(ip, user, "АДМИН: техи %s клану #%s: %d/%d игроков" % (
                        b.get("techs"), b.get("clan"), d["done"], d["total"]))
            elif op == "backups":
                d = players.player_backups(self.cfg, b.get("uid"))
            elif op == "restore":
                d = players.restore_inventory(self.cfg, b.get("uid"), b.get("dir"), b.get("file"))
                self.audit(ip, user, "АДМИН: откат инвентаря игрока #%s из %s/%s → %s" % (
                    b.get("uid"), b.get("dir"), b.get("file"), "ok" if d.get("ok") else d.get("error")))
            else:
                d = {"ok": False, "error": "неизвестная операция"}
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: admin_tools %s", op)
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 400)

    def _api_map_clans(self, h, method, q, sess):
        """GET ?map=N — легенда «карта по кланам»."""
        try:
            d = players.map_clans(self.cfg, (q.get("map") or ["1"])[0])
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: map_clans")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _api_food_ingredients(self, h, method, q, sess):
        """53 ингредиента кулинарии (Data\\product\\product_genes.json) с генами —
        для чекбоксов оптимизатора во вкладке «Кулинария»."""
        try:
            d = players.food_data_read(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: food_data_read")
            d = {"ok": False, "error": str(e)}
        out = {"ok": d.get("ok"), "ingredients": d.get("ingredients", []), "error": d.get("error")}
        return self._json(h, out, 200 if out.get("ok") else 404)

    def _api_food_lib(self, h, method, q, sess):
        """Все блюда, когда-либо приготовленные на сервере (product_lib.json)."""
        try:
            d = players.food_lib_read(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: food_lib_read")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 404)

    def _api_food_optimize(self, h, method, q, sess):
        """POST {available:[id], genes:["A".."D"], max_eat:float|null, top:10} —
        асинхронный перебор (полный набор ~2-5 с), прогресс — GET /api/job?id=..."""
        b = self._body(h)
        jid = secrets.token_hex(8)
        job = {"id": jid, "done": False, "ok": None, "result": None,
               "started": time.time(), "finished": None}
        with self._jobs_lock:
            self._jobs[jid] = job
            while len(self._jobs) > 30:
                self._jobs.pop(next(iter(self._jobs)))

        def run():
            try:
                d = players.food_optimize(self.cfg, b.get("available"), b.get("genes"),
                                          b.get("max_eat"), b.get("top", 10))
            except Exception as e:  # noqa: BLE001
                logging.exception("webui: food_optimize")
                d = {"ok": False, "error": str(e)}
            job.update(ok=bool(d.get("ok")), result=d, done=True, finished=time.time())

        threading.Thread(target=run, name="foodopt", daemon=True).start()
        return self._json(h, {"job": jid})

    def _api_mapdt_index(self, h, method, q, sess):
        try:
            d = players.mapdt_index(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: mapdt_index")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 500)

    def _api_mapdt(self, h, method, q, sess):
        try:
            d = players.mapdt_summary(self.cfg, (q.get("map") or ["1"])[0])
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: mapdt")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 500)

    def _api_mapdt_find(self, h, method, q, sess):
        """Поиск предмета по карте/всему миру: ?map=N|all&item=<id|имя|подстрока>."""
        mp = (q.get("map") or ["all"])[0]
        item = (q.get("item") or [""])[0]
        try:
            d = players.mapdt_find(self.cfg, mp, item)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: mapdt_find")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 500)

    def _api_mapdt_image(self, h, method, q, sess):
        """PNG-картинка карты: ?map=N&scale=auto&claims=1&owner=<id>&force=1."""
        mp = (q.get("map") or ["1"])[0]
        scale = (q.get("scale") or ["auto"])[0]
        claims = (q.get("claims") or ["1"])[0] not in ("0", "false", "no")
        owner = (q.get("owner") or [""])[0]
        force = (q.get("force") or ["0"])[0] not in ("0", "", "false", "no")
        by_clan = (q.get("clans") or ["0"])[0] == "1"
        shops = (q.get("shops") or ["0"])[0] == "1"
        try:
            png, fn, meta = players.mapdt_image(self.cfg, mp, scale=scale, claims=claims, owner=owner,
                                                force=force, by_clan=by_clan, shops=shops)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: mapdt_image")
            return self._json(h, {"ok": False, "error": str(e)}, 500)
        if not isinstance(png, (bytes, bytearray)):
            return self._json(h, png if isinstance(png, dict) else {"ok": False}, 500)
        return self._send(h, 200, "image/png", bytes(png), {
            "Cache-Control": "max-age=60",
            "Content-Disposition": 'inline; filename="%s"' % (fn or "map.png"),
        })

    def _api_mapdt_owners(self, h, method, q, sess):
        """Сетка владения землёй карты для hover-подсказки: ?map=N."""
        try:
            d = players.mapdt_owners(self.cfg, (q.get("map") or ["1"])[0])
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: mapdt_owners")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 500)

    def _api_mapdt_containers(self, h, method, q, sess):
        """Непустые контейнеры карты: ?map=N&min=1 -> [{x,y,slot,items,total}]."""
        try:
            mn = max(1, int((q.get("min") or ["1"])[0]))
        except ValueError:
            mn = 1
        try:
            d = players.mapdt_containers(self.cfg, (q.get("map") or ["1"])[0], min_items=mn)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: mapdt_containers")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 500)

    def _api_player_item_find(self, h, method, q, sess):
        """Кто из игроков держит предмет: ?item=<id|имя|подстрока>."""
        item = (q.get("item") or [""])[0]
        try:
            d = players.player_item_search(self.cfg, item)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: player_item_find")
            d = {"ok": False, "error": str(e)}
        return self._json(h, d, 200 if d.get("ok") else 500)

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
        self.audit(_cip(h), sess["user"],
                   "ТВИНК-ДЕТЕКТ: пароль %d групп / IP %d / >=%d акк."
                   % (d.get("code_flagged", 0), d.get("flagged_ips", 0), mn))
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
            self.audit(_cip(h), sess["user"],
                       "ИНВЕНТАРЬ игрока #%s: %s %s×%s %s (бэкап %s)"
                       % (pid, op, d.get("name") or item,
                          d.get("count") if op == "give" else d.get("removed"),
                          d.get("where"), d.get("backup")))
        else:
            self.audit(_cip(h), sess["user"],
                       "ИНВЕНТАРЬ игрока #%s: %s отклонено — %s" % (pid, op, d.get("error")))
        return self._json(h, d, 200 if d.get("ok") else 400)

    def _api_player_moderate(self, h, pid, sess):
        """Бан/роль/телепорт/техи/статы/сброс пароля — оффлайн, под админ-паролем."""
        body = self._body(h)
        act = (body.get("action") or "").strip()
        ACTS = {"ban", "unban", "role", "position", "tech", "stat", "reset_code"}
        if act not in ACTS:
            return self._json(h, {"error": "bad_action"}, 400)
        if ROLE_LEVEL.get(sess.get("role"), 0) < 3 and act not in self._MOD_MODERATE:
            return self._json(h, {"error": "forbidden", "role": sess.get("role")}, 403)
        # Мастер в игре = GM панели: выдать/снять Мастера или сбросить ему пароль — только GM
        if sess.get("role") != "gm" and act in ("role", "reset_code"):
            try:
                to_master = act == "role" and int(body.get("role") or 0) == 3
                pid_i = int(pid)
            except (TypeError, ValueError):
                return self._json(h, {"error": "bad"}, 400)
            if to_master or self.game_panel_role(pid_i) == "gm":
                return self._json(h, {"error": "forbidden", "detail": "роль Мастера — только GM"}, 403)
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
            self.audit(_cip(h), sess["user"],
                       "МОДЕРАЦИЯ игрока #%s: %s (бэкап %s)" % (pid, summ, d.get("backup")))
        else:
            self.audit(_cip(h), sess["user"],
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

    # ------------------------------------------------------- журнал активности (GM)
    _ACT_GROUPS = {
        "logins": ("login_ok", "login_fail", "login_blocked", "logout", "admin_enter"),
        "bad": ("login_fail", "login_blocked", "denied", "probe", "block", "flood"),
        "req": ("req", "page"), "ui": ("ui",), "audit": ("audit",), "guard": ("block", "flood"),
    }

    def _api_activity_log(self, h, method, q, sess):
        """GET ?src=admin|player&ev=<группа или события через запятую>&user=&ip=&text=
        &hours=&before=&limit= — новые сверху, before — догрузка более старых."""
        g = lambda k, d="": (q.get(k) or [d])[0]
        ev = g("ev")
        evs = self._ACT_GROUPS.get(ev) or [x for x in ev.split(",") if x]
        try:
            hours = float(g("hours", "0") or 0)
            before = float(g("before", "0") or 0)
            limit = max(20, min(2000, int(g("limit", "300"))))
        except ValueError:
            return self._json(h, {"error": "bad"}, 400)
        d = self.act.query(src=g("src") or None, evs=evs, user=g("user"), ip=g("ip"), text=g("text"),
                           since=time.time() - hours * 3600 if hours > 0 else None,
                           before=before or None, limit=limit)
        d.update(ok=True, size=self.act.size(), files=len(self.act.files()))
        return self._json(h, d)

    def _api_track(self, h, method, q, sess):
        """Действия в интерфейсе, которые присылает браузер: вкладки, клики, ввод в
        поля поиска/фильтров. Со стороны клиента — можно подделать, но не скрыть
        запросы к API: они пишутся сервером отдельно."""
        h._nolog = True
        b = self._body(h)
        cnt = getattr(self, "_trk_cnt", {})
        now = time.time()
        sid = getattr(h, "_sid", "")
        c = cnt.get(sid)
        if not c or now - c[0] > 3600:
            c = cnt[sid] = [now, 0]
        self._trk_cnt = cnt
        for e in (b.get("ev") or [])[:60]:
            if not isinstance(e, dict) or c[1] >= 5000:
                break
            c[1] += 1
            self.act.write({"src": "admin", "ev": "ui", "user": sess["user"], "role": sess.get("role"),
                            "ip": _cip(h), "sid": sid, "via": getattr(h, "_via", None),
                            "a": str(e.get("a") or "")[:16], "tab": str(e.get("tab") or "")[:32],
                            "d": str(e.get("d") or "")[:300]})
        return self._json(h, {"ok": True})

    def _api_guard(self, h, method, q, sess):
        """Блокировки входа (обе панели). POST {op:"unblock", key}."""
        if method == "POST":
            b = self._body(h)
            key = str(b.get("key") or "")
            if b.get("op") != "unblock" or not key:
                return self._json(h, {"error": "bad_op"}, 400)
            done = self.guard.unblock(key)
            self.audit(_cip(h), sess["user"], "РАЗБЛОКИРОВКА входа: %s" % key)
            return self._json(h, {"ok": done})
        d = self.guard.status()
        d["ok"] = True
        return self._json(h, d)

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

    # -------------------------------------------------------------- login_flow
    def _api_login_flow(self, h, method, q, sess):
        """Тюнер координат входа (вкладка «Вход»): отдельный маленький JSON-редактор
        login_flow, не завязанный на общую форму /api/settings."""
        if method == "GET":
            return self._json(h, {
                "ok": True,
                "login_flow": self.cfg.get("login_flow", {}) or {},
                "game_window_size": self.cfg.get("game_window_size", [1024, 768]),
                "active_account": self.cfg.get("active_account") or "",
                "game_accounts": [a.get("label") for a in (self.cfg.get("game_accounts") or [])],
            })
        b = self._body(h)
        lf = b.get("login_flow")
        if not isinstance(lf, dict):
            return self._json(h, {"error": "invalid", "detail": "login_flow: ожидается объект"}, 400)
        self.cfg["login_flow"] = lf
        try:
            common.save_config(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: login_flow save_config")
            return self._json(h, {"error": "save_failed", "detail": str(e)}, 500)
        self.audit(_cip(h), sess["user"], "login_flow: сохранён из тюнера")
        return self._json(h, {"ok": True})

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
        if sess.get("role") != "gm" and _eff_super(tg) != _eff_super({"allowed_user_ids": admins, "super_admin_id": sa}):
            return self._json(h, {"error": "forbidden", "detail": "главного админа Telegram меняет только GM"}, 403)
        tg.update(allowed_user_ids=admins, moderator_user_ids=mods,
                  super_admin_id=sa, default_lang=lang, alerts_enabled=alerts)
        try:
            common.save_config(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: save_config")
            return self._json(h, {"error": "save_failed", "detail": str(e)}, 500)
        self.bot.apply_roles(tg)
        self.audit(_cip(h), sess["user"],
                   "роли: админы=%s модераторы=%s super=%s lang=%s alerts=%s"
                   % (admins, mods, sa, lang, alerts))
        return self._json(h, {"ok": True, "allowed_user_ids": admins, "moderator_user_ids": mods,
                              "super_admin_id": sa, "default_lang": lang, "alerts_enabled": alerts})

    def _api_settings(self, h, method, q, sess):
        """Полный редактор настроек бота (config.json) + игровые аккаунты."""
        if method == "GET":
            sections = []
            for key, tr, te, fields in SETTINGS_SCHEMA:
                out_fields = []
                for (p, lr, le, typ, hint) in fields:
                    cur = _cfg_get_path(self.cfg, p)
                    fd = {"path": p, "label_ru": lr, "label_en": le, "type": typ, "hint": hint}
                    if typ == "secret":
                        fd["value"] = ""
                        fd["has_secret"] = bool(cur)
                    elif typ == "intlist":
                        fd["value"] = ", ".join(str(x) for x in (cur or []))
                    elif typ == "strlist":
                        fd["value"] = ", ".join(cur or [])
                    elif typ == "intn":
                        fd["value"] = "" if cur is None else str(cur)
                    elif typ == "bool":
                        fd["value"] = bool(cur)
                    else:
                        fd["value"] = "" if cur is None else cur
                    out_fields.append(fd)
                sections.append({"key": key, "title_ru": tr, "title_en": te, "fields": out_fields})
            accs = []
            for a in (self.cfg.get("game_accounts") or []):
                accs.append({"label": a.get("label") or a.get("user") or "?",
                             "user": a.get("user") or "", "has_password": bool(a.get("password"))})
            lf = self.cfg.get("login_flow", {}) or {}
            return self._json(h, {
                "ok": True, "sections": sections,
                "game_accounts": accs, "active_account": self.cfg.get("active_account") or "",
                "login_flow_json": json.dumps(lf, ensure_ascii=False, indent=2),
                "restart_hint_ru": "watchdog, монитор сервера, Discord-стата, роли/язык/алерты применяются "
                                   "сразу; остальное (веб-панель, players.*, serverlist.*, поля Telegram-"
                                   "подключения) — после перезапуска задачи (кнопка «Перезапустить "
                                   "задачу» на вкладке «Действия»).",
            })

        b = self._body(h)
        cfg = copy.deepcopy(self.cfg)
        try:
            vals = b.get("values") or {}
            if not isinstance(vals, dict):
                raise _Bad("values: ожидается объект")
            for p, raw in vals.items():
                fd = _SETTINGS_FIELDS.get(p)
                if not fd:
                    continue
                typ, _ = fd
                if typ == "secret":
                    if str(raw).strip() == "":
                        continue          # пусто = не менять
                    _cfg_set_path(cfg, p, str(raw))
                else:
                    _cfg_set_path(cfg, p, _coerce_setting(p, typ, raw))

            if "login_flow" in b and b["login_flow"] not in (None, ""):
                lf = b["login_flow"]
                lf = json.loads(lf) if isinstance(lf, str) else lf
                if not isinstance(lf, dict):
                    raise _Bad("login_flow: ожидается JSON-объект")
                cfg["login_flow"] = lf

            # игровые аккаунты
            if "game_accounts" in b:
                old = {a.get("label"): a for a in (self.cfg.get("game_accounts") or [])}
                new = []
                for a in (b.get("game_accounts") or []):
                    lbl = (a.get("label") or a.get("user") or "").strip()
                    if not lbl:
                        continue
                    pw = a.get("password")
                    if pw in (None, "", "••••••"):
                        pw = (old.get(lbl) or {}).get("password", "")
                    new.append({"label": lbl, "user": (a.get("user") or "").strip(), "password": pw})
                cfg["game_accounts"] = new
                aa = (b.get("active_account") or "").strip()
                cfg["active_account"] = aa if any(x["label"] == aa for x in new) else (new[0]["label"] if new else "")

            tg = cfg.setdefault("telegram", {})
            admins = tg.get("allowed_user_ids") or []
            if not admins:
                raise _Bad("нужен хотя бы один администратор Telegram")
            if sess.get("role") != "gm" and _eff_super(tg) != _eff_super(self.cfg.get("telegram") or {}):
                raise _Bad("главного админа Telegram меняет только GM")
            sa = tg.get("super_admin_id")
            if sa is not None and sa not in admins:
                raise _Bad("главный админ должен быть среди администраторов")
            lang = (tg.get("default_lang") or "ru").lower()
            if lang not in i18n.SUPPORTED:
                tg["default_lang"] = "ru"
            port = _cfg_get_path(cfg, "webui.port")
            if port is not None and not (1 <= int(port) <= 65535):
                raise _Bad("порт вне 1..65535")
            try:
                nets = _parse_nets(_cfg_get_path(cfg, "webui.allowed_nets") or [])
            except ValueError as e:
                raise _Bad("разрешённые адреса: %s" % e)
            if not _ip_allowed(_cip(h), nets):
                raise _Bad("разрешённые адреса: ваш текущий адрес %s в список не входит — "
                           "сохранение заблокировало бы вам доступ" % _cip(h))
        except _Bad as e:
            return self._json(h, {"error": "invalid", "detail": str(e)}, 400)
        except ValueError as e:
            return self._json(h, {"error": "invalid", "detail": "JSON/число: %s" % e}, 400)

        self.cfg.clear()
        self.cfg.update(cfg)
        try:
            common.save_config(self.cfg)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: settings save_config")
            return self._json(h, {"error": "save_failed", "detail": str(e)}, 500)
        try:
            self.bot.apply_roles(self.cfg.get("telegram", {}))
        except Exception:  # noqa: BLE001
            logging.exception("webui: apply_roles after settings")
        try:
            self.bot.apply_monitor(self.cfg.get("monitor", {}))
        except Exception:  # noqa: BLE001
            logging.exception("webui: apply_monitor after settings")
        try:
            self.bot.apply_discord(self.cfg.get("discord", {}))
        except Exception:  # noqa: BLE001
            logging.exception("webui: apply_discord after settings")
        try:
            if self.wd:
                self.wd.apply()
        except Exception:  # noqa: BLE001
            logging.exception("webui: watchdog apply after settings")
        self.audit(_cip(h), sess["user"],
                   "настройки: изменено %d полей%s%s" % (
                       len(vals), " +login_flow" if b.get("login_flow") else "",
                       " +аккаунты(%d)" % len(cfg.get("game_accounts") or []) if "game_accounts" in b else ""))
        return self._json(h, {"ok": True, "restart_recommended": True})

    # ----------------------------------------------------------------- actions
    _OPS = {"startgame", "stopgame", "restartgame", "restartsteam", "login",
            "watchdog", "restartvm", "stopbot", "restarttask", "testalert",
            "navshot", "navstate", "navclickp"}
    _CONFIRM = {"restartvm", "stopbot", "restarttask"}

    def _api_action(self, h, method, q, sess):
        b = self._body(h)
        op = (b.get("op") or "").strip()
        if op not in self._OPS:
            return self._json(h, {"error": "bad_op"}, 400)
        if ROLE_LEVEL.get(sess.get("role"), 0) < 3 and op not in self._MOD_OPS:
            return self._json(h, {"error": "forbidden", "role": sess.get("role")}, 403)
        if op in self._CONFIRM and not b.get("confirm"):
            return self._json(h, {"error": "need_confirm"}, 400)
        lang = i18n.norm(b.get("lang") or self.cfg.get("telegram", {}).get("default_lang", "ru"))
        if op in gamectl.LOCKED_OPS:
            ok, info = common.try_action_lock(self.state, sess["user"], op)
            if not ok:
                msg = i18n.t(lang, "action.locked", actor=info["actor"],
                             op=i18n.op_label(lang, info["op"]), left=info["left"])
                return self._json(h, {"error": "locked", "detail": msg}, 409)
        jid = secrets.token_hex(8)
        job = {"id": jid, "op": op, "done": False, "ok": None, "text": "",
               "started": time.time(), "finished": None}
        with self._jobs_lock:
            self._jobs[jid] = job
            while len(self._jobs) > 30:
                self._jobs.pop(next(iter(self._jobs)))
        ip, user = _cip(h), sess["user"]
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
            ok, msg = gamectl.restart_vm(self.cfg)
            return ok, ("VM перезагружается" if ok else "не удалось: %s" % msg)
        if op == "stopbot":
            task = self.cfg.get("task_name", "SigmaSteamBot")
            ok, msg = gamectl.disable_bot_task(task)
            txt = ("задача %s отключена — супервизор останавливается; поднять обратно только с VM"
                   % task) if ok else ("процесс останавливаю, но задачу %s не отключить: %s" % (task, msg))
            threading.Timer(1.5, self._shutdown_supervisor).start()
            return ok, txt
        if op == "restarttask":
            ok = self._detached_task_restart(self.cfg.get("task_name", "SigmaSteamBot"))
            return ok, ("задача перезапустится через ~15 с — панель оборвётся на это время, обновите страницу"
                        if ok else "не удалось создать задачу перезапуска — см. logs/supervisor.log")
        if op == "testalert":
            self.bot.push_alert("🔔 Тест-алерт из веб-панели • %s" % _now_iso())
            return True, "тест-алерт поставлен в очередь отправки администраторам"
        if op in ("navshot", "navstate", "navclickp"):
            return self._do_nav_op(op, b)
        return False, "неизвестная операция"

    def _do_nav_op(self, op, b):
        """Тюнер входа: гоняет nav.py через задачу SigmaNav (runner.run_nav) —
        та же интерактивная сессия, что и авто-вход watchdog'а."""
        try:
            import runner
        except Exception as e:  # noqa: BLE001
            return False, "модуль runner недоступен: %s" % e
        if op == "navshot":
            args = "shot tuner"
        elif op == "navstate":
            args = "state"
        else:  # navclickp
            try:
                xp, yp = float(b.get("xp")), float(b.get("yp"))
            except (TypeError, ValueError):
                return False, "нужны числовые xp/yp"
            button = "right" if b.get("button") == "right" else "left"
            args = "clickp %.3f %.3f %s tuner%s" % (xp, yp, button, " --dbl" if b.get("dbl") else "")
        try:
            _ok, out = runner.run_nav(args, timeout=60)
        except Exception as e:  # noqa: BLE001
            logging.exception("webui: nav op %s", op)
            return False, "ошибка: %s" % e
        failed = any(m in out for m in ("Traceback", "RuntimeError", "не найдено"))
        tail = "\n".join(ln for ln in out.strip().splitlines()[-6:])
        return (not failed), tail or "(нет вывода)"

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
        """Перезапустить задачу планировщика, не полагаясь на то, что наш же
        процесс переживёт собственный ``schtasks /End``.

        Раньше End→Run гнались отдельным detached cmd.exe, запущенным ИЗ этого
        же процесса — но Task Scheduler держит все процессы задачи в одном
        Job Object, и когда ``/End`` завершает задачу, вместе с ней убивается
        и подряженный (detached) потомок, несмотря на DETACHED_PROCESS/
        CREATE_NEW_PROCESS_GROUP (это флаги консоли/группы процессов, а не
        job'ы). End→Run молча не доезжал до конца именно поэтому. Решение —
        не подряжать потомка самим, а зарегистрировать ОДНОРАЗОВУЮ задачу в
        самом планировщике (SC ONCE, через ~15 с): её запускает служба
        планировщика заново, отдельно от нашего job'а, так что наша смерть на
        End её не касается. Самоудаляется последней командой в своей же
        цепочке — флаг ``/Z`` тут не работает, валит создание задачи ошибкой
        в XML EndBoundary. 15 с, а не 5 — на глаз (проверено на .108):
        schtasks сам предупреждает и не гарантирует запуск, если /ST ближе
        ~10 с к текущему моменту (round-trip создания задачи + погрешность
        планировщика).

        Между End и Run — пауза ``ping -n 4 127.0.0.1 >nul`` (~3 с; НЕ
        ``timeout /t`` — тот падает без консоли: «Input redirection is not
        supported», а тут задача запускается службой планировщика без
        интерактивной сессии). Без паузы 2026-09-17 дважды поймали гонку:
        Run стартовал новый процесс раньше, чем старый реально исчезал из
        таблицы процессов — новый видел «живой» PID в supervisor.lock,
        считал себя дублем и тихо выходил (exit 0), оставляя панель
        недоступной до ручного ``schtasks /Run``."""
        helper = task + "RestartHelper"
        when = (datetime.now() + timedelta(seconds=15)).strftime("%H:%M:%S")
        tr = ('cmd /c "schtasks /End /TN {t} & ping -n 4 127.0.0.1 >nul '
              '& schtasks /Change /TN {t} /ENABLE & schtasks /Run /TN {t} '
              '& schtasks /Delete /F /TN {h}"').format(t=task, h=helper)
        r = subprocess.run(
            ["schtasks", "/Create", "/F", "/SC", "ONCE", "/ST", when,
             "/TN", helper, "/TR", tr],
            capture_output=True, timeout=15,
        )
        ok = r.returncode == 0
        if ok:
            logging.info("webui: задача %s перезапустится в %s через одноразовую %s", task, when, helper)
        else:
            logging.error("webui: не удалось создать %s: %s", helper,
                          r.stderr.decode("cp866", "replace").strip() or r.stdout.decode("cp866", "replace").strip())
        return ok


# --------------------------------------------------------------------------- SPA
PAGE = r"""<!doctype html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SigmaSteamBot</title>
<link rel="icon" id="favicon" href="/favicon.ico">
<style>
:root{
  --bg:#0f1216; --panel:#171c22; --panel2:#1e252d; --line:#2b333d; --fg:#e7ecf1;
  --mut:#93a1b0; --acc:#4c8dff; --ok:#3fb950; --warn:#d29922; --err:#f85149;
  --radius:10px;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;   /* серии графиков (проверено на CVD, тёмная тема) */
}
:root[data-theme="light"]{
  --bg:#f4f6f8; --panel:#ffffff; --panel2:#eef1f4; --line:#d7dde3; --fg:#1b2229;
  --mut:#5b6670; --acc:#1f6feb; --ok:#1a7f37; --warn:#9a6700; --err:#cf222e;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
}
*{box-sizing:border-box}
.mc{position:relative;padding:12px 14px}
.mc-h{margin-bottom:4px} .mc-leg{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;margin-bottom:4px}
.mc-leg span{display:inline-flex;align-items:center;gap:6px} .mc-leg i{display:inline-block;width:14px;height:2px;border-radius:1px}
.mc-tip{position:absolute;pointer-events:none;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:6px 9px;font-size:12px;
  white-space:nowrap;box-shadow:0 4px 14px rgba(0,0,0,.25);display:none;z-index:3}
.mc-tip i{display:inline-block;width:10px;height:2px;margin-right:6px;vertical-align:middle}
.ld-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}
.ld-tile{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:10px 12px}
.ld-tile .v{font-size:22px;font-weight:600;margin-top:2px} .ld-tile .s{font-size:12px;color:var(--mut)}
.ld-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:12px}
@media (max-width:560px){ .ld-grid{grid-template-columns:1fr} }
html{scrollbar-color:var(--line) var(--panel);scrollbar-width:thin}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-track{background:var(--panel)}
::-webkit-scrollbar-corner{background:var(--panel)}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:6px;border:2px solid var(--panel);background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:var(--mut);background-clip:padding-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
a{color:var(--acc)}
header{display:flex;align-items:center;gap:12px;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5;flex-wrap:wrap}
header h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px;display:flex;align-items:center;gap:8px}
#hlogo{width:26px;height:26px;border-radius:6px;object-fit:contain}
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
.modal{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);width:320px;max-width:100%;padding:16px;margin-top:60px}
.modal h3{margin:0 0 10px}
.chips{display:flex;flex-wrap:wrap;gap:4px}
.chip{font-size:11.5px;padding:1px 7px;border:1px solid var(--line);border-radius:20px;color:var(--mut)}
.bar{position:relative;height:14px;background:var(--panel2);border:1px solid var(--line);border-radius:7px;overflow:hidden;min-width:90px}
.bar>span{position:absolute;inset:0 auto 0 0;background:var(--acc);opacity:.55}
.bar>b{position:absolute;inset:0;text-align:center;font-size:10.5px;font-weight:500;line-height:14px}
.spark{display:flex;align-items:flex-end;gap:1px;height:38px}
.spark i{flex:1;background:var(--acc);opacity:.5;min-height:1px}
.chart{position:relative;width:100%}
.chart svg{width:100%;height:auto;display:block}
.chart .cg{stroke:var(--line);stroke-width:.5}
.chart .cax{fill:var(--mut);font-size:9px;font-family:system-ui,sans-serif}
.chart .cguide{stroke:var(--acc);stroke-width:.7;stroke-dasharray:3 3;opacity:0}
.chart .ctip{position:absolute;pointer-events:none;background:var(--panel2);border:1px solid var(--acc);border-radius:6px;padding:4px 9px;font-size:12px;white-space:nowrap;opacity:0;transform:translate(-50%,-118%);transition:opacity .08s;z-index:2}
.chart-legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin:2px 0 6px}
.mapinfo{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:6px 10px;font-size:12.5px;margin:6px 0;min-height:18px;display:flex;flex-direction:column;justify-content:center;gap:2px}
.chart-legend b{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.card.wide{grid-column:1 / -1}
.sc{max-height:360px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.sc>table{border:0}
.sc-lg{max-height:60vh}
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
  dash:"Дашборд", act:"Действия", srv:"Серверы", load:"Нагрузка",
  ld_intro:"Нагрузка сервера, игры и панели — снимок раз в {n} с, хранится ~6 недель. На графике — среднее за интервал, в подсказке — ещё и максимум. CPU процессов — в процентах от всей машины ({c} ядер).",
  ld_nodata:"данных пока нет — первые точки появятся через минуту", ld_cpu:"CPU", ld_cpu_core:"CPU: самое загруженное ядро", ld_ram:"Память", ld_diskio:"Диск: операций в секунду (IOPS)", ld_diskmb:"Диск: скорость",
  ld_gameio:"Игра: операций с диском в секунду", ld_net:"Сеть", ld_req:"Панели: запросов в минуту", ld_lat:"Панели: среднее время ответа", ld_online:"Игроков онлайн",
  ld_server:"Сервер", ld_game:"Игра", ld_panel:"Панель", ld_steam:"Steam", ld_read:"Чтение", ld_write:"Запись", ld_in:"Приём", ld_out:"Отдача", ld_admin:"Админка", ld_player:"Панель игроков",
  ld_proc:"Процессы сейчас", ld_col_proc:"Процесс", ld_col_cpu:"CPU", ld_col_ram:"RAM", ld_col_iops:"IOPS чт./зап.", ld_col_mbs:"МБ/с чт./зап.", ld_col_thr:"Потоки", ld_col_h:"Дескрипторы",
  ld_t_cpu:"CPU сервера", ld_t_ram:"Память", ld_t_iops:"Диск IOPS", ld_t_disk:"Диск", ld_t_net:"Сеть", ld_t_free:"Свободно на диске мира", ld_t_online:"Онлайн", ld_t_core:"Макс. ядро",
  ld_notrun:"не запущен", ld_mbs:"МБ/с", ld_ops:"оп/с", ld_gb:"ГБ", ld_ms:"мс", ld_rpm:"в мин", ld_players:"игроков", ld_avg:"сред.", ld_max:"макс.", ld_auto:"обновлять каждые 30 с", chat:"Чат", stats:"Статы", map:"Карта", players:"Игроки", twinks:"Твинки", entry:"Вход", buffs:"Микстуры", food:"Кулинария", clans:"Кланы", craft:"Крафт", trade:"Торговля", economy:"Экономика", suspicious:"Нарушения", activity:"Активность", leaders:"Рейтинги", admin:"Админ", fleet:"Флот", roles:"Настройки", logs:"Логи",
  pf_title:"Поиск предмета у игроков", pf_ph:"id или имя предмета", pf_go:"искать",
  pf_wait:"сканирую инвентари игроков…", pf_none:"ни у кого нет", pf_players:"игроков",
  pf_stash:"склад", pf_carry:"при себе", pf_total:"всего", pf_matched:"совпадения по имени",
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
  set_save:"Сохранить настройки", set_saved:"Сохранено", set_restart:"Часть изменений применится после перезапуска задачи.", set_accounts:"Игровые аккаунты", set_acc_add:"＋ аккаунт", set_acc_label:"метка", set_acc_user:"логин", set_acc_pw:"пароль (пусто = не менять)", set_acc_active:"активный", set_lf:"login_flow (JSON, продвинутое)", set_secret_set:"задан", set_secret_ph:"оставьте пустым, чтобы не менять", roles_alerts:"Алерты в Telegram включены", roles_hint:"ID через запятую/пробел/с новой строки. ID из обоих списков считается администратором. Нужен ≥1 админ. Главный админ должен быть среди администраторов.",
  save:"Сохранить", saved:"Сохранено, роли применены на лету",
  entry_intro:"Тюнер координат входа: снимок окна игры, клик по нему — проценты ширины/высоты окна (не зависят от разрешения/DPI). Требует запущенную задачу SigmaNav в интерактивной сессии.",
  bn_intro:"Сервер сам ведёт библиотеку всех смешиваний микстур всех игроков (Data\\product\\buff_lib.json) — панель читает её напрямую, загружать buff_notepad.json с машины игрока больше не нужно.",
  bn_src_server:"библиотека сервера",
  bn_upload:"Библиотека рецептов сервера", bn_paste:"…или вставьте содержимое файла сюда",
  bn_save:"Сохранить", bn_saved:"Сохранено", bn_invalid:"Не похоже на buff_notepad.json",
  bn_none:"Пока ничего не загружено.", bn_count:"записей", bn_saved_at:"загружено",
  bn_all:"все", bn_records:"Комбинации", bn_time:"время, с", bn_effect:"эффект",
  bn_no_effect:"без эффекта", bn_ingredients:"ингредиенты",
  bn_state_note:"Формула смешивания разобрана и проверена (совпадает с сервером на всех известных рецептах) — оптимизатор ниже считает точно, не угадывает.",
  bn_opt_title:"Оптимизатор рецептов", bn_opt_intro:"Отметь, какие ингредиенты у тебя есть, выбери желаемый эффект — переберём все сочетания по 4 и найдём топ-5.",
  bn_opt_target:"Эффект", bn_opt_go:"Найти", bn_opt_checked:"комбинаций проверено", bn_opt_found:"с положительным эффектом",
  bn_opt_none:"Ни одна комбинация из выбранных ингредиентов не даёт этот эффект в плюс.",
  bn_opt_all:"все", bn_opt_none_sel:"ничего", bn_opt_working:"считаю…",
  bn_bt0:"Здоровье", bn_bt1:"Энергия", bn_bt2:"Меткость", bn_bt3:"Скорость движения",
  bn_bt4:"Скорость действия", bn_bt5:"Сила ближнего боя", bn_bt6:"Сила дальнего боя",
  bn_bt7:"Щит", bn_bt8:"Скорость ближнего боя", bn_bt9:"Скорость дальнего боя",
  fd_lib:"Блюда сервера", fd_lib_intro:"Все блюда, которые когда-либо готовили на этом сервере (Data\\product\\product_lib.json) — сервер сам их запоминает, ничего загружать не нужно.",
  fd_only_eat:"только съедобные (сытость > 0)", fd_sort_eat:"сортировать по сытости", fd_shown:"показано",
  fd_eat:"Сытость", fd_genes:"Гены", fd_no_genes:"без генов", fd_per_point:"блюд на 1 очко генетики",
  fd_gene_gain:"к каждому гену", fd_slots:"Порядок в слотах (сетка 2×2) важен — клади ровно так:",
  fd_opt_title:"Оптимизатор блюд", fd_opt_intro:"Отметь ингредиенты, которые есть, выбери гены, которые обязательно нужны, и (желательно) свой максимум сытости: блюдо сытнее максимума игра просто не даст съесть. Переберём все сочетания по 4 во всех порядках.",
  fd_need_genes:"Нужные гены", fd_max_eat:"Макс. сытость", fd_max_eat_ph:"напр. 150",
  fd_opt_found:"подходящих наборов", fd_opt_none:"Ни одно сочетание не подходит под условия.",
  fd_note:"Формула готовки разобрана из кода игры и проверена на всех блюдах сервера (совпадение 100%). Съеденное блюдо: сытость +N, энергия +N/5, каждый ген блюда +N/5 к Генетике A–D; когда все четыре ≥ 100 — +1 очко генетики. Блюдо с генами ABCD качает генетику быстрее всего.",
  fd_all_on:"все", fd_all_off:"ничего",
  cl_none:"Кланов пока нет.", cl_intro:"Нажми на название клана — откроется состав, специализации, клановые технологии и схема изучения.",
  cl_name:"Клан", cl_player:"Игрок", cl_size:"Состав", cl_online:"Онлайн", cl_rating:"Рейтинг", cl_leader:"Лидер",
  cl_ctech:"Клан-технологии", cl_trade:"Торг. терминал", cl_back:"к списку кланов", cl_members:"Состав",
  cl_role:"Роль", cl_techs:"Техов", cl_now:"сейчас", cl_spec:"Специализация",
  cl_spec_hint:"Строка — позитивная специализация, столбец — негативная. В ячейке — кто её занимает.",
  cl_ctech_scheme:"Схема клановых технологий", cl_ptech_scheme:"Личные технологии участников",
  cl_cov_all:"весь клан — сколько участников знают", pd_tech_scheme:"Схема изучения",
  ts_cost:"время изучения", ts_level:"уровень", ts_known:"знают",
  ts_st_done:"изучено", ts_st_cur:"изучается сейчас", ts_st_avail:"доступно", ts_st_lock:"закрыто",
  ts_cov_legend:"знает хотя бы один (ярче — больше участников)",
  ts_hint:"Наведи на квадрат — название, время и статус. Ветка идёт слева направо, ответвления — новые строки под родителем.",
  ts_unlocks:"открывает", ts_pick_hint:"Кликни по технологии — подсвечу путь до неё и покажу, сколько осталось.",
  ts_path_left:"до неё осталось", ts_steps:"шаг.", ts_who_closer:"Кто из участников ближе всего (меньше часов до цели):",
  ch_title:"История клана", ch_intro:"У игры истории кланов нет — панель сама раз в несколько минут сверяет clans.json и раз в час сохраняет точку рейтинга/CP/состава. Данные копятся с момента включения.",
  ch_empty:"Пока пусто — история начнёт копиться после первых проходов трекинга.",
  ch_joined:"вступил", ch_left:"ушёл", ch_role:"смена роли", ch_tech:"клан-технология", ch_renamed:"переименован",
  ch_slots:"расширен состав", ch_created:"клан создан", ch_disbanded:"клан распущен",
  cr_title:"Калькулятор крафта", cr_intro:"Выбери предмет и количество — разложу до сырья (крафт + станки), покажу промежуточные крафты, время, верстаки и нужные технологии. Если выбрать игрока или клан — отмечу, что у них уже изучено и сколько осталось до недостающего.",
  cr_ph:"предмет (название или id)", cr_who:"технологии:", cr_who_none:"не проверять", cr_who_player:"игрок (ID)…",
  cr_go:"Разложить", cr_sec:"с", cr_or:"или из", cr_raw:"сырьё", cr_time:"Время крафта", cr_benches:"Верстаки / станки",
  cr_used_in:"Используется в", cr_techs:"Технологии", cr_tech:"Технология", cr_status:"Статус", cr_no_tech:"Технологии не нужны.",
  cr_raw_total:"Сырьё всего", cr_item:"Предмет", cr_count:"Кол-во", cr_inter:"Промежуточные", cr_crafts:"Крафтов", cr_tree:"Дерево крафта",
  tr_title:"Торговля", tr_intro:"Все предложения сервера: торговые терминалы игроков (Data\\game\\terminals.dt2) и магазины, стоящие на картах (.dt). Первый разбор всех карт занимает до минуты, дальше — кэш.",
  tr_ph:"поиск по предмету", tr_src_all:"все источники", tr_src_term:"терминал", tr_src_shop:"магазин на карте",
  tr_side_any:"отдают или хотят", tr_side_give:"продают (отдают)", tr_side_want:"покупают (хотят взамен)",
  tr_offers:"предложений", tr_terms:"терминалов", tr_shops:"магазинов", tr_scan:"разбор",
  tr_owner:"Владелец", tr_give:"Отдаёт", tr_want:"Хочет взамен", tr_rate:"Курс", tr_where:"Где",
  tr_terms_list:"Терминалы игроков", tr_shops_list:"Магазины на картах", tr_lots:"Лотов", tr_sales:"Продаж",
  tr_idle:"Не заходил", tr_storage:"Выручка на складе", tr_loading:"собираю данные по всем картам…",
  mi_by_clan:"по кланам", mi_shops:"магазины", mi_shops_hint:"Значки магазинов игроков на карте", mi_no_clans:"на этой карте нет земли кланов",
  mi_blocks:"уч. 8×8", mi_owners:"владельцев", mi_noclan:"без клана",
  fav_title:"Иконка панели", fav_remove:"Убрать", fav_big:"Файл больше 512 КБ",
  fl_title:"Флот игроков", fl_intro:"Корабли в космосе по владельцам (снимок space\\units.dt на момент автосохранения сервера) и станции игроков (Data\\stations).",
  fl_ships:"кораблей", fl_owners:"владельцев", fl_stations:"станций", fl_ship:"Корабль", fl_cargo:"Груз", fl_stations_t:"Станции", fl_size:"Размер",
  ac_days:"Игроки по дням (60 дней)", ac_players:"игроков всего", ac_active:"Заходили за день", ac_reg:"Регистрации",
  ac_cohorts:"Удержание по неделям регистрации", ac_cohorts_hint:"D1 — вернулись на следующий день, D7 — на 7–13 день, D30 — на 30–59 день после регистрации (считаются только недели, которым уже хватает времени).",
  ac_week:"Неделя", ac_size:"Новых", ac_heat:"Когда играют", ac_heat_hint:"Среднее число игроков онлайн по дням недели и часам за последние 4 недели. Максимум:",
  ac_wd:"Пн,Вт,Ср,Чт,Пт,Сб,Вс", ac_churn:"На каком уровне бросают", ac_churn_hint:"«Ушёл» — не заходил {d} дней.",
  ac_quit1h:"бросили, наиграв меньше часа", ac_act:"Активны", ac_gone:"Ушли", ac_rate:"Отток",
  lb_traders:"Торговцы (продажи)", lb_rweek:"Исследования за неделю (техов)", lb_rtotal:"Всего часов исследований",
  lb_clans:"Кланы: рост рейтинга за неделю", lb_growth:"Рост", lb_since:"с", lb_clans_hint:"Рост считается по истории кланов, которую ведёт панель.",
  lb_hint:"Богатство — склад + при себе. Торговцы — продажи через терминалы (и магазины, если «Торговля» уже открывалась). Исследования за неделю — по журналу трекинга техов.",
  ad_title:"Инструменты админа", ad_intro:"Все действия — только для оффлайн-игроков (онлайн пропускаются), перед каждой правкой файл игрока бэкапится, всё пишется в аудит. Нужен пароль панели.",
  ad_mass:"Массовая выдача предметов", ad_mass_hint:"Кому: ID через запятую, clan:N — весь клан, active:D — все, кто заходил за D дней (можно сочетать); пусто — ВСЕМ игрокам. Предмет — выберите из списка (или id). Кладётся на склад; игрокам в сети — нет (если игра остановлена, в сети никого).",
  ad_targets_ph:"пусто = всем; 12, 40, clan:3, active:7", ad_item_ph:"предмет — начните вводить название", ad_give:"Выдать",
  ad_mass_q:"Выдать всем выбранным оффлайн-игрокам?", ad_mass_all_q:"Поле «кому» пустое — выдать ВСЕМ игрокам сервера (оффлайн)?",
  ad_srv_off:"Игра остановлена — сервер оффлайн, все игроки считаются оффлайн: правки применяются ко всем, клановые техи можно выдавать.",
  ad_clan_tech:"клановая", ad_clan_added:"В список клана добавлено", ad_reason:"Причина пропуска",
  ad_clantech:"Выдать технологию всему клану", ad_clantech_hint:"Выберите технологии из списка (можно несколько). 🛡 Клановые — добавляются в список технологий клана (clans.json, только при остановленной игре); обычные — каждому оффлайн-участнику.",
  ad_techs_ph:"id технологий", ad_clantech_q:"Выдать технологии всем оффлайн-участникам клана?",
  ad_rollback:"Откат инвентаря из бэкапа", ad_rollback_hint:"Бэкапы, которые панель сделала перед правками этого игрока. Откат заменяет весь инвентарь (склад или при себе) содержимым бэкапа; текущее состояние перед этим тоже бэкапится.",
  ad_show_backups:"Показать бэкапы", ad_no_backups:"Бэкапов по этому игроку нет.", ad_when:"Когда", ad_where:"Где", ad_content:"Содержимое",
  ad_restore:"Откатить", ad_restore_q:"Заменить инвентарь игрока содержимым этого бэкапа?", ad_restored:"откачено", ad_undo:"отменить можно бэкапом",
  fav_hint:"Показывается во вкладке браузера. PNG/ICO/JPEG/GIF/WebP до 512 КБ, лучше квадратная 64–256 px. Применяется сразу, без «Сохранить». Название панели — поле «Название панели» в блоке «Веб-панель» (после «Сохранить» обнови страницу).",
  ec_title:"Экономика сервера", ec_intro:"Сколько каждого предмета есть в мире: у игроков (склад + при себе), в сундуках и брошенное на картах (без природных кладов под лопату), в торговле (лоты и выручка терминалов/магазинов). Курс — медиана по предложениям в «Торговле». Динамика — по суточным снимкам, которые панель делает сама (копится с момента включения). Клик по строке — график и главные держатели.",
  ec_sort:"сортировка:", ec_s_total:"всего", ec_s_players:"у игроков", ec_s_cont:"в сундуках", ec_s_trade:"в торговле",
  ec_s_holders:"держателей", ec_s_d1:"рост за сутки", ec_s_d7:"рост за неделю",
  ec_items:"предметов", ec_players:"игроков с инвентарём", ec_snaps:"суточных снимков",
  ec_total:"Всего", ec_players_col:"У игроков", ec_cont:"В сундуках", ec_trade:"В торговле", ec_holders:"Держателей",
  ec_top1:"Больше всех", ec_rate:"Курс", ec_d1:"За сутки", ec_d7:"За неделю", ec_hist:"Всего в мире по дням", ec_top:"Главные держатели",
  sv_title:"Мониторинг нарушений", su_intro:"Панель сама раз в несколько минут сравнивает инвентари и исследования всех игроков. Всплеск — резкий рост монет/бустеров (пороги в config.json → players.suspicious.watch) или любого предмета в 10+ раз; рядом — у кого в то же время столько же убыло (возможная передача), ⚠ — если это связанный аккаунт (общий пароль/IP). Быстрое исследование — техи дороже, чем прошло времени + потрачено бустеров (выдача теха через панель тоже сюда попадёт — сверяйтесь с аудитом). Это подсказки для проверки, не приговор.",
  su_k_all:"все события", su_k_inv:"всплеск инвентаря", su_k_res:"быстрое исследование", su_only_twink:"только с твинками",
  su_events:"событий", su_none:"Ничего подозрительного.", su_twink:"твинк", su_donors:"у кого убыло",
  su_res_txt:"изучил техи на", su_allowed:"возможно", su_boost:"бустеров", su_trade:"Аномальные цены в торговле",
  su_trade_hint:"Лоты, у которых курс в 10+ раз отличается от медианы той же пары «товар → оплата» (нужно ≥3 предложений). Слишком дёшево — частый способ передать ценности своему твинку.",
  su_trade_need:"Сначала откройте вкладку «Торговля» — анализ цен берёт её свежие данные.", su_vs_median:"от медианы",
  entry_shot:"Обновить снимок", entry_state:"Определить экран", entry_testclick:"Тест-клик по точке",
  entry_seq:"Прогнать вход", entry_seq_confirm:"Прогнать полную последовательность входа (login) прямо сейчас?",
  entry_pick_hint:"кликните по снимку — координаты появятся здесь", entry_pick:"выбрано",
  entry_add_step:"+ шаг из выбранной точки", entry_use_pick:"взять выбранную точку", entry_saved:"Сохранено",
  log_sup:"Супервизор", log_audit:"Аудит панели", log_nav:"Вход в игру (скрины)", log_act:"Журнал активности", log_blocks:"Блокировки входа",
  fl_near:"рядом:", fl_transit:"в пути, вдали от объектов системы",
  ph_title:"История цен рынка", ph_intro:"Цена за 1 шт. по предложениям у терминалов и магазинов — снимок раз в час (панель игроков, копится с 25.09.2026). Резкие изменения — медианная цена в той же валюте изменилась на 50% и больше за сутки или неделю: демпинг, накрутка, перекачка ценностей через рынок.", ph_snaps:"снимков", ph_alerts:"Резкие изменения цен", ph_noalerts:"Резких изменений цен нет.", ph_item:"Предмет (валюта)", ph_period:"За", ph_was:"Было", ph_now:"Сейчас", ph_lots:"Лотов", ph_pick:"график цены предмета", ph_median:"Медианная цена", ph_min:"Минимальная цена", ph_other:"Также продают за",
  sr_title:"Сезонный рейтинг — полностью", sr_intro:"Игроков с очками в сезоне: {n} из {all}. Награда по месту — фиксированная шкала (места 1–6). «Получено» — сколько раз и сколько ускорителей игрок получил за всё время.", sr_last:"Последняя раздача", sr_find:"ник или клан", sr_clan:"Клан", sr_points:"Очки", sr_gap:"Отставание от места выше", sr_reward:"Награда сейчас", sr_got:"Получено",
  pt_title:"Инструменты", pt_where:"Всё имущество", pt_where_ph:"фильтр по предмету", pt_total:"Итого предметов", pt_place:"Где", pt_items:"Что лежит", pt_market_pending:"Склад терминала появится через пару минут — собираются данные рынка.",
  pt_journal:"Журнал игрока", pt_journal_hint:"События игрока из журналов сервера и панели в одной ленте. Кнопки — показать/скрыть категорию.", pt_journal_gm:"События игрока из журналов сервера и панели в одной ленте, плюс (для GM) его действия в панели игроков и в админке за 30 дней — категория «Панель». Кнопки — показать/скрыть категорию.",
  pt_when:"Когда", pt_event:"Событие", pt_k_tech:"Техи", pt_k_research:"Исследование", pt_k_booster:"Ускорители", pt_k_level:"Уровни", pt_k_clan:"Клан", pt_k_death:"Смерти", pt_k_reward:"Награды", pt_k_role:"Роли", pt_k_land:"Участки", pt_k_session:"Сессии", pt_k_panel:"Панель",
  pt_hist:"Графики", pt_techs:"Изучено техов", pt_hist_none:"Истории пока нет — снимки делаются раз в час.", pt_hist_since:"Снимки с",
  pt_viewas:"Глазами игрока", pt_viewas_hint:"Ссылка одноразовая и действует 60 секунд. Откроет панель игрока от его имени только на чтение на 30 минут; вход и все действия записываются в журнал активности. Выход — кнопкой «Выйти из просмотра» вверху страницы.", pt_viewas_open:"Открыть панель игрока глазами",
  sg_title:"Генерация космоса", sg_run:"Анализировать", sg_intro:"Что в космосе (звёздные системы, кластеры, стартовая карта новичков) создано или изменено после первоначальной генерации мира и когда. Три независимых признака: порядок номеров звёзд в кластерах (не зависит от дат), даты файлов (если мир копировали или восстанавливали — это даты копирования) и история по бэкапам мира; плюс дата обновления игры в Steam. Только чтение.",
  sg_world:"Мир", sg_gen:"Генерация мира", sg_stars:"Звёздных систем", sg_missing:"нет номеров:", sg_clusters:"Кластеров", sg_start:"Стартовая карта новичков", sg_was:"было", sg_steam:"Обновление игры в Steam", sg_backups:"Бэкапов мира",
  sg_concl:"Выводы", sg_late:"Системы, созданные или изменённые после генерации", sg_what:"Что", sg_when:"Когда", sg_cluster:"Кластер", sg_objs:"Объектов (план./спутн./астер.)", sg_planets:"Планеты",
  sg_created:"создана", sg_modified:"изменена", sg_noname:"(без имени)", sg_ooo:"вне порядка генерации", sg_startcl:"стартовый", sg_clch:"Кластеры, изменённые после генерации", sg_startfiles:"Файлы стартовой карты", sg_file:"Файл", sg_hist:"История по бэкапам (только изменения)", sg_backup:"Бэкап", sg_diff:"Что изменилось",
  sp_item:"Предмет", sp_k1_planet:"планета", sp_k1_satellite:"спутник", sp_k1_asteroid:"астероид",
  pz_hint:"колёсико — масштаб, мышью — двигать, двойной клик — сброс", sp_cluster:"кластер", sp_objs:"объектов", sp_owners:"владельцев", sp_system:"Система",
  sp_find_ph:"найти систему по имени", sp_galaxy:"Галактика", sp_galaxy_hint:"Все звёздные системы сервера (позиции из заголовков star*.json, индекс обновляется раз в час). Синие — есть участки (размер — сколько), оранжевые — только корабли. Клик — открыть систему ниже.",
  sp_leg_claims:"есть участки", sp_leg_ships:"только корабли", sp_leg_other:"остальные", sp_busiest:"Самые заселённые системы",
  sp_k_planet:"планеты", sp_k_satellite:"спутники", sp_k_asteroid:"астероиды", sp_only_claimed:"только с участками", sp_stations:"станции",
  sp_st_landed:"на земле:", sp_st_parked:"рядом:", sp_st_flight:"в полёте", sp_st_open:"в открытом космосе",
  sp_claimed_objs:"Объекты с участками", sp_obj:"Объект", sp_type:"Тип", sp_model:"Корабль", sp_status:"Состояние",
  map_space:"Космос", pd_terr_maps:"карт:", pd_terr_n:"Участков",
  role_gm:"GM", ac_src_all:"— где —", ac_src_admin:"Админка", ac_src_player:"Панель игроков", ac_src_guard:"Защита входа",
  ac_ev_all:"— все события —", ac_ev_logins:"Входы/выходы", ac_ev_bad:"Неудачи, отказы, зондирование", ac_ev_req:"Запросы (что смотрели/искали)",
  ac_ev_ui:"Действия в интерфейсе", ac_ev_audit:"Аудит (изменения)", ac_ev_guard:"Блокировки",
  ac_user:"кто (ник/логин/#id)", ac_ip:"IP", ac_text:"текст (поиск по всему)", ac_hours:"за", ac_h1:"1 час", ac_h24:"сутки", ac_h168:"неделю", ac_hall:"всё время",
  ac_find:"Найти", ac_more:"Ещё", ac_col_ts:"Время", ac_col_src:"Где", ac_col_who:"Кто", ac_col_ev:"Событие", ac_col_d:"Подробности",
  ac_none:"Ничего не найдено", ac_size:"Журнал", ac_intro:"Всё, что делают в админке и в панели игроков: входы и неудачные попытки, каждый запрос (что открывали и искали), действия в интерфейсе, изменения. Одинаковые автообновления — не чаще раза в 2 минуты (×N — сколько раз повторилось). Клик по нику или IP — отфильтровать.",
  ev_login_ok:"вход", ev_login_fail:"НЕУДАЧНЫЙ вход", ev_login_blocked:"вход ЗАБЛОКИРОВАН", ev_logout:"выход", ev_admin_enter:"кнопка «Админка»",
  ev_req:"запрос", ev_page:"открыл страницу", ev_denied:"ОТКАЗ", ev_probe:"зондирование", ev_ui:"действие", ev_audit:"изменение", ev_block:"БЛОКИРОВКА", ev_flood:"МАССОВЫЙ ПЕРЕБОР",
  gb_intro:"Защита от перебора паролей (админка и панель игроков). Адрес: {ip} неудач за {ipw} мин → блок. Учётка: {ac} неудач за {acw} мин с любых адресов → вход с новых адресов закрыт (с адресов, откуда уже входили, — можно). Блокировка растёт: {steps}. О блокировках учёток и массовом переборе — тревога главному админу в Telegram.",
  gb_fails10:"Неудачных входов за 10 мин", gb_flood:"идёт массовый перебор!", gb_key:"Ключ", gb_left:"Осталось", gb_lvl:"Уровень", gb_fails:"Неудач в окне", gb_who:"Последний логин", gb_unblock:"Снять", gb_none:"Блокировок нет",
  level:"Уровень", lines:"строк", download:"Скачать", navshots_none:"Скринов последовательности входа нет",
  chpass_title:"Смена пароля", chpass_note:"Вход по умолчанию admin/admin. Смените пароль сейчас — минимум 6 символов, не «admin».",
  chpass_old:"Текущий пароль", chpass_new:"Новый пароль", chpass_rep:"Повторите новый пароль",
  chpass_mismatch:"Пароли не совпадают", change:"Сменить пароль",
  err_forbidden:"Недостаточно прав для этого действия", role_admin:"админ", role_moderator:"модератор", role_viewer:"наблюдатель",
  us_title:"Пользователи панели", us_intro:"Админ — всё; модератор — просмотр, скриншот, перезапуск игры и вход, бан/разбан, нарушения и твинки; наблюдатель — только просмотр (без паролей, приватов и IP игроков, без рецептов). Новый пользователь сменит пароль при первом входе.",
  us_name:"Логин", us_role:"Роль", us_pw:"Временный пароль (от 6 символов)", us_add:"Добавить", us_reset:"Сбросить пароль", us_del:"Удалить",
  us_me:"это вы", us_mc:"ждёт смены пароля", us_new_pw:"Новый временный пароль для ", us_confirm_del:"Удалить пользователя ",
  err_bad_credentials:"Неверный логин или пароль", err_throttled:"Слишком много попыток, подождите",
  err_bad_old:"Текущий пароль неверный", err_too_short:"Минимум 6 символов", err_too_weak:"Слишком простой пароль",
  err_auth:"Сессия истекла — войдите заново", err_net:"Нет связи с сервером",
  pl_head:"Игроки локального сервера", pl_world:"Мир", pl_registered:"зарегистрировано",
  pl_online:"онлайн (по аналитике)", pl_online_gs:"онлайн (game_state)", pl_bymap:"По картам",
  pl_stale:"исправлено", pl_stale_hint:"столько игроков висели «онлайн» с прошлого запуска сервера (не было exit после падения/рестарта) — панель сама сверила их последний вход со временем последнего Server ready и убрала из счёта",
  pl_space_note:"карта 0 = космос: игра считает игроков онлайн, фактически могут быть оффлайн",
  pl_only_online:"Только онлайн", pl_search:"поиск по имени",
  pl_col_status:"Статус", pl_col_map:"Карта", pl_col_pos:"Коорд.",
  pl_col_enter:"Вход", pl_col_exit:"Выход", pl_col_sess:"Сессия",
  pl_col_hours:"Часов", pl_col_lvl:"Ур.", pl_col_role:"Роль", pl_col_ban:"Бан",
  pl_role_player:"игрок", pl_role_staff:"стафф", pl_role_mod:"модератор", pl_role_admin:"админ", pl_role_gm:"GM",
  pl_recent:"Последние события",
  pl_ev_register:"зарегистрировался", pl_ev_enter:"вошёл", pl_ev_exit:"вышел",
  pl_none:"Данных о игроках нет", pl_map:"карта",
  tw_intro:"Твинки: по общему паролю (code), по IP и по железу. Чувствительно — под паролем панели.",
  tw_prompt:"Подтвердите своим паролем от панели:", tw_min:"мин. аккаунтов на IP",
  tw_show:"Показать", tw_none:"Совпадений нет", tw_summary:"IP всего",
  tw_flagged:"помечено IP", tw_connects:"подкл.", tw_other_ips:"ещё IP", tw_ignored:"игнор",
  tw_bycode:"по паролю", tw_byip:"по IP", tw_byfp:"по железу (много ложных)", tw_samepw:"общий пароль",
  st_online:"Онлайн (7 дней)", st_now:"сейчас", st_peak:"пик 7д", st_growth:"Рост",
  st_reg:"рег.", st_dau:"актив/день", st_ret:"retention", st_toplvl:"Топ по уровню",
  st_toptime:"Топ по часам", st_clans:"Кланы", st_month:"Топ месяца", st_bans:"Бан-лист",
  st_staff:"Стафф", st_lvldist:"Уровни", st_countries:"Страны", st_hist:"история ролей",
  st_world:"Мир · карты", st_terr:"Территории", st_owner:"владелец", st_avatars:"аватары", st_terrfilter:"карта",
  sp_title:"Космос", sp_inspace:"в космосе сейчас", sp_stuck:"залипли оффлайн", sp_units:"космо-юнитов всего",
  sp_ship:"есть корабль (spaceUnitId)", sp_planets:"Освоение других карт", sp_plots:"участков", sp_owners:"владельцев",
  su_title:"Звёздная система (снимок)", su_ships:"корабли", su_meteorites:"метеориты", su_pods:"космо-предметы", su_vel:"скорость (vx,vy)", su_hp:"HP",
  su_cargo:"груз", su_moving:"в движении", su_stopped:"стоит",
  st_toptech:"Популярные техи", st_researching:"изучает", st_tech:"тех", st_size:"размер",
  st_branch:"открывает", st_technote:"названия — что тех открывает в крафте (из craft.json + локализации клиента); ветка/тир — из дерева tech.json",
  md_open:"показать карту", md_title:"Карта .dt", md_parsing:"разбираю бинарную карту (крупная — до ~15 c)…",
  md_blocks:"блоки", md_machines:"машины", md_ore:"руда / камень", md_containers:"в контейнерах мира",
  md_landowners:"владельцы земли (блоки 8×8)", md_ground:"суша / вода", md_misc:"прочее",
  md_containers_list:"Содержимое контейнеров", md_containers_filter:"фильтр по предмету",
  md_containers_col_where:"Где", md_containers_col_items:"Содержимое", md_containers_capped:"список обрезан лимитом",
  md_offworld_hint:"внесистемная карта (не 0/1) — планета/данж; имя и координаты в звёздной системе (Data\\world\\star1.json, id карты = id записи, реверс-инжиниринг — см. схему системы)",
  md_spacename:"имя в космосе (коорд.)",
  su_starmap:"Схема системы", su_star:"звезда", su_planets:"планеты", su_satellites:"спутники", su_asteroids:"астероиды",
  su_hidden:"скрыто (нет тел рядом):",
  su_scatter_note:"позиции планет — реверс-инжиниринг бинарного формата Data\\world\\star<N>.json без исходника (сервер-генератор мира не дан); координаты проверены, но имена НЕ уникальны между звёздными системами",
  su_hover_hint:"наведите курсор на карту",
  su_find_ph:"имя планеты/спутника/астероида (звезда 1)", su_find_none:"не найдено в этой системе",
  su_find_ph2:"имя планеты/спутника/астероида —", su_cluster:"кластер", su_system:"система",
  su_clusters:"кластеров", su_systems:"систем всего",
  su_find_hits:"найдено", su_find_click:"показать на схеме", su_objects:"объектов",
  mf_title:"Поиск предмета в мире", mf_ph:"id или имя (напр. tech_booster)", mf_map:"карта",
  mf_all:"весь мир", mf_go:"искать", mf_wait:"сканирую карты (весь мир — до ~2 мин, кэшируется)…",
  mf_total:"всего штук", mf_spots:"точек", mf_scanned:"карт просканировано", mf_where:"где",
  mf_nomatch:"ничего не найдено", mf_matched:"совпадения по имени",
  mf_owner:"на чьей земле", mf_byowner:"по владельцам земли", mf_nobody:"— ничья —",
  mi_title:"Картинка карты", mi_wait:"рисую…", mi_claims:"клаймы", mi_owner:"владелец id",
  mi_hover_hint:"наведите курсор на карту", mi_free:"свободно",
  mi_rot_ccw:"повернуть против часовой на 45°", mi_rot_cw:"повернуть по часовой на 45°",
  mi_review:"пересмотреть", mi_review_hint:"перерисовать карту заново, игнорируя кэш",
  mi_show:"показать", mi_water:"вода", mi_land:"суша", mi_grass:"природа", mi_mtn:"горы",
  mi_ore:"руда", mi_wall:"стены/пол", mi_built:"постройки", mi_claim:"клаймы = цвет по владельцу (галка), либо один владелец по id",
  st_toptechp:"Топ по числу техов", st_techs:"техов", st_resh:"часы иссл.",
  st_resh_note:"= сумма стоимости изученных техов (tech.json cost в минутах, 1440 = сутки); исследование идёт и оффлайн, бустеры/мозги ускоряют",
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
  tt_title:"Трекинг техов / бустеров", tt_none:"пока пусто (панель ведёт лог с момента включения)",
  tt_gained:"изучил", tt_spent:"потратил бустер", tt_bgain:"получил бустер", tt_reschg:"новое исследование", tt_map:"сменил карту",
  tt_chart_title:"Учёба и бустеры по дням", tt_chart_tech:"техов изучено", tt_chart_boost:"бустеров потрачено",
  pd_inv_edit:"Правка инвентаря (только оффлайн)", pd_inv_online:"игрок сейчас онлайн — правка недоступна",
  pd_inv_give:"Выдать на склад", pd_inv_take:"Изъять", pd_inv_item:"предмет: имя или id",
  pd_inv_count:"кол-во", pd_inv_from:"откуда", pd_inv_carry:"при себе",
  pd_mod:"Модерация (оффлайн)", pd_mod_ban:"Забанить", pd_mod_unban:"Разбанить",
  pd_mod_tp:"Телепорт", pd_mod_givetech:"Выдать техи", pd_mod_resetpw:"Сброс пароля игрока",
  pd_mod_newcode:"новый пароль игрока",
  pd_p0:"Энергия", pd_p1:"Сытость", pd_p2:"Здоровье", pd_p3:"Меткость",
  pd_p4:"Скорость движения", pd_p5:"Скорость действия", pd_p6:"Скорость атаки",
  pd_p7:"Генетика A", pd_p8:"Генетика B", pd_p9:"Генетика C", pd_p10:"Генетика D",
  pd_p11:"Кислород", pd_p12:"Очки генетики",
  pd_lp0:"Опыт", pd_lp1:"Уровень", pd_lp2:"Очки распределения",
  pd_skill_pfx:"Навык", pd_skill_hint:"название неизвестно панели — по 2% к чему-то за уровень",
  ago:"назад", never:"нет данных", n_a:"н/д",
  disk_used:"занято", disk_free:"свободно", disk_tip:"Диск с миром игры: занято {u} из {t} ГБ, свободно {f} ГБ" },
 en:{ title:"SigmaSteamBot", logout:"Log out", login:"Log in", user:"Username", pass:"Password",
  dash:"Dashboard", act:"Actions", srv:"Servers", load:"Load",
  ld_intro:"Server, game and panel load — sampled every {n} s, kept ~6 weeks. The chart shows the interval average; the tooltip adds the maximum. Process CPU is a share of the whole machine ({c} cores).",
  ld_nodata:"no data yet — the first points appear in a minute", ld_cpu:"CPU", ld_cpu_core:"CPU: busiest core", ld_ram:"Memory", ld_diskio:"Disk: operations per second (IOPS)", ld_diskmb:"Disk: throughput",
  ld_gameio:"Game: disk operations per second", ld_net:"Network", ld_req:"Panels: requests per minute", ld_lat:"Panels: average response time", ld_online:"Players online",
  ld_server:"Server", ld_game:"Game", ld_panel:"Panel", ld_steam:"Steam", ld_read:"Read", ld_write:"Write", ld_in:"In", ld_out:"Out", ld_admin:"Admin panel", ld_player:"Player panel",
  ld_proc:"Processes now", ld_col_proc:"Process", ld_col_cpu:"CPU", ld_col_ram:"RAM", ld_col_iops:"IOPS r/w", ld_col_mbs:"MB/s r/w", ld_col_thr:"Threads", ld_col_h:"Handles",
  ld_t_cpu:"Server CPU", ld_t_ram:"Memory", ld_t_iops:"Disk IOPS", ld_t_disk:"Disk", ld_t_net:"Network", ld_t_free:"Free on world disk", ld_t_online:"Online", ld_t_core:"Busiest core",
  ld_notrun:"not running", ld_mbs:"MB/s", ld_ops:"ops/s", ld_gb:"GB", ld_ms:"ms", ld_rpm:"per min", ld_players:"players", ld_avg:"avg", ld_max:"max", ld_auto:"refresh every 30 s", chat:"Chat", stats:"Stats", map:"Map", players:"Players", twinks:"Twinks", entry:"Login", buffs:"Mixtures", food:"Cooking", clans:"Clans", craft:"Craft", trade:"Trade", economy:"Economy", suspicious:"Violations", activity:"Activity", leaders:"Leaderboards", admin:"Admin", fleet:"Fleet", roles:"Settings", logs:"Logs",
  pf_title:"Find an item on players", pf_ph:"item id or name", pf_go:"search",
  pf_wait:"scanning player inventories…", pf_none:"nobody has it", pf_players:"players",
  pf_stash:"stash", pf_carry:"carried", pf_total:"total", pf_matched:"name matches",
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
  set_save:"Save settings", set_saved:"Saved", set_restart:"Some changes take effect after restarting the task.", set_accounts:"Game accounts", set_acc_add:"＋ account", set_acc_label:"label", set_acc_user:"username", set_acc_pw:"password (empty = keep)", set_acc_active:"active", set_lf:"login_flow (JSON, advanced)", set_secret_set:"set", set_secret_ph:"leave empty to keep", roles_alerts:"Telegram alerts enabled", roles_hint:"IDs separated by comma / space / newline. An ID in both lists counts as admin. At least one admin required. Super admin must be one of the admins.",
  save:"Save", saved:"Saved, roles applied live",
  entry_intro:"Login-flow coordinate tuner: a screenshot of the game window, click on it — percent of window width/height (resolution/DPI independent). Needs the SigmaNav scheduled task running in an interactive session.",
  bn_intro:"The server keeps its own library of every potion mix by every player (Data\\product\\buff_lib.json) — the panel reads it directly, no need to upload buff_notepad.json from a player's machine.",
  bn_src_server:"server library",
  bn_upload:"Server recipe library", bn_paste:"…or paste the file contents here",
  bn_save:"Save", bn_saved:"Saved", bn_invalid:"Doesn't look like a buff_notepad.json",
  bn_none:"Nothing uploaded yet.", bn_count:"records", bn_saved_at:"uploaded",
  bn_all:"all", bn_records:"Combos", bn_time:"time, s", bn_effect:"effect",
  bn_no_effect:"no effect", bn_ingredients:"ingredients",
  bn_state_note:"The mixing formula has been fully reverse-engineered and verified (matches the server on every known recipe) — the optimizer below computes exactly, it doesn't guess.",
  bn_opt_title:"Recipe optimizer", bn_opt_intro:"Check the ingredients you have, pick the effect you want — every 4-ingredient combo will be tried to find the top 5.",
  bn_opt_target:"Effect", bn_opt_go:"Search", bn_opt_checked:"combos checked", bn_opt_found:"with a positive effect",
  bn_opt_none:"None of the selected ingredients produce a positive value for this effect.",
  bn_opt_all:"all", bn_opt_none_sel:"none", bn_opt_working:"crunching…",
  bn_bt0:"Health", bn_bt1:"Energy", bn_bt2:"Accuracy", bn_bt3:"Move speed",
  bn_bt4:"Action speed", bn_bt5:"Melee strength", bn_bt6:"Ranged strength",
  bn_bt7:"Shield", bn_bt8:"Melee speed", bn_bt9:"Ranged speed",
  fd_lib:"Server dishes", fd_lib_intro:"Every dish ever cooked on this server (Data\\product\\product_lib.json) — the server keeps them itself, nothing to upload.",
  fd_only_eat:"edible only (satiety > 0)", fd_sort_eat:"sort by satiety", fd_shown:"shown",
  fd_eat:"Satiety", fd_genes:"Genes", fd_no_genes:"no genes", fd_per_point:"dishes per genetics point",
  fd_gene_gain:"to each gene", fd_slots:"Slot order (2×2 grid) matters — place exactly like this:",
  fd_opt_title:"Dish optimizer", fd_opt_intro:"Check the ingredients you have, pick the genes you need and (ideally) your max satiety: the game won't let you eat a dish above it. Every 4-ingredient combo in every order will be tried.",
  fd_need_genes:"Required genes", fd_max_eat:"Max satiety", fd_max_eat_ph:"e.g. 150",
  fd_opt_found:"matching sets", fd_opt_none:"No combination matches these conditions.",
  fd_note:"The cooking formula was reverse-engineered from the game code and verified on every dish on the server (100% match). Eating a dish: satiety +N, energy +N/5, each dish gene +N/5 to Genetics A–D; once all four reach 100 — +1 genetics point. ABCD dishes level genetics fastest.",
  fd_all_on:"all", fd_all_off:"none",
  cl_none:"No clans yet.", cl_intro:"Click a clan name to open its roster, specializations, clan techs and research scheme.",
  cl_name:"Clan", cl_player:"Player", cl_size:"Members", cl_online:"Online", cl_rating:"Rating", cl_leader:"Leader",
  cl_ctech:"Clan techs", cl_trade:"Trade terminal", cl_back:"back to clans", cl_members:"Roster",
  cl_role:"Role", cl_techs:"Techs", cl_now:"now", cl_spec:"Specialization",
  cl_spec_hint:"Row — positive specialization, column — negative. Cell — who holds it.",
  cl_ctech_scheme:"Clan tech scheme", cl_ptech_scheme:"Members' personal techs",
  cl_cov_all:"whole clan — how many members know it", pd_tech_scheme:"Research scheme",
  ts_cost:"research time", ts_level:"level", ts_known:"known by",
  ts_st_done:"researched", ts_st_cur:"researching now", ts_st_avail:"available", ts_st_lock:"locked",
  ts_cov_legend:"known by at least one (brighter — more members)",
  ts_hint:"Hover a square for name, time and status. Branches run left to right, forks start new rows under the parent.",
  ts_unlocks:"unlocks", ts_pick_hint:"Click a tech — I'll highlight the path to it and show what's left.",
  ts_path_left:"left to it", ts_steps:"steps", ts_who_closer:"Which members are closest (fewest hours to the target):",
  ch_title:"Clan history", ch_intro:"The game keeps no clan history — the panel diffs clans.json every few minutes and stores an hourly rating/CP/size point. Data accumulates from the moment it's enabled.",
  ch_empty:"Empty so far — history starts after the first tracking passes.",
  ch_joined:"joined", ch_left:"left", ch_role:"role change", ch_tech:"clan tech", ch_renamed:"renamed",
  ch_slots:"roster expanded", ch_created:"clan created", ch_disbanded:"clan disbanded",
  cr_title:"Craft calculator", cr_intro:"Pick an item and quantity — I'll break it down to raw materials (crafting + machines), with intermediate crafts, time, workbenches and required techs. Pick a player or clan to see what they already know and what's missing.",
  cr_ph:"item (name or id)", cr_who:"techs:", cr_who_none:"don't check", cr_who_player:"player (ID)…",
  cr_go:"Break down", cr_sec:"s", cr_or:"or from", cr_raw:"raw", cr_time:"Craft time", cr_benches:"Workbenches / machines",
  cr_used_in:"Used in", cr_techs:"Techs", cr_tech:"Tech", cr_status:"Status", cr_no_tech:"No techs needed.",
  cr_raw_total:"Raw materials total", cr_item:"Item", cr_count:"Qty", cr_inter:"Intermediate", cr_crafts:"Crafts", cr_tree:"Craft tree",
  tr_title:"Trade", tr_intro:"Every offer on the server: players' trade terminals (Data\\game\\terminals.dt2) and shops placed on maps (.dt). The first pass over all maps takes up to a minute, then it's cached.",
  tr_ph:"search by item", tr_src_all:"all sources", tr_src_term:"terminal", tr_src_shop:"map shop",
  tr_side_any:"selling or buying", tr_side_give:"selling (gives)", tr_side_want:"buying (wants)",
  tr_offers:"offers", tr_terms:"terminals", tr_shops:"shops", tr_scan:"scan",
  tr_owner:"Owner", tr_give:"Gives", tr_want:"Wants", tr_rate:"Rate", tr_where:"Where",
  tr_terms_list:"Player terminals", tr_shops_list:"Map shops", tr_lots:"Lots", tr_sales:"Sales",
  tr_idle:"Idle", tr_storage:"Revenue in storage", tr_loading:"collecting data from all maps…",
  mi_by_clan:"by clan", mi_shops:"shops", mi_shops_hint:"Player shop markers on the map", mi_no_clans:"no clan land on this map",
  mi_blocks:"8×8 plots", mi_owners:"owners", mi_noclan:"no clan",
  fav_title:"Panel icon", fav_remove:"Remove", fav_big:"File is over 512 KB",
  fl_title:"Player fleet", fl_intro:"Ships in space by owner (snapshot of space\\units.dt at the server's last autosave) and player stations (Data\\stations).",
  fl_ships:"ships", fl_owners:"owners", fl_stations:"stations", fl_ship:"Ship", fl_cargo:"Cargo", fl_stations_t:"Stations", fl_size:"Size",
  ac_days:"Players by day (60 days)", ac_players:"players total", ac_active:"Active that day", ac_reg:"Registrations",
  ac_cohorts:"Retention by registration week", ac_cohorts_hint:"D1 — came back the next day, D7 — on days 7–13, D30 — on days 30–59 after registering (only weeks old enough are counted).",
  ac_week:"Week", ac_size:"New", ac_heat:"When people play", ac_heat_hint:"Average players online by weekday and hour over the last 4 weeks. Max:",
  ac_wd:"Mon,Tue,Wed,Thu,Fri,Sat,Sun", ac_churn:"At which level players quit", ac_churn_hint:"\"Gone\" — hasn't logged in for {d} days.",
  ac_quit1h:"quit with under an hour played", ac_act:"Active", ac_gone:"Gone", ac_rate:"Churn",
  lb_traders:"Traders (sales)", lb_rweek:"Research this week (techs)", lb_rtotal:"Total research hours",
  lb_clans:"Clans: rating growth this week", lb_growth:"Growth", lb_since:"since", lb_clans_hint:"Growth comes from the clan history the panel keeps.",
  lb_hint:"Wealth — stash + carried. Traders — terminal sales (plus shops once \"Trade\" has been opened). Weekly research — from the tech tracking log.",
  ad_title:"Admin tools", ad_intro:"Everything applies to offline players only (online ones are skipped), each player file is backed up before editing, and everything is audited. Requires the panel password.",
  ad_mass:"Mass item grant", ad_mass_hint:"To: comma-separated IDs, clan:N — a whole clan, active:D — everyone active in the last D days (can be combined); empty — ALL players. Item — pick from the list (or id). Goes to the stash; not to online players (if the game is stopped, nobody is online).",
  ad_targets_ph:"empty = all; 12, 40, clan:3, active:7", ad_item_ph:"item — start typing its name", ad_give:"Grant",
  ad_mass_q:"Grant to all selected offline players?", ad_mass_all_q:"“To” is empty — grant to ALL server players (offline)?",
  ad_srv_off:"The game is stopped — the server is offline and every player counts as offline: edits apply to all, clan techs can be granted.",
  ad_clan_tech:"clan", ad_clan_added:"Added to the clan list", ad_reason:"Skip reason",
  ad_clantech:"Grant a tech to a whole clan", ad_clantech_hint:"Pick techs from the list (several allowed). 🛡 Clan techs go to the clan's tech list (clans.json, only while the game is stopped); regular ones to every offline member.",
  ad_techs_ph:"tech ids", ad_clantech_q:"Grant the techs to all offline clan members?",
  ad_rollback:"Roll back inventory from a backup", ad_rollback_hint:"Backups the panel made before editing this player. A rollback replaces the whole inventory (stash or carried) with the backup's; the current state is backed up first.",
  ad_show_backups:"Show backups", ad_no_backups:"No backups for this player.", ad_when:"When", ad_where:"Where", ad_content:"Contents",
  ad_restore:"Roll back", ad_restore_q:"Replace the player's inventory with this backup?", ad_restored:"rolled back", ad_undo:"undo via backup",
  fav_hint:"Shown in the browser tab. PNG/ICO/JPEG/GIF/WebP up to 512 KB, square 64–256 px works best. Applied immediately, no \"Save\" needed. The panel name is the \"Panel title\" field in \"Web panel\" (reload the page after \"Save\").",
  ec_title:"Server economy", ec_intro:"How much of every item exists: with players (stash + carried), in chests and dropped on maps (natural buried stashes excluded), in trade (terminal/shop lots and revenue). Rate — median across \"Trade\" offers. Trend — from daily snapshots the panel takes itself (accumulates from when it's enabled). Click a row for the chart and top holders.",
  ec_sort:"sort:", ec_s_total:"total", ec_s_players:"with players", ec_s_cont:"in chests", ec_s_trade:"in trade",
  ec_s_holders:"holders", ec_s_d1:"24h growth", ec_s_d7:"7d growth",
  ec_items:"items", ec_players:"players with inventory", ec_snaps:"daily snapshots",
  ec_total:"Total", ec_players_col:"Players", ec_cont:"Chests", ec_trade:"Trade", ec_holders:"Holders",
  ec_top1:"Top holder", ec_rate:"Rate", ec_d1:"24h", ec_d7:"7d", ec_hist:"World total by day", ec_top:"Top holders",
  sv_title:"Violation monitor", su_intro:"Every few minutes the panel diffs every player's inventory and research. Spike — a sharp rise in coins/boosters (thresholds in config.json → players.suspicious.watch) or any item 10×+; next to it — who lost about as much at the same time (possible transfer), ⚠ if that's a linked account (shared password/IP). Fast research — techs costing more than the elapsed time + boosters spent (a tech granted via the panel lands here too — check the audit). These are leads, not verdicts.",
  su_k_all:"all events", su_k_inv:"inventory spike", su_k_res:"fast research", su_only_twink:"twinks only",
  su_events:"events", su_none:"Nothing suspicious.", su_twink:"twink", su_donors:"who lost it",
  su_res_txt:"researched techs worth", su_allowed:"possible", su_boost:"boosters", su_trade:"Abnormal trade prices",
  su_trade_hint:"Lots whose rate is 10×+ off the median for the same \"goods → payment\" pair (needs ≥3 offers). Too cheap is a common way to pass valuables to a twink.",
  su_trade_need:"Open the \"Trade\" tab first — price analysis uses its fresh data.", su_vs_median:"vs median",
  entry_shot:"Refresh screenshot", entry_state:"Detect screen", entry_testclick:"Test-click point",
  entry_seq:"Run login", entry_seq_confirm:"Run the full login sequence right now?",
  entry_pick_hint:"click the screenshot — coordinates appear here", entry_pick:"picked",
  entry_add_step:"+ step from picked point", entry_use_pick:"use picked point", entry_saved:"Saved",
  log_sup:"Supervisor", log_audit:"Panel audit", log_nav:"In-game login (shots)", log_act:"Activity log", log_blocks:"Login blocks",
  fl_near:"near:", fl_transit:"in transit, far from system objects",
  ph_title:"Market price history", ph_intro:"Price per 1 item from terminal and shop offers — hourly snapshot (player panel, collected since 2026-09-25). Sharp changes — the median price in the same currency moved by 50% or more over a day or a week: dumping, inflation, value transfer via the market.", ph_snaps:"snapshots", ph_alerts:"Sharp price changes", ph_noalerts:"No sharp price changes.", ph_item:"Item (currency)", ph_period:"Over", ph_was:"Was", ph_now:"Now", ph_lots:"Lots", ph_pick:"item price chart", ph_median:"Median price", ph_min:"Lowest price", ph_other:"Also sold for",
  sr_title:"Season rating — full", sr_intro:"Players with season points: {n} of {all}. Reward by place is a fixed scale (places 1–6). “Received” — how many times and how many boosters the player got in total.", sr_last:"Last payout", sr_find:"nick or clan", sr_clan:"Clan", sr_points:"Points", sr_gap:"Behind the place above", sr_reward:"Reward now", sr_got:"Received",
  pt_title:"Tools", pt_where:"All property", pt_where_ph:"filter by item", pt_total:"Items total", pt_place:"Where", pt_items:"What", pt_market_pending:"Terminal storage will appear in a couple of minutes — market data is being collected.",
  pt_journal:"Player journal", pt_journal_hint:"The player's events from server and panel logs in one feed. Buttons show/hide a category.", pt_journal_gm:"The player's events from server and panel logs in one feed, plus (GM) their actions in the player panel and admin panel for 30 days — the “Panel” category. Buttons show/hide a category.",
  pt_when:"When", pt_event:"Event", pt_k_tech:"Techs", pt_k_research:"Research", pt_k_booster:"Boosters", pt_k_level:"Levels", pt_k_clan:"Clan", pt_k_death:"Deaths", pt_k_reward:"Rewards", pt_k_role:"Roles", pt_k_land:"Plots", pt_k_session:"Sessions", pt_k_panel:"Panel",
  pt_hist:"Charts", pt_techs:"Techs learned", pt_hist_none:"No history yet — snapshots are hourly.", pt_hist_since:"Snapshots since",
  pt_viewas:"View as player", pt_viewas_hint:"One-time link valid for 60 seconds. Opens the player panel as this player, read-only, for 30 minutes; the login and every action are recorded in the activity log. Exit with the “Exit view” button at the top.", pt_viewas_open:"Open the player panel as",
  sg_title:"Space generation", sg_run:"Analyze", sg_intro:"What in space (star systems, clusters, newcomer start map) was created or changed after the initial world generation, and when. Three independent signals: star id order within clusters (date-independent), file dates (after a copy/restore these are copy dates) and world backup history; plus the Steam game update date. Read-only.",
  sg_world:"World", sg_gen:"World generation", sg_stars:"Star systems", sg_missing:"missing ids:", sg_clusters:"Clusters", sg_start:"Newcomer start map", sg_was:"was", sg_steam:"Steam game update", sg_backups:"World backups",
  sg_concl:"Findings", sg_late:"Systems created or changed after generation", sg_what:"What", sg_when:"When", sg_cluster:"Cluster", sg_objs:"Objects (plan./sat./aster.)", sg_planets:"Planets",
  sg_created:"created", sg_modified:"modified", sg_noname:"(no name)", sg_ooo:"out of generation order", sg_startcl:"start", sg_clch:"Clusters changed after generation", sg_startfiles:"Start map files", sg_file:"File", sg_hist:"Backup history (changes only)", sg_backup:"Backup", sg_diff:"What changed",
  sp_item:"Item", sp_k1_planet:"planet", sp_k1_satellite:"moon", sp_k1_asteroid:"asteroid",
  pz_hint:"wheel — zoom, drag — pan, double-click — reset", sp_cluster:"cluster", sp_objs:"objects", sp_owners:"owners", sp_system:"System",
  sp_find_ph:"find a system by name", sp_galaxy:"Galaxy", sp_galaxy_hint:"All star systems of the server (positions from star*.json headers, index refreshed hourly). Blue — has plots (size = how many), orange — ships only. Click to open the system below.",
  sp_leg_claims:"has plots", sp_leg_ships:"ships only", sp_leg_other:"others", sp_busiest:"Most settled systems",
  sp_k_planet:"planets", sp_k_satellite:"moons", sp_k_asteroid:"asteroids", sp_only_claimed:"only with plots", sp_stations:"stations",
  sp_st_landed:"landed:", sp_st_parked:"near:", sp_st_flight:"in flight", sp_st_open:"in open space",
  sp_claimed_objs:"Objects with plots", sp_obj:"Object", sp_type:"Type", sp_model:"Ship", sp_status:"Status",
  map_space:"Space", pd_terr_maps:"maps:", pd_terr_n:"Plots",
  role_gm:"GM", ac_src_all:"— where —", ac_src_admin:"Admin panel", ac_src_player:"Player panel", ac_src_guard:"Login guard",
  ac_ev_all:"— all events —", ac_ev_logins:"Logins/logouts", ac_ev_bad:"Failures, denials, probes", ac_ev_req:"Requests (viewed/searched)",
  ac_ev_ui:"UI actions", ac_ev_audit:"Audit (changes)", ac_ev_guard:"Blocks",
  ac_user:"who (nick/login/#id)", ac_ip:"IP", ac_text:"text (search everything)", ac_hours:"for", ac_h1:"1 hour", ac_h24:"day", ac_h168:"week", ac_hall:"all time",
  ac_find:"Search", ac_more:"More", ac_col_ts:"Time", ac_col_src:"Where", ac_col_who:"Who", ac_col_ev:"Event", ac_col_d:"Details",
  ac_none:"Nothing found", ac_size:"Log", ac_intro:"Everything done in the admin and player panels: logins and failed attempts, every request (what was opened and searched), UI actions, changes. Identical auto-refreshes are logged at most once per 2 minutes (×N = repeat count). Click a nick or IP to filter.",
  ev_login_ok:"login", ev_login_fail:"FAILED login", ev_login_blocked:"login BLOCKED", ev_logout:"logout", ev_admin_enter:"«Admin» button",
  ev_req:"request", ev_page:"opened page", ev_denied:"DENIED", ev_probe:"probe", ev_ui:"action", ev_audit:"change", ev_block:"BLOCK", ev_flood:"MASS BRUTE-FORCE",
  gb_intro:"Password brute-force protection (admin and player panels). Address: {ip} failures in {ipw} min → block. Account: {ac} failures in {acw} min from any address → login from new addresses closed (known addresses still work). Blocks escalate: {steps}. Account blocks and mass brute-force alert the super admin in Telegram.",
  gb_fails10:"Failed logins in 10 min", gb_flood:"mass brute-force in progress!", gb_key:"Key", gb_left:"Left", gb_lvl:"Level", gb_fails:"Failures in window", gb_who:"Last login", gb_unblock:"Lift", gb_none:"No blocks",
  level:"Level", lines:"lines", download:"Download", navshots_none:"No login-sequence screenshots",
  chpass_title:"Change password", chpass_note:"Default login is admin/admin. Change it now — at least 6 characters, not \"admin\".",
  chpass_old:"Current password", chpass_new:"New password", chpass_rep:"Repeat new password",
  chpass_mismatch:"Passwords do not match", change:"Change password",
  err_forbidden:"Not enough rights for this action", role_admin:"admin", role_moderator:"moderator", role_viewer:"viewer",
  us_title:"Panel users", us_intro:"Admin — everything; moderator — viewing, screenshot, game restart and login, ban/unban, violations and twinks; viewer — viewing only (no player passwords, private messages or IPs, no recipes). A new user changes the password on first login.",
  us_name:"Login", us_role:"Role", us_pw:"Temporary password (6+ chars)", us_add:"Add", us_reset:"Reset password", us_del:"Delete",
  us_me:"you", us_mc:"must change password", us_new_pw:"New temporary password for ", us_confirm_del:"Delete user ",
  err_bad_credentials:"Wrong username or password", err_throttled:"Too many attempts, wait a bit",
  err_bad_old:"Current password is wrong", err_too_short:"At least 6 characters", err_too_weak:"Password too weak",
  err_auth:"Session expired — log in again", err_net:"No connection to server",
  pl_head:"Local server players", pl_world:"World", pl_registered:"registered",
  pl_online:"online (analytics)", pl_online_gs:"online (game_state)", pl_bymap:"By map",
  pl_stale:"corrected", pl_stale_hint:"this many players were stuck \"online\" since before the last server start (no exit after a crash/restart) — the panel compared their last login to the last Server ready time and cleared them",
  pl_space_note:"map 0 = space: the game counts these players online, they may actually be offline",
  pl_only_online:"Online only", pl_search:"search by name",
  pl_col_status:"Status", pl_col_map:"Map", pl_col_pos:"Coords",
  pl_col_enter:"Enter", pl_col_exit:"Exit", pl_col_sess:"Session",
  pl_col_hours:"Hours", pl_col_lvl:"Lvl", pl_col_role:"Role", pl_col_ban:"Ban",
  pl_role_player:"player", pl_role_staff:"staff", pl_role_mod:"moderator", pl_role_admin:"admin", pl_role_gm:"GM",
  pl_recent:"Recent events",
  pl_ev_register:"registered", pl_ev_enter:"entered", pl_ev_exit:"left",
  pl_none:"No player data", pl_map:"map",
  tw_intro:"Twinks: by shared password (code), by IP and by hardware. Sensitive — behind the panel password.",
  tw_prompt:"Confirm with your panel password:", tw_min:"min accounts per IP",
  tw_show:"Show", tw_none:"No matches", tw_summary:"IPs total",
  tw_flagged:"flagged IPs", tw_connects:"conn.", tw_other_ips:"more IPs", tw_ignored:"ignored",
  tw_bycode:"by password", tw_byip:"by IP", tw_byfp:"by hardware (noisy)", tw_samepw:"shared password",
  st_online:"Online (7 days)", st_now:"now", st_peak:"7d peak", st_growth:"Growth",
  st_reg:"reg.", st_dau:"active/day", st_ret:"retention", st_toplvl:"Top by level",
  st_toptime:"Top by hours", st_clans:"Clans", st_month:"Month top", st_bans:"Ban list",
  st_staff:"Staff", st_lvldist:"Levels", st_countries:"Countries", st_hist:"role history",
  st_world:"World · maps", st_terr:"Territories", st_owner:"owner", st_avatars:"avatars", st_terrfilter:"map",
  sp_title:"Space", sp_inspace:"in space now", sp_stuck:"stuck offline", sp_units:"space units total",
  sp_ship:"has a ship (spaceUnitId)", sp_planets:"Off-world land", sp_plots:"plots", sp_owners:"owners",
  su_title:"Star system (snapshot)", su_ships:"ships", su_meteorites:"meteorites", su_pods:"space items", su_vel:"velocity (vx,vy)", su_hp:"HP",
  su_cargo:"cargo", su_moving:"moving", su_stopped:"stopped",
  st_toptech:"Popular techs", st_researching:"researching", st_tech:"tech", st_size:"size",
  st_branch:"unlocks", st_technote:"names = what the tech unlocks in crafting (from craft.json + client localization); branch/tier from the tech.json tree",
  md_open:"show map", md_title:"Map .dt", md_parsing:"parsing binary map (big one — up to ~15 s)…",
  md_blocks:"blocks", md_machines:"machines", md_ore:"ore / stone", md_containers:"in world containers",
  md_landowners:"land owners (8×8 blocks)", md_ground:"land / water", md_misc:"misc",
  md_containers_list:"Container contents", md_containers_filter:"filter by item",
  md_containers_col_where:"Where", md_containers_col_items:"Contents", md_containers_capped:"list capped by limit",
  md_offworld_hint:"off-world map (not 0/1) — planet/dungeon; name and coordinates in the star system (Data\\world\\star1.json, map id = record id, reverse-engineered — see the system map)",
  md_spacename:"space name (coord.)",
  su_starmap:"System map", su_star:"star", su_planets:"planets", su_satellites:"satellites", su_asteroids:"asteroids",
  su_hidden:"hidden (no bodies nearby):",
  su_scatter_note:"planet positions are reverse-engineered from the binary Data\\world\\star<N>.json format (no source for the world generator); coordinates are validated, but names are NOT unique across star systems",
  su_hover_hint:"hover over the map",
  su_find_ph:"planet/satellite/asteroid name (star 1)", su_find_none:"not found in this system",
  su_find_ph2:"planet/satellite/asteroid name —", su_cluster:"cluster", su_system:"system",
  su_clusters:"clusters", su_systems:"systems total",
  su_find_hits:"found", su_find_click:"show on map", su_objects:"objects",
  mf_title:"Find an item in the world", mf_ph:"id or name (e.g. tech_booster)", mf_map:"map",
  mf_all:"whole world", mf_go:"search", mf_wait:"scanning maps (whole world — up to ~2 min, cached)…",
  mf_total:"total qty", mf_spots:"spots", mf_scanned:"maps scanned", mf_where:"where",
  mf_nomatch:"nothing found", mf_matched:"name matches",
  mf_owner:"on whose land", mf_byowner:"by land owner", mf_nobody:"— unclaimed —",
  mi_title:"Map image", mi_wait:"rendering…", mi_claims:"claims", mi_owner:"owner id",
  mi_hover_hint:"hover over the map", mi_free:"free",
  mi_rot_ccw:"rotate 45° counter-clockwise", mi_rot_cw:"rotate 45° clockwise",
  mi_review:"rescan", mi_review_hint:"re-render the map, ignoring the cache",
  mi_show:"show", mi_water:"water", mi_land:"land", mi_grass:"nature", mi_mtn:"mountains",
  mi_ore:"ore", mi_wall:"walls/floor", mi_built:"structures", mi_claim:"claims = colour per owner (checkbox), or one owner by id",
  st_toptechp:"Top by tech count", st_techs:"techs", st_resh:"research h",
  st_resh_note:"= sum of researched techs' cost (tech.json cost is minutes, 1440 = a day); research runs offline too, boosters/brains speed it up",
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
  tt_title:"Tech / booster tracking", tt_none:"empty so far (the panel logs from when it was enabled)",
  tt_gained:"researched", tt_spent:"spent booster", tt_bgain:"gained booster", tt_reschg:"new research", tt_map:"changed map",
  tt_chart_title:"Research & boosters by day", tt_chart_tech:"techs learned", tt_chart_boost:"boosters spent",
  pd_inv_edit:"Edit inventory (offline only)", pd_inv_online:"player is online — editing disabled",
  pd_inv_give:"Give to stash", pd_inv_take:"Take", pd_inv_item:"item: name or id",
  pd_inv_count:"qty", pd_inv_from:"from", pd_inv_carry:"carried",
  pd_mod:"Moderation (offline)", pd_mod_ban:"Ban", pd_mod_unban:"Unban",
  pd_mod_tp:"Teleport", pd_mod_givetech:"Grant tech", pd_mod_resetpw:"Reset player password",
  pd_mod_newcode:"new player password",
  pd_p0:"Energy", pd_p1:"Hunger", pd_p2:"Health", pd_p3:"Accuracy",
  pd_p4:"Move speed", pd_p5:"Action speed", pd_p6:"Attack speed",
  pd_p7:"Genetics A", pd_p8:"Genetics B", pd_p9:"Genetics C", pd_p10:"Genetics D",
  pd_p11:"Oxygen", pd_p12:"Genetic points",
  pd_lp0:"Exp", pd_lp1:"Level", pd_lp2:"Distribution points",
  pd_skill_pfx:"Skill", pd_skill_hint:"exact name unknown to the panel — +2%/level to something",
  ago:"ago", never:"no data", n_a:"n/a",
  disk_used:"used", disk_free:"free", disk_tip:"Game world disk: {u} of {t} GB used, {f} GB free" }
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

// ---- журнал действий для GM: вкладки, клики, ввод в поиск/фильтры (пароли не шлём) ----
var TRK=[],TRKT=null,TRKTAB=null,TRKIN={},TRK_SECRET=/pass|парол|token|токен|secret|секрет|key|ключ|code|код/i;
function trkOn(){ return S.authed && !S.must_change; }
function trkName(x){ var lb=x.closest&&x.closest("label");
  return x.id||x.name||x.getAttribute("placeholder")||x.getAttribute("aria-label")||x.title||(lb? (lb.innerText||"").replace(/\s+/g," ").trim().slice(0,40):"")||x.tagName.toLowerCase(); }
function trkLabel(x){ var s=x.id||x.getAttribute("aria-label")||x.title||"", tx=(x.innerText||x.value||"").replace(/\s+/g," ").trim().slice(0,80);
  return s&&tx&&s!==tx? s+" «"+tx+"»" : (tx||s||x.tagName.toLowerCase()); }
function trk(a,d){ if(!trkOn()) return; TRK.push({a:a,tab:S.tab,d:String(d==null?"":d).slice(0,300)});
  if(TRK.length>=50) trkFlush(); else if(!TRKT) TRKT=setTimeout(trkFlush,5000); }
function trkFlush(){ clearTimeout(TRKT); TRKT=null; if(!TRK.length) return; if(!trkOn()){ TRK=[]; return; }
  var b=TRK.splice(0,60);
  try{ fetch("/api/track",{method:"POST",keepalive:true,headers:{"Content-Type":"application/json","X-CSRF-Token":S.csrf},body:JSON.stringify({ev:b})}).catch(function(){}); }catch(e){} }
function trkSecret(x){ return x.type==="password"||TRK_SECRET.test(trkName(x)); }
document.addEventListener("click",function(e){ var x=e.target&&e.target.closest&&e.target.closest("button,a,summary,th,[onclick]");
  if(x) trk("click",trkLabel(x)); },true);
document.addEventListener("change",function(e){ var x=e.target; if(!x||trkSecret(x)) return;
  if(x.tagName==="SELECT") trk("change",trkName(x)+" = "+(x.selectedIndex>=0&&x.options[x.selectedIndex]? x.options[x.selectedIndex].text : x.value));
  else if(x.type==="checkbox"||x.type==="radio") trk("change",trkName(x)+" = "+(x.checked?"вкл":"выкл"));
  else if(x.type==="file") trk("change",trkName(x)+" = файл "+((x.files&&x.files[0]&&x.files[0].name)||"")); },true);
document.addEventListener("input",function(e){ var x=e.target;
  if(!x||trkSecret(x)||x.tagName==="SELECT"||x.type==="checkbox"||x.type==="radio"||x.type==="file") return;
  var k=trkName(x); clearTimeout(TRKIN[k]); TRKIN[k]=setTimeout(function(){ trk("input",k+" = "+x.value); },1200); },true);
document.addEventListener("visibilitychange",function(){ if(document.hidden) trkFlush(); });
window.addEventListener("pagehide",trkFlush);
function trkTab(){ if(trkOn() && S.tab!==TRKTAB){ TRKTAB=S.tab; trk("tab",S.tab); } }
function errText(e){ if(!e) return t("err_net"); if(e.err==="net") return t("err_net");
  var k="err_"+(e.error||e.err||""); return T[S.lang][k]||e.detail||e.error||t("err_net"); }

// ---- password confirmation modal (чувствительные действия) ----
// Сервер сам решает, нужен ли пароль (сессия недавно подтверждена — см.
// REAUTH_ELEVATE_SECONDS на бэке): сначала пробуем без пароля, и только если
// сервер ответит password_required/bad_password — показываем диалог.
function openPasswordModal(onSubmit){
  var pw=el("input",{type:"password",placeholder:t("pass"),autocomplete:"current-password"});
  var errEl=el("div",{class:"msg err",style:"display:none;margin-top:8px"},[]);
  var busy=false;
  function close(){ if(ovl.parentNode) ovl.parentNode.removeChild(ovl); document.removeEventListener("keydown",onKey); }
  function onKey(e){ if(e.key==="Escape") close(); }
  function submit(){
    if(busy || !pw.value) return;
    busy=true; errEl.style.display="none";
    Promise.resolve(onSubmit(pw.value)).then(function(){ close(); })
      .catch(function(e){
        busy=false;
        errEl.style.display=""; errEl.textContent=(e&&e.error==="bad_password")? t("pd_code_bad") : errText(e);
        pw.value=""; pw.focus();
      });
  }
  pw.addEventListener("keydown",function(e){ if(e.key==="Enter") submit(); });
  var ovl=el("div",{class:"ovl",onclick:function(e){ if(e.target===ovl) close(); }},[
    el("div",{class:"modal",style:"max-width:320px"},[
      el("h3",{},[t("pd_code_prompt")]),
      el("div",{class:"row"},[pw, el("button",{class:"small pri",onclick:submit},[t("pd_code_btn")])]),
      errEl,
    ]),
  ]);
  document.body.appendChild(ovl);
  setTimeout(function(){ pw.focus(); },0);
}
// JSON-запрос через api(), защищённый паролем панели.
function gatedApi(url, extra, onOk, onErr){
  function attempt(pw){
    var body=Object.assign({}, extra||{}); if(pw) body.password=pw;
    return api(url,{body:body});
  }
  attempt(null).then(onOk).catch(function(e){
    if(e && (e.error==="password_required" || e.error==="bad_password")){
      openPasswordModal(function(pw){ return attempt(pw).then(onOk); });
    } else if(onErr) onErr(e);
  });
}
// Как gatedApi, но для скачивания файла (world-backup): onOk получает "сырой" Response.
function gatedFetchBlob(url, extra, onOk, onErr){
  function attempt(pw){
    var body=Object.assign({}, extra||{}); if(pw) body.password=pw;
    return fetch(url,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":S.csrf},body:JSON.stringify(body)})
      .then(function(r){ if(r.ok) return onOk(r); return r.json().then(function(j){ throw j; }); });
  }
  attempt(null).catch(function(e){
    if(e && (e.error==="password_required" || e.error==="bad_password")){
      openPasswordModal(function(pw){ return attempt(pw); });
    } else if(onErr) onErr(e);
  });
}

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
  clearInterval(dashTimer); clearInterval(logTimer); clearInterval(plTimer); clearInterval(chTimer); clearInterval(ldTimer);
  var app=$("#app"); app.innerHTML="";
  if(!S.authed){ app.appendChild(viewLogin()); return; }
  if(S.must_change){ app.appendChild(viewChpass()); return; }
  app.appendChild(shell());
  routeTab();
  loadMapNames();
  trkTab();
}
// место на диске мира — в шапке рядом с языком; меньше 10% свободно — красным
var DISK_TIMER=null;
function diskBadge(){
  var sp=el("span",{id:"disk",class:"small",style:"white-space:nowrap"},[]);
  function fill(d){
    var e=$("#disk")||sp; e.innerHTML="";
    if(!d||!d.ok||d.free_pct==null) return;
    var bad=d.free_pct<10;
    e.style.color=bad?"var(--err)":"var(--ok)";
    e.title=t("disk_tip").replace("{u}",d.used_gb).replace("{t}",d.total_gb).replace("{f}",d.free_gb);
    e.appendChild(document.createTextNode("💾 "+d.drive+" "+t("disk_used")+" "+Math.round(d.used_pct)+"% · "+t("disk_free")+" "+Math.round(d.free_pct)+"%"));
  }
  function load(){ api("/api/disk").then(function(d){ S.disk=d; fill(d); }).catch(function(){}); }
  if(S.disk) fill(S.disk);
  if(!DISK_TIMER){ load(); DISK_TIMER=setInterval(function(){ if(S.authed) load(); },60000); }
  return sp;
}
function header(){
  var langBtn=el("button",{class:"small",onclick:function(){ S.lang=S.lang==="ru"?"en":"ru"; localStorage.setItem("sw_lang",S.lang); render(); }},[S.lang==="ru"?"EN":"RU"]);
  var thBtn=el("button",{class:"small",title:"theme",onclick:toggleTheme},["◐"]);
  var out=[ el("span",{id:"conn",class:"dot "+(S.conn===false?"err":(S.conn?"ok":""))}),
            el("h1",{},[el("img",{id:"hlogo",src:"/favicon.ico?v="+(S.favV||0),alt:"",onerror:function(){ this.remove(); }}), S.title||t("title")]), el("span",{class:"sp"}),
            el("span",{class:"muted small"},[(S.user||"")+(S.role&&S.role!=="admin"? " · "+t("role_"+S.role) : "")]), diskBadge(), langBtn, thBtn,
            el("button",{class:"small",onclick:doLogout},[t("logout")]) ];
  return el("header",{},out);
}
// вкладки по ролям (сервер всё равно проверяет каждый запрос — это только чтобы не показывать лишнее)
var ROLE_TABS={
  viewer:["dash","srv","load","chat","stats","map","players","activity","leaders","clans","fleet","trade","economy","craft"],
  moderator:["dash","act","srv","load","chat","stats","map","players","activity","leaders","twinks","suspicious","clans","fleet","trade","economy","craft"]};
function isAdmin(){ var r=S.role||"admin"; return r==="admin"||r==="gm"; }
function isGM(){ return S.role==="gm"; }
function shell(){
  var tabs=["dash","act","srv","load","chat","stats","map","players","activity","leaders","twinks","suspicious","clans","fleet","trade","economy","entry","buffs","food","craft","admin","roles","logs"];
  if(!isAdmin()){ var allow=ROLE_TABS[S.role]||ROLE_TABS.viewer; tabs=tabs.filter(function(x){ return allow.indexOf(x)>=0; });
    if(tabs.indexOf(S.tab)<0) S.tab=tabs[0]; }
  var nav=el("nav",{}, tabs.map(function(id){
    return el("button",{class:S.tab===id?"active":"",onclick:function(){ S.tab=id; localStorage.setItem("sw_tab",id); render(); }},[t(id)]);
  }));
  return el("div",{},[ header(), nav, el("main",{id:"view"},[]) ]);
}
// справочник названий карт (id карты = id объекта космоса) — один раз после входа
var MAPN=null;
function loadMapNames(){ if(MAPN!==null || !S.authed) return; MAPN={};
  api("/api/map-names").then(function(d){ MAPN=(d&&d.maps)||{}; routeTab(); }).catch(function(){}); }
function mapName(id){ if(id==null||id==="") return "—"; if(+id===0) return t("map_space");
  var m=MAPN&&MAPN[String(id)]; return m? m.name+" #"+id : "#"+id; }
function mapFull(id){ if(id==null||id==="") return "—"; if(+id===0) return t("map_space");
  var m=MAPN&&MAPN[String(id)]; return m? m.name+" ("+m.kind+", "+m.star_name+") #"+id : "#"+id; }
function routeTab(){ var v=$("#view"); v.innerHTML="";
  ({dash:tabDash,act:tabAct,srv:tabSrv,load:tabLoad,chat:tabChat,stats:tabStats,map:tabMap,players:tabPlayers,twinks:tabTwinks,entry:tabEntry,buffs:tabBuffs,food:tabFood,clans:tabClans,craft:tabCraft,trade:tabTrade,economy:tabEconomy,suspicious:tabSuspicious,activity:tabActivity,leaders:tabLeaders,admin:tabAdmin,fleet:tabFleet,roles:tabSettings,logs:tabLogs}[S.tab]||tabDash)(v); }
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
    S.authed=true; S.user=j.username; S.csrf=j.csrf; S.must_change=!!j.must_change; S.role=j.role||"admin"; render();
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
  if(!isAdmin()) defs=defs.filter(function(d){ return d[0]==="restartgame"||d[0]==="login"; });
  var grid=el("div",{class:"actions"}, defs.map(function(d){
    var cls=d[2]?"danger":""; if(d[0]==="restartgame"||d[0]==="login") cls="pri";
    return el("button",{class:cls,onclick:function(){ runAction(d[0], d[3]||{}, d[2], t(d[1])); }},[t(d[1])]);
  }));
  var out=el("div",{id:"actout"},[]);
  var expMsg=el("span",{class:"muted small"},[]);
  var exp=el("div",{class:"card",style:"margin-top:16px"},[
    el("h3",{},[t("ex_title")]),
    el("div",{class:"row",style:"flex-wrap:wrap;gap:8px"},[
      el("button",{class:"small",onclick:function(){ window.open("/api/players-csv","_blank"); }},[t("ex_csv")]),
      el("button",{class:"small",onclick:function(){ backupDownload("state",expMsg); }},[t("ex_bstate")]),
      el("button",{class:"small danger",onclick:function(){ if(window.confirm(t("ex_bfull")+"?")) backupDownload("full",expMsg); }},[t("ex_bfull")]),
      expMsg
    ])
  ]);
  v.appendChild(el("div",{},[grid, out, isAdmin()? exp : null]));
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

// ---- entry (login-flow tuner) ----
function tabEntry(v){
  var LF=null, PICK=null;
  var img=el("img",{style:"max-width:100%;border:1px solid var(--bd);cursor:crosshair;display:block;background:#0002"},[]);
  var pickInfo=el("div",{class:"muted small"},[t("entry_pick_hint")]);
  var out=el("div",{},[]);
  var stepsBox=el("div",{},[]);

  img.addEventListener("click", function(e){
    var r=img.getBoundingClientRect();
    if(!r.width||!r.height) return;
    PICK={xp:(e.clientX-r.left)/r.width*100, yp:(e.clientY-r.top)/r.height*100};
    pickInfo.textContent=t("entry_pick")+": xp="+PICK.xp.toFixed(2)+"  yp="+PICK.yp.toFixed(2);
  });
  function refreshShot(){ img.src="/api/nav-shot?name=latest.png&_="+Date.now(); }
  function act(op, extra){
    var m=el("div",{class:"msg info"},[t("working")]);
    out.innerHTML=""; out.appendChild(m);
    api("/api/action",{body:Object.assign({op:op},extra||{})}).then(function(j){
      pollJob(j.job, m); setTimeout(refreshShot, 1200);
    }).catch(function(e){ m.className="msg err"; m.textContent=errText(e); });
  }
  function stepRow(list, key, i, s){
    function fld(prop,ph,w){
      var inp=el("input",{value:s[prop]!=null?s[prop]:"",placeholder:ph||"",style:"width:"+(w||58)+"px"},[]);
      inp.addEventListener("change",function(){
        var v=inp.value;
        s[prop]=(prop==="xp"||prop==="yp"||prop==="wait")? (v===""?undefined:parseFloat(v)) : v;
      });
      return inp;
    }
    var actSel=el("select",{},["click","ensure_check","type","key"].map(function(a){
      return el("option",{value:a,selected:s.action===a?"selected":null},[a]); }));
    actSel.addEventListener("change",function(){ s.action=actSel.value; drawSteps(); });
    var extraFld = (s.action==="type") ? fld("text","{account_user}",110)
                  : (s.action==="key") ? fld("key","enter",70)
                  : fld("tag","tag",70);
    var btn=el("select",{},["left","right"].map(function(b){
      return el("option",{value:b,selected:(s.button||"left")===b?"selected":null},[b]); }));
    btn.addEventListener("change",function(){ s.button=btn.value; });
    var dbl=el("input",{type:"checkbox",checked:s.dbl?"checked":null},[]);
    dbl.addEventListener("change",function(){ s.dbl=dbl.checked; });
    var showXY = s.action==="click" || s.action==="ensure_check";
    var del=el("button",{class:"small danger",onclick:function(){ list.splice(i,1); drawSteps(); }},["×"]);
    var pickBtn=el("button",{class:"small",title:t("entry_use_pick"),onclick:function(){
      if(!PICK) return; s.xp=+PICK.xp.toFixed(2); s.yp=+PICK.yp.toFixed(2); drawSteps(); }},["◎"]);
    return el("tr",{},[
      el("td",{},[String(i+1)]), el("td",{},[actSel]),
      el("td",{},[showXY? fld("xp",null,50):null]), el("td",{},[showXY? fld("yp",null,50):null]),
      el("td",{},[showXY? pickBtn:null]),
      el("td",{},[extraFld]), el("td",{},[fld("wait","1.5",45)]),
      el("td",{},[showXY? btn:null]), el("td",{},[showXY? dbl:null]), el("td",{},[del]),
    ]);
  }
  function stepGroup(key, title){
    LF[key]=LF[key]||[];
    var list=LF[key];
    var box=el("div",{class:"card"},[el("h3",{},[title+" · "+list.length])]);
    var tb=el("table",{},[el("tr",{},["#","action","xp","yp","","text/key","wait","btn","2×",""].map(function(x){
      return el("th",{},[x]); }))]);
    list.forEach(function(s,i){ tb.appendChild(stepRow(list, key, i, s)); });
    box.appendChild(tb);
    box.appendChild(el("div",{class:"row",style:"margin-top:6px"},[
      el("button",{class:"small",onclick:function(){
        var s={action:"click", wait:1.5, tag:key};
        if(PICK){ s.xp=+PICK.xp.toFixed(2); s.yp=+PICK.yp.toFixed(2); }
        list.push(s); drawSteps();
      }},[t("entry_add_step")]),
    ]));
    return box;
  }
  function drawSteps(){
    stepsBox.innerHTML="";
    stepsBox.appendChild(stepGroup("menu_steps", "menu_steps"));
    stepsBox.appendChild(stepGroup("account_steps", "account_steps"));
    stepsBox.appendChild(stepGroup("after_ingame", "after_ingame"));
    LF.ok=LF.ok||{};
    var okxp=el("input",{value:LF.ok.xp!=null?LF.ok.xp:"",style:"width:60px"},[]);
    var okyp=el("input",{value:LF.ok.yp!=null?LF.ok.yp:"",style:"width:60px"},[]);
    okxp.addEventListener("change",function(){ LF.ok.xp=parseFloat(okxp.value); });
    okyp.addEventListener("change",function(){ LF.ok.yp=parseFloat(okyp.value); });
    stepsBox.appendChild(el("div",{class:"card"},[el("h3",{},["ok"]),
      el("div",{class:"row"},[el("span",{},["xp"]),okxp,el("span",{},["yp"]),okyp,
        el("button",{class:"small",onclick:function(){
          if(!PICK) return; LF.ok={xp:+PICK.xp.toFixed(2),yp:+PICK.yp.toFixed(2)}; drawSteps(); }},[t("entry_use_pick")])
      ])]));
    stepsBox.appendChild(el("div",{class:"row",style:"margin-top:10px"},[
      el("button",{class:"small",onclick:save},[t("save")])]));
  }
  function save(){
    var m=el("div",{class:"msg info"},[t("working")]);
    out.innerHTML=""; out.appendChild(m);
    api("/api/login-flow",{body:{login_flow:LF}}).then(function(){
      m.className="msg ok"; m.textContent="✅ "+t("entry_saved");
    }).catch(function(e){ m.className="msg err"; m.textContent=errText(e); });
  }
  v.appendChild(el("div",{},[
    el("p",{class:"muted"},[t("entry_intro")]),
    el("div",{class:"row",style:"flex-wrap:wrap;gap:8px;margin-bottom:10px"},[
      el("button",{class:"small",onclick:function(){ act("navshot"); }},[t("entry_shot")]),
      el("button",{class:"small",onclick:function(){ act("navstate"); }},[t("entry_state")]),
      el("button",{class:"small",onclick:function(){ if(PICK) act("navclickp",{xp:PICK.xp,yp:PICK.yp}); }},[t("entry_testclick")]),
      el("button",{class:"small danger",onclick:function(){ if(window.confirm(t("entry_seq_confirm"))) runAction("login",{},0,t("entry_seq")); }},[t("entry_seq")]),
    ]),
    out,
    el("div",{},[
      el("div",{style:"max-width:720px;margin-bottom:14px"},[img, pickInfo]),
      stepsBox,
    ]),
  ]));
  api("/api/login-flow").then(function(j){ LF=j.login_flow||{}; drawSteps(); })
    .catch(function(e){ out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  refreshShot();
}

// ---- buff notepad (мешаем микстуры) ----
// enum ZData.BuffType (Il2CppDumper, 2026-09-17, GameAssembly.dll) — id'ы отличаются
// от статов игрока (UnitParamType) несмотря на пересечение диапазона 0-9.
var BUFF_TYPE_KEY={0:"bn_bt0",1:"bn_bt1",2:"bn_bt2",3:"bn_bt3",4:"bn_bt4",
                    5:"bn_bt5",6:"bn_bt6",7:"bn_bt7",8:"bn_bt8",9:"bn_bt9"};
function buffTypeName(state){ var k=BUFF_TYPE_KEY[state]; return k? t(k) : ("#"+state); }
function tabBuffs(v){
  var msg=el("span",{class:"muted small"},[]);
  var body=el("div",{},[el("p",{class:"muted"},["…"])]);
  var activeFilter=null;
  function load(){
    api("/api/buff-notepad").then(render).catch(function(e){
      body.innerHTML=""; body.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  function chipStyle(on){ return "cursor:pointer"+(on?";background:var(--acc);color:#fff;border-color:var(--acc)":""); }
  function render(d){
    body.innerHTML="";
    if(!d.ok){ body.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
    if(!d.count){ body.appendChild(el("div",{class:"muted"},[t("bn_none")])); return; }
    body.appendChild(el("div",{class:"muted small",style:"margin-bottom:8px"},[
      d.count+" "+t("bn_count")+(d.saved_at? " · "+t("bn_saved_at")+" "+d.saved_at:"")+(d.source==="server"? " · "+t("bn_src_server") : "")]));
    var chips=el("div",{class:"chips",style:"margin-bottom:10px"},[
      el("span",{class:"chip",style:chipStyle(activeFilter===null),onclick:function(){ activeFilter=null; render(d); }},[t("bn_all")+" ("+d.count+")"])
    ]);
    d.by_item.forEach(function(it){
      chips.appendChild(el("span",{class:"chip",style:chipStyle(activeFilter===it.id),
        onclick:function(){ activeFilter=(activeFilter===it.id)?null:it.id; render(d); }},[it.name+" ×"+it.count]));
    });
    body.appendChild(chips);
    var recs=d.records.filter(function(r){ return activeFilter===null || r.items.some(function(it){ return it.id===activeFilter; }); });
    body.appendChild(el("h3",{},[t("bn_records")+" · "+recs.length]));
    var tb=el("table",{},[el("tr",{},["#",t("bn_time"),t("bn_ingredients"),t("bn_effect")].map(function(x){ return el("th",{},[x]); }))]);
    recs.forEach(function(r){
      var ingrCell=el("td",{},[el("div",{class:"chips"}, r.items.map(function(it){
        return el("span",{class:"chip",style:it.id===activeFilter?"border-color:var(--acc);color:var(--acc)":""},[it.name]); }))]);
      var buffCell = r.buff.length
        ? el("div",{}, r.buff.map(function(b){ return el("div",{},[b.name+": "+(b.val>0?"+":"")+b.val]); }))
        : el("span",{class:"muted small"},[t("bn_no_effect")]);
      tb.appendChild(el("tr",{},[el("td",{},[String(r.idx+1)]), el("td",{},[r.time!=null?String(r.time):"—"]), ingrCell, buffCell]));
    });
    body.appendChild(tb);
  }
  load();

  // ---- оптимизатор рецептов (полный перебор по проверенной формуле) ----
  var optIngr = {};
  var optChips = el("div",{class:"chips",style:"margin:8px 0"},[el("span",{class:"muted small"},["…"])]);
  var optTarget = el("select",{}, Object.keys(BUFF_TYPE_KEY).map(function(k){
    return el("option",{value:k},[t(BUFF_TYPE_KEY[k])]);
  }));
  var optMsg = el("span",{class:"muted small"},[]);
  var optResults = el("div",{},[]);
  function loadIngredients(){
    api("/api/buff-ingredients").then(function(d){
      optChips.innerHTML="";
      if(!d.ok || !d.ingredients.length){ optChips.appendChild(el("div",{class:"muted small"},[d.error||t("bn_none")])); return; }
      d.ingredients.forEach(function(it){
        var cb=el("input",{type:"checkbox",checked:true},[]);
        optIngr[it.id]=cb;
        optChips.appendChild(el("label",{style:"display:inline-flex;align-items:center;gap:4px;border:1px solid var(--line);border-radius:20px;padding:2px 8px;font-size:11.5px;cursor:pointer"},[cb, it.name]));
      });
    }).catch(function(e){ optChips.innerHTML=""; optChips.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  function pollOpt(jid){
    var iv=setInterval(function(){
      api("/api/job?id="+jid).then(function(j){
        if(!j.done) return;
        clearInterval(iv);
        optMsg.textContent="";
        var d=j.result||{};
        optResults.innerHTML="";
        if(!d.ok){ optResults.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
        optResults.appendChild(el("div",{class:"muted small",style:"margin-bottom:8px"},[
          d.checked+" "+t("bn_opt_checked")+" · "+d.count+" "+t("bn_opt_found")]));
        if(!d.results.length){ optResults.appendChild(el("div",{class:"muted"},[t("bn_opt_none")])); return; }
        d.results.forEach(function(res,i){
          var ingr=res.materials.map(function(m){ return m.name; }).join(" + ");
          var buffs=res.buffs.map(function(b){ return b.name+": "+(b.val>0?"+":"")+b.val; }).join(", ");
          optResults.appendChild(el("div",{class:"card",style:"margin-bottom:6px"},[
            el("b",{},["#"+(i+1)+"  "+ingr]),
            el("div",{class:"small",style:"margin-top:4px"},[buffs])
          ]));
        });
      }).catch(function(){ clearInterval(iv); optMsg.textContent=t("err_net"); });
    },1200);
  }
  function runOptimize(){
    var available=Object.keys(optIngr).filter(function(k){ return optIngr[k].checked; });
    if(available.length<4){ optMsg.textContent=t("bn_opt_none"); return; }
    optMsg.textContent=t("bn_opt_working");
    optResults.innerHTML="";
    api("/api/buff-optimize",{body:{available:available,target:parseInt(optTarget.value,10),top:5}})
      .then(function(r){ pollOpt(r.job); })
      .catch(function(e){ optMsg.textContent=errText(e); });
  }
  loadIngredients();

  v.appendChild(el("div",{},[
    el("div",{class:"card",style:"margin-bottom:12px"},[
      el("h3",{},[t("bn_upload")]),
      el("p",{class:"muted small"},[t("bn_intro")]), msg,
    ]),
    el("div",{class:"card",style:"margin-bottom:12px"},[
      el("h3",{},[t("bn_opt_title")]),
      el("p",{class:"muted small"},[t("bn_opt_intro")]),
      optChips,
      el("div",{class:"row",style:"gap:8px;flex-wrap:wrap;align-items:center"},[
        el("span",{class:"small"},[t("bn_opt_target")]), optTarget,
        el("button",{class:"small pri",onclick:runOptimize},[t("bn_opt_go")]),
        optMsg
      ]),
      el("div",{style:"margin-top:10px"},[optResults])
    ]),
    body,
    el("div",{class:"muted small",style:"margin-top:8px"},[t("bn_state_note")])
  ]));
}

// ---- кулинария (ProductLib.GetProductLibItem, разобрано Ghidra 2026-09-24) ----
var FOOD_GENES=["A","B","C","D"];
function foodGenesChips(genes){
  if(!genes || !genes.length) return el("span",{class:"muted small"},[t("fd_no_genes")]);
  return el("span",{class:"chips",style:"display:inline-flex"}, genes.map(function(g){
    return el("span",{class:"chip",style:genes.length===4?"border-color:var(--acc);color:var(--acc)":""},[g]); }));
}
function foodSlots(items){
  // слоты 0 1 / 2 3 — баланс считается по строкам и столбцам этой сетки
  var cell=function(i){ return el("div",{style:"border:1px solid var(--line);border-radius:6px;padding:3px 8px;font-size:12px;text-align:center"},
    [el("span",{class:"muted small"},[(i+1)+". "]), items[i].name]); };
  return el("div",{style:"display:inline-grid;grid-template-columns:1fr 1fr;gap:4px;min-width:240px"},[cell(0),cell(1),cell(2),cell(3)]);
}
function foodDishCard(res, idx){
  var info=[el("b",{},[t("fd_eat")+": "+res.eat]), " · ", t("fd_genes")+": ", foodGenesChips(res.genes)];
  if(res.genes.length) info.push(el("span",{class:"small"},[" · +"+res.gene_gain+" "+t("fd_gene_gain")]));
  if(res.per_point) info.push(el("span",{class:"small"},[" · "+res.per_point+" "+t("fd_per_point")]));
  return el("div",{class:"card",style:"margin-bottom:6px"},[
    el("div",{style:"margin-bottom:6px"},[idx!=null? el("b",{},["#"+(idx+1)+"  "]) : "", el("span",{},info)]),
    foodSlots(res.items)
  ]);
}
function tabFood(v){
  // ---- оптимизатор ----
  var optIngr={};
  var optChips=el("div",{class:"chips",style:"margin:8px 0"},[el("span",{class:"muted small"},["…"])]);
  var geneCbs={};
  var geneRow=el("span",{style:"display:inline-flex;gap:8px;align-items:center"}, FOOD_GENES.map(function(g){
    var cb=el("input",{type:"checkbox"},[]); geneCbs[g]=cb;
    return el("label",{style:"display:inline-flex;gap:3px;align-items:center;cursor:pointer"},[cb,g]); }));
  var maxEat=el("input",{type:"number",min:"0",step:"any",placeholder:t("fd_max_eat_ph"),style:"width:90px"});
  var optMsg=el("span",{class:"muted small"},[]);
  var optResults=el("div",{},[]);
  function setAll(on){ Object.keys(optIngr).forEach(function(k){ optIngr[k].checked=on; }); }
  api("/api/food-ingredients").then(function(d){
    optChips.innerHTML="";
    if(!d.ok || !d.ingredients.length){ optChips.appendChild(el("div",{class:"muted small"},[d.error||t("bn_none")])); return; }
    d.ingredients.forEach(function(it){
      var cb=el("input",{type:"checkbox",checked:true},[]);
      optIngr[it.id]=cb;
      optChips.appendChild(el("label",{title:t("fd_genes")+": "+(it.genes.join("")||"—"),
        style:"display:inline-flex;align-items:center;gap:4px;border:1px solid var(--line);border-radius:20px;padding:2px 8px;font-size:11.5px;cursor:pointer"},
        [cb, it.name, el("span",{class:"muted",style:"font-size:10.5px"},[it.genes.join("")||"·"])]));
    });
  }).catch(function(e){ optChips.innerHTML=""; optChips.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  function pollOpt(jid){
    var iv=setInterval(function(){
      api("/api/job?id="+jid).then(function(j){
        if(!j.done) return;
        clearInterval(iv);
        optMsg.textContent="";
        var d=j.result||{};
        optResults.innerHTML="";
        if(!d.ok){ optResults.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
        optResults.appendChild(el("div",{class:"muted small",style:"margin-bottom:8px"},[
          d.checked+" "+t("bn_opt_checked")+" · "+d.count+" "+t("fd_opt_found")]));
        if(!d.results.length){ optResults.appendChild(el("div",{class:"muted"},[t("fd_opt_none")])); return; }
        optResults.appendChild(el("div",{class:"muted small",style:"margin-bottom:6px"},[t("fd_slots")]));
        d.results.forEach(function(res,i){ optResults.appendChild(foodDishCard(res,i)); });
      }).catch(function(){ clearInterval(iv); optMsg.textContent=t("err_net"); });
    },1000);
  }
  function runOptimize(){
    var available=Object.keys(optIngr).filter(function(k){ return optIngr[k].checked; }).map(Number);
    if(available.length<4){ optMsg.textContent=t("fd_opt_none"); return; }
    var genes=FOOD_GENES.filter(function(g){ return geneCbs[g].checked; });
    optMsg.textContent=t("bn_opt_working");
    optResults.innerHTML="";
    api("/api/food-optimize",{body:{available:available,genes:genes,
      max_eat:maxEat.value===""?null:parseFloat(maxEat.value),top:10}})
      .then(function(r){ pollOpt(r.job); })
      .catch(function(e){ optMsg.textContent=errText(e); });
  }

  // ---- блюда сервера ----
  var libBody=el("div",{},[el("p",{class:"muted"},["…"])]);
  var onlyEat=el("input",{type:"checkbox",checked:true},[]);
  var sortEat=el("input",{type:"checkbox",checked:true},[]);
  var activeFilter=null, LIB=null, LIMIT=200;
  function chipStyle(on){ return "cursor:pointer"+(on?";background:var(--acc);color:#fff;border-color:var(--acc)":""); }
  function renderLib(){
    var d=LIB; libBody.innerHTML="";
    if(!d.ok){ libBody.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
    var chips=el("div",{class:"chips",style:"margin-bottom:10px"},[
      el("span",{class:"chip",style:chipStyle(activeFilter===null),onclick:function(){ activeFilter=null; renderLib(); }},[t("bn_all")+" ("+d.count+")"])]);
    d.by_item.forEach(function(it){
      chips.appendChild(el("span",{class:"chip",style:chipStyle(activeFilter===it.id),
        onclick:function(){ activeFilter=(activeFilter===it.id)?null:it.id; renderLib(); }},[it.name+" ×"+it.count]));
    });
    libBody.appendChild(chips);
    var rows=d.dishes.filter(function(r){
      return (!onlyEat.checked || r.eat>0) && (activeFilter===null || r.items.some(function(it){ return it.id===activeFilter; })); });
    if(sortEat.checked) rows=rows.slice().sort(function(a,b){ return b.eat-a.eat || b.genes.length-a.genes.length; });
    libBody.appendChild(el("div",{class:"muted small",style:"margin-bottom:6px"},[
      t("fd_shown")+" "+Math.min(rows.length,LIMIT)+" / "+rows.length]));
    var tb=el("table",{},[el("tr",{},[t("bn_ingredients"),t("fd_eat"),t("fd_genes")].map(function(x){ return el("th",{},[x]); }))]);
    rows.slice(0,LIMIT).forEach(function(r){
      tb.appendChild(el("tr",{},[
        el("td",{},[el("div",{class:"chips"}, r.items.map(function(it,i){
          return el("span",{class:"chip",style:it.id===activeFilter?"border-color:var(--acc);color:var(--acc)":""},[(i+1)+". "+it.name]); }))]),
        el("td",{},[r.eat>0? String(r.eat) : el("span",{class:"muted"},["0"])]),
        el("td",{},[foodGenesChips(r.genes), r.per_point? el("span",{class:"muted small"},[" ("+r.per_point+")"]) : ""])
      ]));
    });
    libBody.appendChild(tb);
  }
  onlyEat.addEventListener("change",function(){ if(LIB) renderLib(); });
  sortEat.addEventListener("change",function(){ if(LIB) renderLib(); });
  api("/api/food-lib").then(function(d){ LIB=d; renderLib(); }).catch(function(e){
    libBody.innerHTML=""; libBody.appendChild(el("div",{class:"msg err"},[errText(e)])); });

  v.appendChild(el("div",{},[
    el("div",{class:"card",style:"margin-bottom:12px"},[
      el("h3",{},[t("fd_opt_title")]),
      el("p",{class:"muted small"},[t("fd_opt_intro")]),
      el("div",{class:"row",style:"gap:6px"},[
        el("button",{class:"small",onclick:function(){ setAll(true); }},[t("fd_all_on")]),
        el("button",{class:"small",onclick:function(){ setAll(false); }},[t("fd_all_off")])
      ]),
      optChips,
      el("div",{class:"row",style:"gap:10px;flex-wrap:wrap;align-items:center"},[
        el("span",{class:"small"},[t("fd_need_genes")]), geneRow,
        el("span",{class:"small"},[t("fd_max_eat")]), maxEat,
        el("button",{class:"small pri",onclick:runOptimize},[t("bn_opt_go")]),
        optMsg
      ]),
      el("div",{style:"margin-top:10px"},[optResults])
    ]),
    el("div",{class:"card",style:"margin-bottom:12px"},[
      el("h3",{},[t("fd_lib")]),
      el("p",{class:"muted small"},[t("fd_lib_intro")]),
      el("div",{class:"row",style:"gap:14px;flex-wrap:wrap;margin-bottom:8px"},[
        el("label",{style:"display:inline-flex;gap:4px;align-items:center"},[onlyEat,t("fd_only_eat")]),
        el("label",{style:"display:inline-flex;gap:4px;align-items:center"},[sortEat,t("fd_sort_eat")])
      ]),
      libBody
    ]),
    el("div",{class:"muted small",style:"margin-top:8px"},[t("fd_note")])
  ]));
}

// ---- схема изучения (дерево tech.json) ----
// Раскладка «метро»: первая ветка продолжает строку, остальные дети — новые
// строки под родителем. Цвет — статус (изучено/изучается/доступно/закрыто)
// или, в режиме coverage, доля участников клана, знающих технологию.
var TECH_TREE=null;
function loadTechTree(){
  return TECH_TREE? Promise.resolve(TECH_TREE)
    : api("/api/tech-tree").then(function(d){ if(d.ok) TECH_TREE=d; return d; });
}
function svgEl(tag,attrs,kids){ var e=document.createElementNS("http://www.w3.org/2000/svg",tag);
  for(var k in (attrs||{})) if(attrs[k]!=null) e.setAttribute(k,attrs[k]);
  (kids||[]).forEach(function(c){ if(c==null) return; e.appendChild(typeof c==="string"?document.createTextNode(c):c); });
  return e; }
function techScheme(nodes, opt){
  opt=opt||{};
  var list=nodes.filter(opt.filter||function(){ return true; });
  var byId={}, kids={}, roots=[];
  list.forEach(function(n){ byId[n.id]=n; });
  list.forEach(function(n){
    if(n.parent && byId[n.parent]) (kids[n.parent]=kids[n.parent]||[]).push(n.id); else roots.push(n.id); });
  var pos={}, rowStart=[], nRows=0, maxX=0;
  function place(id,x,row){
    pos[id]={x:x,row:row}; if(x>maxX) maxX=x;
    (kids[id]||[]).forEach(function(c,i){
      if(i===0) place(c,x+1,row);
      else { var r=nRows++; rowStart[r]=c; place(c,x+1,r); } });
  }
  roots.forEach(function(id){ var r=nRows++; rowStart[r]=id; place(id,0,r); });
  var done=opt.done||{}, cov=opt.coverage, total=opt.total||1;
  var LBL=150, P=21, C=15, W=LBL+(maxX+1)*P+8, H=nRows*P+6;
  function cx(id){ return LBL+pos[id].x*P; } function cy(id){ return 3+pos[id].row*P; }
  var svg=svgEl("svg",{width:W,height:H,viewBox:"0 0 "+W+" "+H,style:"display:block;font-family:inherit"});
  var prevFam=null;
  rowStart.forEach(function(id,r){
    var n=byId[id], fam=n.family||"";
    svg.appendChild(svgEl("text",{x:LBL-8,y:3+r*P+C-3,"text-anchor":"end","font-size":"11",
      fill:fam===prevFam?"var(--line)":"var(--mut)"},[fam===prevFam?"↳":fam]));
    prevFam=fam;
  });
  list.forEach(function(n){
    if(!n.parent || !pos[n.parent]) return;
    var px=cx(n.parent)+C/2, py=cy(n.parent)+C/2, x=cx(n.id), y=cy(n.id)+C/2;
    var d= (pos[n.parent].row===pos[n.id].row) ? ("M"+(px+C/2)+" "+py+" H"+x)
      : ("M"+px+" "+(py+C/2)+" V"+y+" H"+x);
    var lit = cov? (cov[n.id]>0) : !!done[n.parent];
    var hl = opt.highlight && opt.highlight[n.id];
    svg.appendChild(svgEl("path",{d:d,fill:"none",stroke:hl?"var(--warn)":(lit?"var(--mut)":"var(--line)"),"stroke-width":hl?"2.2":"1.2"}));
  });
  var cnt={done:0,cur:0,avail:0,lock:0};
  list.forEach(function(n){
    var st, fill, stroke, op=1, txt=null;
    if(cov){
      var k=cov[n.id]||0; st=k? "cov":"lock";
      fill=k?"var(--ok)":"transparent"; stroke=k?"var(--ok)":"var(--line)"; op=k? (0.25+0.75*k/total) : 1;
      if(k) txt=String(k);
      if(k) cnt.done++; else cnt.lock++;
    } else if(done[n.id]){ st="done"; fill="var(--ok)"; stroke="var(--ok)"; cnt.done++; }
    else if(opt.current===n.id){ st="cur"; fill="var(--warn)"; stroke="var(--warn)"; cnt.cur++; }
    else if(!n.parent || !byId[n.parent] || done[n.parent]){ st="avail"; fill="transparent"; stroke="var(--acc)"; cnt.avail++; }
    else { st="lock"; fill="transparent"; stroke="var(--line)"; cnt.lock++; }
    var tip=n.label+"  ["+n.id+"]"+(n.cost_h!=null? "\n"+t("ts_cost")+": "+n.cost_h+" "+(S.lang==="ru"?"ч":"h"):"")
      +(n.level? "\n"+t("ts_level")+": "+n.level : "")
      +"\n"+(cov? t("ts_known")+": "+(cov[n.id]||0)+" / "+total : t("ts_st_"+st))
      +((n.unlocks&&n.unlocks.length)? "\n"+t("ts_unlocks")+": "+n.unlocks.join(", ") : "");
    var hlN = opt.highlight && opt.highlight[n.id];
    var g=svgEl("g",{style:opt.onPick?"cursor:pointer":null},[svgEl("title",{},[tip]),
      svgEl("rect",{x:cx(n.id),y:cy(n.id),width:C,height:C,rx:3,fill:fill,"fill-opacity":op,
        stroke:hlN?"var(--warn)":stroke,"stroke-width":hlN?2.4:(st==="avail"?1.6:1.2)})]);
    if(opt.onPick) g.addEventListener("click",function(){ opt.onPick(n); });
    if(txt) g.appendChild(svgEl("text",{x:cx(n.id)+C/2,y:cy(n.id)+C-4,"text-anchor":"middle","font-size":"9",fill:"#fff"},[txt]));
    svg.appendChild(g);
  });
  function lg(color,filled,label){ return el("span",{style:"display:inline-flex;align-items:center;gap:4px"},[
    el("span",{style:"display:inline-block;width:11px;height:11px;border-radius:3px;border:1.5px solid "+color+";background:"+(filled?color:"transparent")}),label]); }
  var legend = cov
    ? el("div",{class:"row small muted",style:"gap:12px;flex-wrap:wrap;margin-bottom:6px"},[
        lg("var(--ok)",true,t("ts_cov_legend")+" ("+cnt.done+" / "+list.length+")"), lg("var(--line)",false,t("ts_st_lock")) ])
    : el("div",{class:"row small muted",style:"gap:12px;flex-wrap:wrap;margin-bottom:6px"},[
        lg("var(--ok)",true,t("ts_st_done")+" "+cnt.done+" / "+list.length),
        cnt.cur? lg("var(--warn)",true,t("ts_st_cur")) : null,
        lg("var(--acc)",false,t("ts_st_avail")+" "+cnt.avail),
        lg("var(--line)",false,t("ts_st_lock")+" "+cnt.lock) ]);
  return el("div",{},[legend, el("div",{style:"overflow:auto;max-height:640px;border:1px solid var(--line);border-radius:8px;padding:6px"},[svg]),
    el("div",{class:"muted small",style:"margin-top:4px"},[t("ts_hint")])]);
}
function techSchemeCard(title, mk){
  var box=el("div",{},[el("p",{class:"muted small"},["…"])]);
  loadTechTree().then(function(tr){
    box.innerHTML="";
    if(!tr.ok){ box.appendChild(el("div",{class:"msg err"},[tr.error||"error"])); return; }
    box.appendChild(mk(tr.nodes));
  }).catch(function(e){ box.innerHTML=""; box.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  return el("div",{class:"card",style:"margin-top:12px"},[el("h3",{},[title]), box]);
}
function setOf(arr){ var o={}; (arr||[]).forEach(function(x){ o[x]=true; }); return o; }
// путь от корня ветки до технологии (включительно)
function techPath(nodes,id){
  var by={}; nodes.forEach(function(n){ by[n.id]=n; });
  var chain=[], cur=by[id], guard=0;
  while(cur && guard++<200){ chain.unshift(cur); cur=cur.parent? by[cur.parent] : null; }
  return chain;
}
function pathCost(chain, done){
  var miss=chain.filter(function(n){ return !done[n.id]; });
  var h=0; miss.forEach(function(n){ h+=(n.cost_h||0); });
  return {miss:miss, hours:Math.round(h*10)/10};
}
// схема + клик по узлу -> подсветка пути и панель info(node, chain)
function schemeWithPlanner(nodes, baseOpt, info){
  var holder=el("div",{},[]), panel=el("div",{style:"margin-top:10px"},[el("div",{class:"muted small"},[t("ts_pick_hint")])]);
  function draw(hl){
    holder.innerHTML="";
    holder.appendChild(techScheme(nodes, Object.assign({}, baseOpt, {highlight:hl, onPick:pick})));
  }
  function pick(n){
    var chain=techPath(nodes,n.id);
    draw(setOf(chain.map(function(x){ return x.id; })));
    panel.innerHTML=""; panel.appendChild(info(n,chain));
  }
  draw(null);
  return el("div",{},[holder,panel]);
}
function techInfoHead(n){
  return el("div",{},[el("b",{},[n.label+"  "]), el("span",{class:"muted small"},["["+n.id+"] · "+(n.family||"")]),
    (n.unlocks&&n.unlocks.length)? el("div",{class:"small",style:"margin-top:4px"},[t("ts_unlocks")+": ",
      el("span",{class:"chips",style:"display:inline-flex"}, n.unlocks.map(function(u){ return el("span",{class:"chip"},[u]); }))]) : null]);
}
function playerPlanInfo(done){
  return function(n,chain){
    var pc=pathCost(chain,done), hh=S.lang==="ru"?"ч":"h";
    var list=el("div",{class:"chips",style:"margin-top:6px"}, chain.map(function(x){
      return el("span",{class:"chip",style:done[x.id]?"color:var(--ok);border-color:var(--ok)":""},[(done[x.id]?"✓ ":"")+x.label]); }));
    return el("div",{class:"card"},[techInfoHead(n),
      el("div",{style:"margin-top:6px"},[ pc.miss.length
        ? el("span",{},[t("ts_path_left")+": ", el("b",{},[pc.miss.length+" "+t("ts_steps")+" · ~"+pc.hours+" "+hh])])
        : el("span",{class:"pill ok"},[t("ts_st_done")]) ]),
      list]);
  };
}
function clanPlanInfo(members){
  return function(n,chain){
    var hh=S.lang==="ru"?"ч":"h";
    var rows=members.map(function(m){ var pc=pathCost(chain,setOf(m.techs)); return {m:m, n:pc.miss.length, h:pc.hours}; })
      .sort(function(a,b){ return a.h-b.h || a.n-b.n; });
    return el("div",{class:"card"},[techInfoHead(n),
      el("div",{class:"muted small",style:"margin:6px 0"},[t("ts_who_closer")]),
      ltable([t("cl_player"),t("ts_path_left")], rows, function(r){ return [plLink(r.m.id,r.m.name),
        r.n? (r.n+" "+t("ts_steps")+" · ~"+r.h+" "+hh) : el("span",{class:"pill ok"},[t("ts_st_done")])]; })]);
  };
}

// ---- мини-график (линия по точкам {t, v}) ----
function lineChart(pts, title, fmt){
  var W=600, H=120, P=6;
  fmt=fmt||function(v){ return Number(v).toLocaleString(); };
  var box=el("div",{style:"flex:1;min-width:220px"},[el("div",{class:"small muted"},[title])]);
  if(pts.length<2){ box.appendChild(el("div",{class:"muted small"},[pts.length? fmt(pts[0].v) : "—"])); return box; }
  var t0=pts[0].t, t1=pts[pts.length-1].t, lo=Infinity, hi=-Infinity;
  pts.forEach(function(p){ if(p.v<lo) lo=p.v; if(p.v>hi) hi=p.v; });
  if(hi===lo){ hi+=1; lo-=1; }
  var d=pts.map(function(p,i){
    var x=P+(W-2*P)*(t1===t0?0:(p.t-t0)/(t1-t0)), y=H-P-(H-2*P)*(p.v-lo)/(hi-lo);
    return (i?"L":"M")+x.toFixed(1)+" "+y.toFixed(1); }).join(" ");
  box.appendChild(svgEl("svg",{viewBox:"0 0 "+W+" "+H,style:"width:100%;height:120px;display:block",preserveAspectRatio:"none"},[
    svgEl("path",{d:d,fill:"none",stroke:"var(--acc)","stroke-width":"2","vector-effect":"non-scaling-stroke"})]));
  var last=pts[pts.length-1].v, first=pts[0].v, dv=last-first;
  box.appendChild(el("div",{class:"small"},[el("b",{},[fmt(last)]),
    el("span",{style:"color:"+(dv>=0?"var(--ok)":"var(--err)")},["  "+(dv>=0?"+":"")+fmt(dv)]),
    el("span",{class:"muted"},["  · "+new Date(t0*1000).toLocaleDateString()+" → "+new Date(t1*1000).toLocaleDateString()])]));
  return box;
}
var CLAN_EV_KEY={joined:"ch_joined",left:"ch_left",role:"ch_role",tech:"ch_tech",renamed:"ch_renamed",
  slots:"ch_slots",created:"ch_created",disbanded:"ch_disbanded"};
function clanHistoryCard(cid){
  var body=el("div",{},[el("p",{class:"muted small"},["…"])]);
  api("/api/clan-history?id="+cid).then(function(h){
    body.innerHTML="";
    if(!h.ok){ body.appendChild(el("div",{class:"msg err"},[h.error||"error"])); return; }
    if(!h.series.length && !h.events.length){ body.appendChild(el("div",{class:"muted small"},[t("ch_empty")])); return; }
    function ser(k){ return h.series.map(function(p){ return {t:p.t, v:p[k]}; }); }
    body.appendChild(el("div",{class:"row",style:"gap:16px;flex-wrap:wrap;align-items:flex-start"},[
      lineChart(ser("rating"),t("cl_rating")), lineChart(ser("cp"),"Clan Points"), lineChart(ser("size"),t("cl_size"))]));
    if(h.events.length){
      var box=el("div",{class:"small",style:"max-height:260px;overflow:auto;margin-top:10px"},[]);
      h.events.forEach(function(e){
        var txt=t(CLAN_EV_KEY[e.kind]||e.kind);
        var who=e.uid!=null? plLink(e.uid,e.name) : null;
        var extra= e.kind==="role"? " "+e.was+" → "+e.role : e.kind==="tech"? " "+(e.label||e.tech)
          : e.kind==="renamed"? " ("+e.was+" → "+e.clan_name+")" : e.kind==="slots"? " "+e.was+" → "+e.max : "";
        box.appendChild(el("div",{},[el("span",{class:"lg-t mono"},[e.ts+"  "]), txt+" ", who, extra]));
      });
      body.appendChild(box);
    }
  }).catch(function(e){ body.innerHTML=""; body.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  return el("div",{class:"card",style:"margin-top:12px"},[el("h3",{},[t("ch_title")]), el("p",{class:"muted small"},[t("ch_intro")]), body]);
}

// ---- крафт: раскладка до сырья ----
var CRAFT_CAT=null;
function tabCraft(v){
  var inp=el("input",{type:"text",list:"craft-dl",placeholder:t("cr_ph"),style:"min-width:260px"});
  var dl=el("datalist",{id:"craft-dl"},[]);
  var qty=el("input",{type:"number",min:"1",value:"1",style:"width:80px"});
  var whoSel=el("select",{},[el("option",{value:""},[t("cr_who_none")])]);
  var uidInp=el("input",{type:"number",placeholder:"ID",style:"width:90px;display:none"});
  var msg=el("span",{class:"muted small"},[]);
  var out=el("div",{style:"margin-top:12px"},[]);
  var byName={};
  function loadCat(cb){
    if(CRAFT_CAT) return cb(CRAFT_CAT);
    api("/api/craft-catalog").then(function(d){ if(d.ok){ CRAFT_CAT=d; cb(d); } else msg.textContent=d.error||"error"; })
      .catch(function(e){ msg.textContent=errText(e); });
  }
  loadCat(function(d){
    d.items.forEach(function(it){ byName[it.name.toLowerCase()]=it.id; dl.appendChild(el("option",{value:it.name},[])); });
  });
  whoSel.appendChild(el("option",{value:"uid"},[t("cr_who_player")]));
  api("/api/clans").then(function(c){ (c.clans||[]).forEach(function(x){
    whoSel.appendChild(el("option",{value:"clan:"+x.id},[t("cl_name")+": "+x.name])); }); }).catch(function(){});
  whoSel.addEventListener("change",function(){ uidInp.style.display=whoSel.value==="uid"?"":"none"; });
  function node(n){
    var hh=n.via==="craft"? (n.workbench? n.workbench+" · ":"")+n.crafts+"× · "+n.time+" "+t("cr_sec")
      : n.via==="machine"? n.machine+(n.alts&&n.alts.length? " ("+t("cr_or")+" "+n.alts.join(", ")+")":"") : t("cr_raw");
    var head=el("span",{},[el("b",{},[n.name]), " × "+n.need+"  ", el("span",{class:"muted small"},[hh])]);
    if(!n.children || !n.children.length) return el("div",{style:"margin-left:14px"},[head]);
    return el("details",{open:"",style:"margin-left:14px"},[el("summary",{},[head])].concat(n.children.map(node)));
  }
  function run(){
    var key=inp.value.trim(); if(!key) return;
    var id=byName[key.toLowerCase()]||key;
    var q="/api/craft-plan?item="+encodeURIComponent(id)+"&qty="+(parseInt(qty.value,10)||1);
    if(whoSel.value==="uid" && uidInp.value) q+="&uid="+encodeURIComponent(uidInp.value);
    if(whoSel.value.indexOf("clan:")===0) q+="&clan="+whoSel.value.slice(5);
    msg.textContent=t("working"); out.innerHTML="";
    api(q).then(function(p){
      msg.textContent="";
      if(!p.ok){ out.appendChild(el("div",{class:"msg err"},[p.error||"error"])); return; }
      var hh=S.lang==="ru"?"ч":"h", mins=Math.round(p.time_s/6)/10;
      var g=el("div",{class:"grid"},[]);
      g.appendChild(kvcard(p.name+" × "+p.qty,[
        [t("cr_time"), p.time_s+" "+t("cr_sec")+(p.time_s>=60? " (~"+mins+" "+t("pd_min")+")":"")],
        [t("cr_benches"), p.benches.length? el("div",{class:"chips"}, p.benches.map(function(b){ return el("span",{class:"chip"},[b]); })) : "—"],
        [t("cr_used_in"), p.used_in.length? el("div",{class:"chips"}, p.used_in.map(function(b){ return el("span",{class:"chip"},[b]); })) : "—"]
      ]));
      g.appendChild(el("div",{class:"card"},[el("h3",{},[t("cr_techs")+(p.who? " · "+p.who:"")]),
        p.techs.length? ltable([t("cr_tech"),t("cr_status")], p.techs, function(x){ return [x.label,
          x.known===null? "—" : x.known? el("span",{class:"pill ok"},[t("ts_st_done")])
            : el("span",{class:"pill warn"},[t("ts_path_left")+": "+x.missing_chain+" "+t("ts_steps")+" · ~"+x.missing_h+" "+hh])]; })
          : el("div",{class:"muted small"},[t("cr_no_tech")])]));
      g.appendChild(el("div",{class:"card"},[el("h3",{},[t("cr_raw_total")]),
        ltable([t("cr_item"),t("cr_count")], p.raw, function(x){ return [x.name, Number(x.count).toLocaleString()]; })]));
      if(p.intermediate.length) g.appendChild(el("div",{class:"card"},[el("h3",{},[t("cr_inter")]),
        ltable([t("cr_item"),t("cr_count"),t("cr_crafts")], p.intermediate, function(x){ return [x.name, String(x.need), x.crafts? String(x.crafts) : "—"]; })]));
      out.appendChild(g);
      out.appendChild(el("div",{class:"card",style:"margin-top:12px"},[el("h3",{},[t("cr_tree")]), node(p.tree)]));
    }).catch(function(e){ msg.textContent=errText(e); });
  }
  inp.addEventListener("keydown",function(e){ if(e.key==="Enter") run(); });
  v.appendChild(el("div",{},[
    el("div",{class:"card"},[el("h3",{},[t("cr_title")]), el("p",{class:"muted small"},[t("cr_intro")]),
      el("div",{class:"row",style:"gap:8px;flex-wrap:wrap;align-items:center"},[inp, dl, el("span",{class:"small"},["×"]), qty,
        el("span",{class:"small"},[t("cr_who")]), whoSel, uidInp,
        el("button",{class:"small pri",onclick:run},[t("cr_go")]), msg])]),
    out]));
}

// ---- торговля: терминалы игроков + магазины на картах ----
function tabTrade(v){
  var q=el("input",{type:"text",placeholder:t("tr_ph"),style:"min-width:240px"});
  var src=el("select",{},[el("option",{value:""},[t("tr_src_all")]),el("option",{value:"terminal"},[t("tr_src_term")]),el("option",{value:"shop"},[t("tr_src_shop")])]);
  var side=el("select",{},[el("option",{value:"any"},[t("tr_side_any")]),el("option",{value:"give"},[t("tr_side_give")]),el("option",{value:"want"},[t("tr_side_want")])]);
  var msg=el("span",{class:"muted small"},[]);
  var out=el("div",{style:"margin-top:12px"},[]);
  var D=null;
  function names(arr){ return arr.map(function(x){ return x.name+" ×"+Number(x.count).toLocaleString(); }).join(", "); }
  function num(v){ return Number(v).toLocaleString(undefined,{maximumFractionDigits:v<10?3:1}); }
  // курс «1 дорогого = N дешёвого» — чтобы не было 0.0002
  function rate(o){
    if(o.unit==null || !o.unit) return "—";
    var g=o.give[0].name, w=o.want[0].name;
    return o.unit>=1 ? ("1 "+g+" = "+num(o.unit)+" "+w) : ("1 "+w+" = "+num(1/o.unit)+" "+g);
  }
  function who(o){ return el("span",{},[o.online? el("span",{class:"dot ok",style:"margin-right:4px"}) : null, plLink(o.id,o.name),
    o.clan? el("span",{class:"muted small"},[" · "+o.clan]) : null]); }
  function render(){
    out.innerHTML="";
    if(!D) return;
    var needle=q.value.trim().toLowerCase();
    function hit(arr){ return arr.some(function(x){ return x.name.toLowerCase().indexOf(needle)>=0 || String(x.id)===needle; }); }
    var rows=D.offers.filter(function(o){
      if(src.value && o.src!==src.value) return false;
      if(!needle) return true;
      return side.value==="give"? hit(o.give) : side.value==="want"? hit(o.want) : (hit(o.give)||hit(o.want)); });
    out.appendChild(el("div",{class:"muted small",style:"margin-bottom:6px"},[
      rows.length+" / "+D.offers.length+" "+t("tr_offers")+" · "+D.terminals.length+" "+t("tr_terms")+" · "+D.shops.length+" "+t("tr_shops")+" · "+t("tr_scan")+" "+D.scan_sec+" s"]));
    out.appendChild(el("div",{class:"card",style:"overflow:auto"},[ltable([t("tr_owner"),t("tr_give"),t("tr_want"),t("tr_rate"),t("tr_where")], rows.slice(0,600), function(o){
      return [who(o.owner), names(o.give), names(o.want),
        rate(o),
        o.src==="terminal"? t("tr_src_term") : o.where]; })]));
    out.appendChild(el("details",{style:"margin-top:12px"},[el("summary",{},[t("tr_terms_list")+" · "+D.terminals.length]),
      ltable([t("tr_owner"),t("tr_lots"),t("tr_sales"),t("tr_idle"),t("tr_storage")], D.terminals, function(x){
        return [who(x.owner), String(x.lots), String(x.sales), x.idle_h!=null? x.idle_h+" "+(S.lang==="ru"?"ч":"h") : "—", names(x.storage)||"—"]; })]));
    out.appendChild(el("details",{style:"margin-top:8px"},[el("summary",{},[t("tr_shops_list")+" · "+D.shops.length]),
      ltable([t("tr_owner"),t("tr_where"),t("tr_lots"),t("tr_sales"),t("tr_storage")], D.shops, function(x){
        return [who(x.owner), mapName(x.map)+" · "+x.x+", "+x.y, String(x.slots), String(x.sales), names(x.storage)||"—"]; })]));
  }
  function load(force){
    msg.textContent=t("tr_loading");
    api("/api/trade"+(force?"?force=1":"")).then(function(r){
      if(r.ready){ msg.textContent=""; D=r.data; render(); return; }
      var iv=setInterval(function(){
        api("/api/job?id="+r.job).then(function(j){
          if(!j.done) return;
          clearInterval(iv); msg.textContent="";
          var d=j.result||{};
          if(!d.ok){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
          D=d; render();
        }).catch(function(){ clearInterval(iv); msg.textContent=t("err_net"); });
      },1500);
    }).catch(function(e){ msg.textContent=errText(e); });
  }
  q.addEventListener("input",render); src.addEventListener("change",render); side.addEventListener("change",render);
  v.appendChild(el("div",{},[
    el("div",{class:"card"},[el("h3",{},[t("tr_title")]), el("p",{class:"muted small"},[t("tr_intro")]),
      el("div",{class:"row",style:"gap:8px;flex-wrap:wrap;align-items:center"},[q, side, src,
        el("button",{class:"small",onclick:function(){ load(true); }},[t("refresh")]), msg])]),
    out]));
  load(false);
}

// ---- тяжёлые отчёты: {ready,data} сразу или {job} -> опрос /api/job ----
function loadHeavy(url, msg, onData){
  msg.textContent=t("tr_loading");
  api(url).then(function(r){
    if(r.ready){ msg.textContent=""; onData(r.data); return; }
    var iv=setInterval(function(){
      api("/api/job?id="+r.job).then(function(j){
        if(!j.done) return;
        clearInterval(iv); msg.textContent="";
        onData(j.result||{ok:false});
      }).catch(function(){ clearInterval(iv); msg.textContent=t("err_net"); });
    },1500);
  }).catch(function(e){ msg.textContent=errText(e); });
}
function fmtN(v){ return v==null? "—" : Number(v).toLocaleString(); }
// «1 дорогого ≈ N дешёвого», чтобы не было «≈ 0»
function rateTxt(name, r){
  if(!r || !r.median) return "—";
  return r.median>=1 ? ("1 "+name+" ≈ "+fmtN(Math.round(r.median*100)/100)+" "+r.pay)
    : ("1 "+r.pay+" ≈ "+fmtN(Math.round(100/r.median)/100)+" "+name);
}
function fmtD(v){ if(v==null) return el("span",{class:"muted"},["—"]); if(!v) return "0";
  return el("span",{style:"color:"+(v>0?"var(--ok)":"var(--err)")},[(v>0?"+":"")+Number(v).toLocaleString()]); }

// ---- экономика ----
function tabEconomy(v){
  var q=el("input",{type:"text",placeholder:t("tr_ph"),style:"min-width:220px"});
  var sortSel=el("select",{},[["total","ec_s_total"],["players","ec_s_players"],["containers","ec_s_cont"],["trade","ec_s_trade"],
    ["holders","ec_s_holders"],["d1","ec_s_d1"],["d7","ec_s_d7"]].map(function(x){ return el("option",{value:x[0]},[t(x[1])]); }));
  var msg=el("span",{class:"muted small"},[]);
  var out=el("div",{style:"margin-top:12px"},[]);
  var detail=el("div",{},[]);
  var D=null;
  function showItem(x){
    detail.innerHTML="";
    var card=el("div",{class:"card",style:"margin-top:12px"},[el("h3",{},[x.name+" · "+fmtN(x.total)])]);
    var hist=el("div",{},[el("p",{class:"muted small"},["…"])]);
    api("/api/economy-item?id="+x.id).then(function(h){
      hist.innerHTML="";
      hist.appendChild(lineChart((h.series||[]).map(function(p){ return {t:p.t, v:p.v}; }), t("ec_hist")));
    }).catch(function(){});
    card.appendChild(el("div",{class:"row",style:"gap:16px;flex-wrap:wrap;align-items:flex-start"},[
      el("div",{style:"flex:1;min-width:260px"},[hist]),
      el("div",{style:"flex:1;min-width:260px"},[el("div",{class:"small muted"},[t("ec_top")]),
        ltable([t("cl_player"),t("cr_count")], x.top, function(u){ return [plLink(u.id,u.name), fmtN(u.count)]; })])]));
    if(x.rate) card.appendChild(el("div",{class:"small",style:"margin-top:8px"},[
      t("ec_rate")+": "+rateTxt(x.name,x.rate)+"  ("+x.rate.n+" "+t("tr_offers")+")"]));
    detail.appendChild(card);
    card.scrollIntoView({behavior:"smooth",block:"nearest"});
  }
  function render(){
    out.innerHTML="";
    if(!D) return;
    if(!D.ok){ out.appendChild(el("div",{class:"msg err"},[D.error||"error"])); return; }
    var needle=q.value.trim().toLowerCase(), k=sortSel.value;
    var rows=D.items.filter(function(x){ return !needle || x.name.toLowerCase().indexOf(needle)>=0 || String(x.id)===needle; });
    rows=rows.slice().sort(function(a,b){ return (b[k]||0)-(a[k]||0); });
    out.appendChild(el("div",{class:"muted small",style:"margin-bottom:6px"},[
      rows.length+" / "+D.items.length+" "+t("ec_items")+" · "+D.players_scanned+" "+t("ec_players")+" · "
      +t("ec_snaps")+": "+D.snapshots+" · "+t("tr_scan")+" "+D.scan_sec+" s"]));
    var tb=el("table",{},[el("tr",{},[t("cr_item"),t("ec_total"),t("ec_players_col"),t("ec_cont"),t("ec_trade"),t("ec_holders"),t("ec_top1"),t("ec_rate"),t("ec_d1"),t("ec_d7")]
      .map(function(x){ return el("th",{},[x]); }))]);
    rows.slice(0,400).forEach(function(x){
      tb.appendChild(el("tr",{style:"cursor:pointer",onclick:function(){ showItem(x); }},[
        el("td",{},[el("a",{class:"pl-link"},[x.name])]), el("td",{},[el("b",{},[fmtN(x.total)])]),
        el("td",{},[fmtN(x.players)]), el("td",{},[fmtN(x.containers)]), el("td",{},[fmtN(x.trade)]),
        el("td",{},[String(x.holders)]),
        el("td",{},[x.top[0]? x.top[0].name+" ("+fmtN(x.top[0].count)+")" : "—"]),
        el("td",{class:"small"},[rateTxt(x.name,x.rate)]),
        el("td",{},[fmtD(x.d1)]), el("td",{},[fmtD(x.d7)])]));
    });
    out.appendChild(el("div",{class:"card",style:"overflow:auto"},[tb]));
  }
  function load(force){ loadHeavy("/api/economy"+(force?"?force=1":""), msg, function(d){ D=d; render(); }); }
  q.addEventListener("input",render); sortSel.addEventListener("change",render);
  v.appendChild(el("div",{},[
    el("div",{class:"card"},[el("h3",{},[t("ec_title")]), el("p",{class:"muted small"},[t("ec_intro")]),
      el("div",{class:"row",style:"gap:8px;flex-wrap:wrap;align-items:center"},[q, el("span",{class:"small"},[t("ec_sort")]), sortSel,
        el("button",{class:"small",onclick:function(){ load(true); }},[t("refresh")]), msg])]),
    adminPriceCard(), detail, out]));
  load(false);
}
// история цен рынка (снимки панели игроков раз в час) + резкие изменения
function adminPriceCard(){
  var body=el("div",{},[el("p",{class:"muted small"},["…"])]);
  api("/api/price-history").then(function(d){
    body.innerHTML="";
    body.appendChild(el("p",{class:"muted small"},[t("ph_intro")+" "+t("ph_snaps")+": "+d.snapshots+(d.since? " · "+new Date(d.since*1000).toLocaleDateString() : "")]));
    if(d.alerts&&d.alerts.length){
      body.appendChild(el("h3",{},["⚠ "+t("ph_alerts")+" · "+d.alerts.length]));
      body.appendChild(scT(ltable([t("ph_item"),t("ph_period"),t("ph_was"),t("ph_now"),"%",t("ph_lots")],d.alerts,function(a){
        return [a.name+" ("+a.pay+")", a.period, fmtN(a.was), fmtN(a.now),
          el("b",{style:"color:"+(a.change<0?"var(--err)":"var(--ok)")},[(a.change>0?"+":"")+a.change+"%"]), String(a.lots)]; })));
    } else body.appendChild(el("div",{class:"muted small"},[t("ph_noalerts")]));
    if(!d.items.length) return;
    var sel=el("select",{style:"margin-top:10px;max-width:100%"},[el("option",{value:""},["— "+t("ph_pick")+" —"])].concat(
      d.items.map(function(x){ return el("option",{value:x.id},[x.name+" · "+fmtN(x.median)+" × "+x.pay+" ("+x.lots+")"]); })));
    var ch=el("div",{},[]);
    sel.onchange=function(){
      ch.innerHTML=""; if(!sel.value) return;
      api("/api/price-history?id="+encodeURIComponent(sel.value)).then(function(h){
        var sr=h.series||[];
        ch.appendChild(el("div",{class:"row",style:"gap:16px;flex-wrap:wrap;align-items:flex-start;margin-top:8px"},[
          lineChart(sr.map(function(p){ return {t:p.t,v:p.median}; }),t("ph_median")+", "+h.pay),
          lineChart(sr.map(function(p){ return {t:p.t,v:p.min}; }),t("ph_min")+", "+h.pay),
          lineChart(sr.map(function(p){ return {t:p.t,v:p.lots}; }),t("ph_lots"))]));
        if(h.other_pays&&h.other_pays.length) ch.appendChild(el("div",{class:"muted small"},[t("ph_other")+": "+h.other_pays.join(", ")]));
      }).catch(function(e){ ch.appendChild(el("div",{class:"msg err"},[errText(e)])); });
    };
    body.appendChild(sel); body.appendChild(ch);
  }).catch(function(e){ body.innerHTML=""; body.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  return el("div",{class:"card",style:"margin-top:12px"},[el("h3",{},[t("ph_title")]), body]);
}
// весь сезонный рейтинг (очки, разрывы, награды)
function seasonRatingCard(){
  var body=el("div",{},[el("p",{class:"muted small"},["…"])]);
  api("/api/season-rating").then(function(d){
    body.innerHTML="";
    if(!d.ok){ body.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
    body.appendChild(el("p",{class:"muted small"},[t("sr_intro").replace("{n}",d.with_points).replace("{all}",d.total_users)
      +(d.payouts.length? " "+t("sr_last")+": "+d.payouts[0].ts+" ("+d.payouts[0].n+")" : "")]));
    var q=el("input",{placeholder:t("sr_find"),style:"margin-bottom:6px;min-width:220px"}), tb=el("div",{style:"max-height:460px;overflow:auto"});
    function draw(){
      var f=q.value.trim().toLowerCase(); tb.innerHTML="";
      tb.appendChild(ltable(["#",t("cl_player"),t("sr_clan"),t("pd_level"),t("sr_points"),t("sr_gap"),t("sr_reward"),t("sr_got")],
        d.rows.filter(function(r){ return !f || r.name.toLowerCase().indexOf(f)>=0 || (r.clan||"").toLowerCase().indexOf(f)>=0; }), function(r){
          return [String(r.place), plLink(r.id,r.name), r.clan||"—", r.level==null?"—":String(r.level), el("b",{},[fmtN(r.points)]),
            r.gap_prev==null?"—":fmtN(r.gap_prev), r.reward? "+"+r.reward : "—", r.rewards_n? r.rewards_n+" · Σ"+r.rewards_sum : "—"]; }));
    }
    q.addEventListener("input",draw); body.appendChild(q); body.appendChild(tb); draw();
  }).catch(function(e){ body.innerHTML=""; body.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  return el("div",{class:"card",style:"margin-bottom:12px"},[el("h3",{},[t("sr_title")]), body]);
}
// инструменты из панели игрока в карточке игрока
var PT_ICO={tech:"🔬",research:"🧪",booster:"⚡",level:"⭐",clan:"🛡",death:"💀",reward:"🏆",role:"🎖",land:"🏚",session:"🎮",panel:"🖥"};
function pdToolsCard(d){
  var out=el("div",{style:"margin-top:10px"},[]);
  function show(title, url, draw){
    out.innerHTML=""; out.appendChild(el("h3",{},[title]));
    var body=el("div",{},[el("p",{class:"muted small"},["…"])]); out.appendChild(body);
    api(url).then(function(j){ body.innerHTML="";
      if(!j.ok){ body.appendChild(el("div",{class:"msg err"},[j.error||"error"])); return; } draw(body,j);
    }).catch(function(e){ body.innerHTML=""; body.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  function chips(list){ return el("div",{class:"row",style:"gap:4px;flex-wrap:wrap"},list.map(function(x){ return el("span",{class:"chip"},[x.name+" ×"+fmtN(x.count)]); })); }
  function where(){ show(t("pt_where"),"/api/players/"+d.id+"/where",function(body,j){
    var q=el("input",{placeholder:t("pt_where_ph"),style:"width:100%;margin-bottom:8px"}), res=el("div");
    function draw(){
      var f=q.value.trim().toLowerCase(); res.innerHTML="";
      var places=j.places.map(function(p){ return {where:p.where, items:p.items.filter(function(x){
        return !f || x.name.toLowerCase().indexOf(f)>=0 || (x.id||"").toLowerCase().indexOf(f)>=0; })}; }).filter(function(p){ return p.items.length; });
      var tot={}; places.forEach(function(p){ p.items.forEach(function(x){ var k=x.id||x.name; tot[k]=tot[k]||{name:x.name,count:0}; tot[k].count+=x.count; }); });
      var all=Object.keys(tot).map(function(k){ return tot[k]; }).sort(function(a,b){ return b.count-a.count; });
      res.appendChild(el("div",{class:"small muted",style:"margin:4px 0"},[t("pt_total")+" · "+all.length])); res.appendChild(chips(all));
      res.appendChild(el("div",{style:"max-height:380px;overflow:auto;margin-top:8px"},[ltable([t("pt_place"),t("pt_items")],places,function(p){ return [p.where, chips(p.items)]; })]));
      if(j.market_pending) res.appendChild(el("div",{class:"muted small"},[t("pt_market_pending")]));
    }
    q.addEventListener("input",draw); body.appendChild(q); body.appendChild(res); draw(); }); }
  function journal(){ show(t("pt_journal"),"/api/players/"+d.id+"/journal",function(body,j){
    var off={session:true}, bar=el("div",{class:"row",style:"gap:4px;flex-wrap:wrap;margin-bottom:8px"}), list=el("div");
    function draw(){
      bar.innerHTML=""; list.innerHTML="";
      Object.keys(j.kinds).forEach(function(k){ bar.appendChild(el("button",{class:"small"+(off[k]?"":" pri"),onclick:function(){ off[k]=!off[k]; draw(); }},
        [(PT_ICO[k]||"")+" "+(T[S.lang]["pt_k_"+k]||k)+" · "+j.kinds[k]])); });
      var rows=j.events.filter(function(e){ return !off[e.kind]; }).slice(0,1000);
      list.appendChild(el("div",{style:"max-height:440px;overflow:auto"},[ltable([t("pt_when"),t("pt_event"),""],rows,function(e){
        return [el("span",{class:"mono small",style:"white-space:nowrap"},[e.t? new Date(e.t*1000).toLocaleString() : "—"]),
          (PT_ICO[e.kind]||"")+" "+e.label, el("span",{class:"small",style:"word-break:break-word"},[e.kind==="session"&&e.detail? e.detail+" "+t("pd_min") : e.detail])]; })]));
    }
    body.appendChild(el("p",{class:"muted small"},[t(isGM()? "pt_journal_gm" : "pt_journal_hint")])); body.appendChild(bar); body.appendChild(list); draw(); }); }
  function hist(){ show(t("pt_hist"),"/api/players/"+d.id+"/history",function(body,j){
    function ser(k){ return j.points.map(function(p){ return {t:p.t,v:p[k]}; }); }
    if(!j.points.length && (j.techs||[]).length<2){ body.appendChild(el("div",{class:"muted small"},[t("pt_hist_none")])); return; }
    body.appendChild(el("div",{class:"row",style:"gap:16px;flex-wrap:wrap;align-items:flex-start"},[
      lineChart(ser("level"),t("pd_level")), lineChart(ser("rating"),t("pd_rating")), lineChart(j.techs||[],t("pt_techs")),
      lineChart(ser("play_h"),t("pd_playtime")), lineChart(ser("research_h"),t("st_resh"))]));
    if(j.since) body.appendChild(el("div",{class:"muted small"},[t("pt_hist_since")+" "+new Date(j.since*1000).toLocaleString()])); }); }
  function viewAs(){
    gatedApi("/api/players/"+d.id+"/view-as",{},function(r){
      var u=location.protocol+"//"+location.hostname+(r.port&&r.port!==80? ":"+r.port : "")+r.path;
      out.innerHTML=""; out.appendChild(el("h3",{},["👁 "+t("pt_viewas")]));
      out.appendChild(el("p",{class:"small"},[t("pt_viewas_hint")]));
      out.appendChild(el("a",{href:u,target:"_blank",rel:"noopener",class:"pl-link",style:"font-weight:600"},[t("pt_viewas_open")+" "+d.name+" ↗"]));
    },function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  var canMod=S.role!=="viewer";
  return el("div",{class:"card",style:"margin-top:12px"},[el("h3",{},[t("pt_title")]),
    el("div",{class:"row",style:"gap:6px;flex-wrap:wrap"},[
      canMod? el("button",{class:"small",onclick:where},["📦 "+t("pt_where")]) : null,
      canMod? el("button",{class:"small",onclick:journal},["📜 "+t("pt_journal")]) : null,
      el("button",{class:"small",onclick:hist},["📈 "+t("pt_hist")]),
      isGM()? el("button",{class:"small danger",onclick:viewAs},["👁 "+t("pt_viewas")]) : null]), out]);
}

// ---- нарушения (под паролем панели) ----
function tabSuspicious(v){
  var out=el("div",{},[el("p",{class:"muted"},["…"])]);
  var kindSel=el("select",{},[["","su_k_all"],["inv_spike","su_k_inv"],["fast_research","su_k_res"]].map(function(x){ return el("option",{value:x[0]},[t(x[1])]); }));
  var onlyTw=el("input",{type:"checkbox"});
  var D=null;
  function render(){
    out.innerHTML="";
    if(!D) return;
    var rows=D.events.filter(function(e){ return (!kindSel.value || e.kind===kindSel.value) && (!onlyTw.checked || e.twink); });
    out.appendChild(el("div",{class:"muted small",style:"margin-bottom:6px"},[rows.length+" / "+D.total+" "+t("su_events")]));
    var box=el("div",{class:"card",style:"overflow:auto"},[]);
    if(!rows.length) box.appendChild(el("div",{class:"muted"},[t("su_none")]));
    rows.forEach(function(e){
      var body;
      if(e.kind==="inv_spike"){
        body=[el("span",{class:"pill "+(e.twink?"err":"warn")},[e.twink? t("su_twink") : t("su_k_inv")]), " ", plLink(e.uid,e.name),
          " +"+fmtN(e.gain)+" "+e.item+" ("+fmtN(e.was)+" → "+fmtN(e.now)+")"];
        if(e.donors && e.donors.length) body.push(el("div",{class:"small muted",style:"margin-left:12px"},[t("su_donors")+": "].concat(
          e.donors.map(function(d,i){ return el("span",{},[i?", ":"", plLink(d.id,d.name), " −"+fmtN(d.loss)+(d.twink?" ⚠ "+t("su_twink"):"")]); }))));
      } else if(e.kind==="fast_research"){
        body=[el("span",{class:"pill warn"},[t("su_k_res")]), " ", plLink(e.uid,e.name),
          " "+t("su_res_txt")+" "+fmtN(e.cost_min)+" "+t("pd_min")+" / "+t("su_allowed")+" "+fmtN(e.allowed_min)+" "+t("pd_min")
          +(e.boosters? " ("+t("su_boost")+" "+e.boosters+")":""),
          el("div",{class:"small muted",style:"margin-left:12px"},[e.techs.join(", ")])];
      } else body=[e.kind];
      box.appendChild(el("div",{style:"padding:6px 0;border-bottom:1px solid var(--line)"},[el("span",{class:"lg-t mono"},[e.ts+"  "])].concat(body)));
    });
    out.appendChild(box);
    var tc=el("div",{class:"card",style:"margin-top:12px;overflow:auto"},[el("h3",{},[t("su_trade")]), el("p",{class:"muted small"},[t("su_trade_hint")])]);
    if(D.trade_anomalies==null) tc.appendChild(el("div",{class:"muted small"},[t("su_trade_need")]));
    else if(!D.trade_anomalies.length) tc.appendChild(el("div",{class:"muted small"},[t("su_none")]));
    else tc.appendChild(ltable([t("tr_owner"),t("tr_give"),t("tr_want"),t("su_vs_median"),t("tr_where")], D.trade_anomalies, function(a){
      return [plLink(a.owner.id,a.owner.name), a.give.map(function(x){ return x.name+" ×"+fmtN(x.count); }).join(", "),
        a.want.map(function(x){ return x.name+" ×"+fmtN(x.count); }).join(", "),
        el("b",{style:"color:"+(a.x<1?"var(--err)":"var(--warn)")},["×"+a.x]), a.src==="terminal"? t("tr_src_term") : a.where]; }));
    out.appendChild(tc);
  }
  function load(){
    gatedApi("/api/suspicious", {}, function(d){ D=d; render(); },
      function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  kindSel.addEventListener("change",render); onlyTw.addEventListener("change",render);
  v.appendChild(el("div",{},[
    el("div",{class:"card",style:"margin-bottom:12px"},[el("h3",{},[t("sv_title")]), el("p",{class:"muted small"},[t("su_intro")]),
      el("div",{class:"row",style:"gap:10px;flex-wrap:wrap;align-items:center"},[kindSel,
        el("label",{class:"small"},[onlyTw," "+t("su_only_twink")]),
        el("button",{class:"small",onclick:load},[t("refresh")])])]),
    out]));
  load();
}

// ---- флот игроков (корабли в космосе по владельцам + станции) ----
function fleetCard(){
  var box=el("div",{},[el("p",{class:"muted small"},["…"])]);
  api("/api/fleet").then(function(d){
    box.innerHTML="";
    if(!d.ok){ box.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
    box.appendChild(el("div",{class:"muted small",style:"margin-bottom:6px"},[d.ships+" "+t("fl_ships")+" · "+d.owners.length+" "+t("fl_owners")+" · "+d.stations.length+" "+t("fl_stations")]));
    if(d.owners.length) box.appendChild(ltable([t("cl_player"),t("cl_name"),t("fl_ship"),t("pd_coords"),t("su_hp"),t("fl_cargo")],
      [].concat.apply([], d.owners.map(function(o){ return o.ships.map(function(sh,i){ return {o:o, sh:sh, first:i===0}; }); })),
      function(r){ var sh=r.sh;
        return [r.first? (r.o.id? plLink(r.o.id,r.o.name) : "—") : "", r.first? (r.o.clan||"—") : "",
          sh.model+(sh.moving? " · "+t("su_moving") : ""),
          el("span",{},["★"+(sh.star_name? sh.star_name+" (#"+sh.star+")" : sh.star)+" · "+Math.round(sh.x)+", "+Math.round(sh.y),
            el("div",{class:"muted small"},[sh.near? t("fl_near")+" "+sh.near+" · "+fmtN(sh.near_dist) : t("fl_transit")])]),
          String(sh.health), sh.cargo.length? el("span",{class:"small"},[sh.cargo.map(function(c){ return c.name+" ×"+fmtN(c.count); }).join(", ")]) : "—"]; }));
    if(d.stations.length){
      box.appendChild(el("div",{class:"small muted",style:"margin-top:10px"},[t("fl_stations_t")]));
      box.appendChild(ltable([t("cl_name"),t("tr_owner"),t("cl_name")+" ("+t("clans")+")",t("pd_coords"),t("fl_size")], d.stations, function(x){
        return [x.name||("#"+x.id), plLink(x.owner.id,x.owner.name), x.clan||"—", "★"+x.star+" · "+x.x+", "+x.y, x.size]; }));
    }
  }).catch(function(e){ box.innerHTML=""; box.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  return el("div",{class:"card",style:"overflow:auto"},[el("h3",{},["🛰 "+t("fl_title")]), el("p",{class:"muted small"},[t("fl_intro")]), box]);
}

function tabFleet(v){ v.appendChild(fleetCard()); }

// ---- активность и удержание ----
function pct(a){ return a && a[1]? Math.round(100*a[0]/a[1])+"% ("+a[0]+"/"+a[1]+")" : "—"; }
function tabActivity(v){
  var out=el("div",{},[el("p",{class:"muted"},["…"])]);
  v.appendChild(out);
  api("/api/activity").then(function(d){
    out.innerHTML="";
    if(!d.ok){ out.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
    var g=el("div",{class:"row",style:"gap:16px;flex-wrap:wrap;align-items:flex-start"},[
      lineChart(d.days.map(function(x){ return {t:Date.parse(x.d)/1000, v:x.active}; }), t("ac_active")),
      lineChart(d.days.map(function(x){ return {t:Date.parse(x.d)/1000, v:x.reg}; }), t("ac_reg"))]);
    out.appendChild(el("div",{class:"card"},[el("h3",{},[t("ac_days")+" · "+d.players+" "+t("ac_players")]), g]));
    out.appendChild(el("div",{class:"card",style:"margin-top:12px;overflow:auto"},[el("h3",{},[t("ac_cohorts")]),
      el("p",{class:"muted small"},[t("ac_cohorts_hint")]),
      ltable([t("ac_week"),t("ac_size"),"D1","D7","D30"], d.cohorts.slice().reverse(), function(c){ return [c.week, String(c.size), pct(c.d1), pct(c.d7), pct(c.d30)]; })]));
    // тепловая карта
    var mx=0; d.heat.forEach(function(r){ r.forEach(function(v){ if(v>mx) mx=v; }); });
    var days=t("ac_wd").split(","), tb=el("table",{class:"small",style:"border-collapse:collapse"},[
      el("tr",{},[el("th",{},[""])].concat(Array.apply(null,{length:24}).map(function(_,hh){ return el("th",{style:"padding:2px 3px;font-weight:400"},[String(hh)]); })))]);
    d.heat.forEach(function(r,wd){
      tb.appendChild(el("tr",{},[el("th",{style:"padding:2px 6px;font-weight:400"},[days[wd]])].concat(r.map(function(val,hh){
        var a=mx? val/mx : 0;
        return el("td",{title:days[wd]+" "+hh+":00 — "+val,style:"width:22px;height:18px;background:rgba(76,141,255,"+(0.08+0.92*a).toFixed(2)+");border:1px solid var(--line)"},[""]); }))));
    });
    out.appendChild(el("div",{class:"card",style:"margin-top:12px;overflow:auto"},[el("h3",{},[t("ac_heat")]),
      el("p",{class:"muted small"},[t("ac_heat_hint")+" "+mx]), tb]));
    out.appendChild(el("div",{class:"card",style:"margin-top:12px"},[el("h3",{},[t("ac_churn")]),
      el("p",{class:"muted small"},[t("ac_churn_hint").replace("{d}",d.churn_days)+" "+t("ac_quit1h")+": "+d.quit_first_hour]),
      ltable([t("pd_level"),t("ac_act"),t("ac_gone"),t("ac_rate")], d.levels, function(r){ var tot=r.active+r.churned;
        return [r.bucket, String(r.active), String(r.churned), tot? Math.round(100*r.churned/tot)+"%" : "—"]; })]));
  }).catch(function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
}

// ---- рейтинги ----
function tabLeaders(v){
  var out=el("div",{},[el("p",{class:"muted"},["…"])]);
  v.appendChild(seasonRatingCard());
  v.appendChild(out);
  function board(title, rows, fmt){
    return el("div",{class:"card"},[el("h3",{},[title]), rows.length? ltable(["#",t("cl_player"),""], rows, function(r){
      return [String(rows.indexOf(r)+1), plLink(r.id,r.name), el("b",{},[fmt? fmt(r.v) : fmtN(r.v)])]; }) : el("div",{class:"muted small"},["—"])]);
  }
  api("/api/leaderboards").then(function(d){
    out.innerHTML="";
    if(!d.ok){ out.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
    var g=el("div",{class:"grid"},[]);
    d.rich.forEach(function(r){ g.appendChild(board("💰 "+r.item, r.rows)); });
    g.appendChild(board("🏪 "+t("lb_traders"), d.traders));
    g.appendChild(board("🔬 "+t("lb_rweek"), d.research_week));
    g.appendChild(board("📚 "+t("lb_rtotal"), d.research_total, function(v){ return fmtN(v)+" "+(S.lang==="ru"?"ч":"h"); }));
    g.appendChild(el("div",{class:"card wide",style:"overflow:auto"},[el("h3",{},["⚑ "+t("lb_clans")]),
      d.clans.length? ltable(["#",t("cl_name"),t("cl_rating"),t("lb_growth")], d.clans, function(c){
        return [String(d.clans.indexOf(c)+1), c.name, fmtN(c.rating), fmtD(c.growth)]; }) : el("div",{class:"muted small"},["—"]),
      el("p",{class:"muted small"},[t("lb_clans_hint")+(d.clans[0]&&d.clans[0].since? " ("+t("lb_since")+" "+d.clans[0].since+")":"")])]));
    out.appendChild(g);
    out.appendChild(el("p",{class:"muted small",style:"margin-top:8px"},[t("lb_hint")]));
  }).catch(function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
}

// ---- инструменты админа (под паролем панели) ----
function tabAdmin(v){
  function resBox(){ return el("div",{style:"margin-top:8px"},[]); }
  function showRes(box, d){
    box.innerHTML="";
    if(!d.ok){ box.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
    if(d.clan_added && d.clan_added.length) box.appendChild(el("div",{class:"msg ok"},["🛡 "+t("ad_clan_added")+": "+d.clan_added.join(", ")]));
    if(d.clan_error) box.appendChild(el("div",{class:"msg err"},["🛡 "+d.clan_error]));
    if(d.total) box.appendChild(el("div",{class:"msg ok"},["✅ "+d.done+" / "+d.total]));
    var bad=d.results.filter(function(r){ return !r.ok; });
    if(bad.length) box.appendChild(ltable([t("cl_player"),t("ad_reason")], bad, function(r){ return [plLink(r.id,r.name), r.error||"—"]; }));
  }
  function run(body, box){
    box.innerHTML=""; box.appendChild(el("span",{class:"muted small"},[t("working")]));
    gatedApi("/api/admin-tools", body, function(d){ showRes(box,d); }, function(e){ box.innerHTML=""; box.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  // массовая выдача
  ensureItemList();
  var mgT=el("input",{type:"text",placeholder:t("ad_targets_ph"),style:"min-width:260px"});
  var mgI=el("input",{type:"text",list:"mf-itemlist",placeholder:t("ad_item_ph"),style:"min-width:260px"}), mgC=el("input",{type:"number",min:"1",value:"1",style:"width:90px"});
  var mgR=resBox();
  var srvNote=el("div",{class:"small",style:"margin-bottom:8px"},[]);
  api("/api/state").then(function(st){ var run=((st.snapshot||st.last_snapshot||{}).game||st.game||{}).running;
    if(run===false) srvNote.appendChild(el("div",{class:"msg ok"},["🔌 "+t("ad_srv_off")])); }).catch(function(){});
  // техи клану: выбор из списка (клановые — в список клана, обычные — участникам)
  var ctC=el("select",{},[]), ctPick=el("input",{type:"text",list:"ad-techlist",placeholder:t("ad_techs_ph"),style:"min-width:260px"}), ctR=resBox();
  var ctSel=[], ctChips=el("div",{class:"chips",style:"margin-top:6px"},[]), techById={};
  var tdl=el("datalist",{id:"ad-techlist"},[]); document.body.appendChild(tdl);
  api("/api/tech-tree").then(function(tt){ (tt.nodes||[]).slice().sort(function(a,b){ return (b.clan?1:0)-(a.clan?1:0) || String(a.label).localeCompare(String(b.label)); })
    .forEach(function(n){ techById[n.id]=n; tdl.appendChild(el("option",{value:n.id+" — "+n.label+(n.clan? " ["+t("ad_clan_tech")+"]" : "")})); }); }).catch(function(){});
  function drawCt(){ ctChips.innerHTML=""; ctSel.forEach(function(id,i){ var n=techById[id]||{};
    ctChips.appendChild(el("span",{class:"chip"},[(n.clan? "🛡 " : "")+id+" — "+(n.label||""), el("a",{href:"#",style:"margin-left:6px",onclick:function(e){ e.preventDefault(); ctSel.splice(i,1); drawCt(); }},["×"])])); }); }
  ctPick.addEventListener("change",function(){ var id=ctPick.value.split(" — ")[0].trim(); if(id && techById[id] && ctSel.indexOf(id)<0){ ctSel.push(id); drawCt(); } ctPick.value=""; });
  api("/api/clans").then(function(c){ (c.clans||[]).forEach(function(x){ ctC.appendChild(el("option",{value:String(x.id)},[x.name+" ("+x.size+")"])); }); }).catch(function(){});
  // откат
  var rbU=el("input",{type:"number",placeholder:"ID",style:"width:100px"}), rbR=resBox();
  function loadBackups(){
    rbR.innerHTML=""; if(!rbU.value) return;
    gatedApi("/api/admin-tools",{op:"backups",uid:rbU.value},function(d){
      rbR.innerHTML="";
      if(!d.ok){ rbR.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
      if(!d.backups.length){ rbR.appendChild(el("div",{class:"muted small"},[t("ad_no_backups")])); return; }
      rbR.appendChild(ltable([t("ad_when"),t("ad_where"),t("ad_content"),""], d.backups, function(b){
        return [b.dir.replace(/^(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d).*/,"$1-$2-$3 $4:$5:$6"), b.where==="stash"? t("pd_stash") : t("pd_carry"),
          el("span",{class:"small"},[b.items.slice(0,8).map(function(i){ return i.name+" ×"+i.count; }).join(", ")+(b.items.length>8?" …":"")]),
          el("button",{class:"small danger",onclick:function(){
            if(!confirm(t("ad_restore_q"))) return;
            gatedApi("/api/admin-tools",{op:"restore",uid:rbU.value,dir:b.dir,file:b.file},function(r){
              rbR.insertBefore(el("div",{class:r.ok?"msg ok":"msg err"},[r.ok? "✅ "+t("ad_restored")+" ("+t("ad_undo")+" "+r.backup+")" : (r.error||"error")]), rbR.firstChild);
            },function(e){ alert(errText(e)); }); }},[t("ad_restore")])]; }));
    },function(e){ rbR.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  v.appendChild(el("div",{},[
    el("div",{class:"card"},[el("h3",{},[t("ad_title")]), el("p",{class:"muted small"},[t("ad_intro")]), srvNote]),
    el("div",{class:"card",style:"margin-top:12px"},[el("h3",{},["🎁 "+t("ad_mass")]), el("p",{class:"muted small"},[t("ad_mass_hint")]),
      el("div",{class:"row",style:"gap:8px;flex-wrap:wrap;align-items:center"},[mgT, mgI, el("span",{},["×"]), mgC,
        el("button",{class:"small pri",onclick:function(){
          if(!mgI.value.trim()) return;
          var all=!mgT.value.trim();
          if(!confirm(all? t("ad_mass_all_q") : t("ad_mass_q"))) return;
          run({op:"mass_give",targets:all? "all" : mgT.value,item:mgI.value,count:parseInt(mgC.value,10)||1}, mgR); }},[t("ad_give")])]), mgR]),
    el("div",{class:"card",style:"margin-top:12px"},[el("h3",{},["🔬 "+t("ad_clantech")]), el("p",{class:"muted small"},[t("ad_clantech_hint")]),
      el("div",{class:"row",style:"gap:8px;flex-wrap:wrap;align-items:center"},[ctC, ctPick,
        el("button",{class:"small pri",onclick:function(){
          if(!ctSel.length) return;
          if(!confirm(t("ad_clantech_q"))) return;
          run({op:"clan_tech",clan:ctC.value,techs:ctSel.join(" ")}, ctR); }},[t("ad_give")])]), ctChips, ctR]),
    el("div",{class:"card",style:"margin-top:12px"},[el("h3",{},["↩ "+t("ad_rollback")]), el("p",{class:"muted small"},[t("ad_rollback_hint")]),
      el("div",{class:"row",style:"gap:8px;align-items:center"},[rbU, el("button",{class:"small",onclick:loadBackups},[t("ad_show_backups")])]), rbR])
  ]));
}

// ---- кланы ----
var CLAN_SEL=null;
function tabClans(v){
  var out=el("div",{},[el("p",{class:"muted"},["…"])]);
  v.appendChild(out);
  if(CLAN_SEL!=null) return clanDetail(out, CLAN_SEL);
  api("/api/clans").then(function(d){
    out.innerHTML="";
    if(!d.ok){ out.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
    if(!d.clans.length){ out.appendChild(el("div",{class:"muted"},[t("cl_none")])); return; }
    var tb=el("table",{},[el("tr",{},["#",t("cl_name"),t("cl_size"),t("cl_online"),t("cl_rating"),"CP",t("cl_leader"),t("cl_ctech"),t("cl_trade")]
      .map(function(x){ return el("th",{},[x]); }))]);
    d.clans.forEach(function(c,i){
      tb.appendChild(el("tr",{},[
        el("td",{},[String(i+1)]),
        el("td",{},[el("a",{class:"pl-link",onclick:function(){ CLAN_SEL=c.id; routeTab(); }},[c.name])]),
        el("td",{},[c.size+" / "+(c.max!=null?c.max:"?")]),
        el("td",{},[c.online? el("span",{class:"pill ok"},[String(c.online)]) : "0"]),
        el("td",{},[c.rating!=null? Number(c.rating).toLocaleString() : "—"]),
        el("td",{},[c.clan_point!=null? String(c.clan_point) : "—"]),
        el("td",{},[c.leader? plLink(c.leader.id,c.leader.name) : "—"]),
        el("td",{},[String(c.tech_count)]),
        el("td",{},[c.trading? "✓" : "—"])
      ]));
    });
    out.appendChild(el("div",{class:"card"},[el("h3",{},[t("clans")+" · "+d.clans.length]), el("p",{class:"muted small"},[t("cl_intro")]), tb]));
  }).catch(function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
}
function clanDetail(out, cid){
  api("/api/clans?id="+cid).then(function(d){
    out.innerHTML="";
    var back=el("div",{class:"row",style:"margin-bottom:10px"},[
      el("button",{class:"small",onclick:function(){ CLAN_SEL=null; routeTab(); }},["← "+t("cl_back")])]);
    out.appendChild(back);
    if(!d.ok){ out.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
    var hh=S.lang==="ru"?"ч":"h";
    var g=el("div",{class:"grid"},[]);
    g.appendChild(kvcard(d.name,[
      [t("cl_rating"), d.rating!=null? Number(d.rating).toLocaleString() : "—"],
      ["Clan Points", d.clan_point!=null? d.clan_point : "—"],
      [t("cl_size"), d.size+" / "+(d.max!=null?d.max:"?")],
      [t("cl_online"), d.online],
      [t("cl_trade"), d.trading? "✓" : "—"],
      [t("cl_ctech"), d.tech_named.length? el("div",{class:"chips"}, d.tech_named.map(function(x){ return el("span",{class:"chip"},[x.label]); })) : "—"]
    ]));
    // матрица специализаций: строка = позитивная, столбец = негативная
    var types=[], seen={};
    d.slots.forEach(function(s){ if(!seen[s.positive]){ seen[s.positive]=1; types.push(s.positive); } });
    var mt=el("table",{class:"small"},[el("tr",{},[el("th",{},["+ \\ −"])].concat(types.map(function(x){ return el("th",{},[x]); })))]);
    types.forEach(function(pt){
      mt.appendChild(el("tr",{},[el("th",{},[pt])].concat(types.map(function(nt){
        var s=d.slots.filter(function(z){ return z.positive===pt && z.negative===nt; })[0];
        if(!s || s.blocked) return el("td",{style:"background:var(--line);opacity:.35"},[""]);
        return el("td",{},[s.user? plLink(s.user.id,s.user.name) : el("span",{class:"muted"},["·"])]); }))));
    });
    g.appendChild(el("div",{class:"card",style:"overflow:auto"},[el("h3",{},[t("cl_spec")]), el("p",{class:"muted small"},[t("cl_spec_hint")]), mt]));
    out.appendChild(g);
    out.appendChild(clanHistoryCard(cid));

    var rows=d.members.map(function(m){ return [
      el("span",{},[m.online? el("span",{class:"dot ok",style:"margin-right:5px"}) : null, plLink(m.id,m.name)]),
      m.role_name, m.level!=null? String(m.level) : "—", String(m.playtime_h),
      m.last_seen_h!=null? (m.online? t("cl_now") : m.last_seen_h+" "+hh) : "—",
      String(m.tech_count), "~"+m.research_h+" "+hh, m.researching||"—",
      m.spec? ("+"+m.spec.positive+" / −"+m.spec.negative) : "—",
      m.rating!=null? Number(m.rating).toLocaleString() : "—", m.clan_point!=null? String(m.clan_point) : "—" ]; });
    out.appendChild(el("div",{class:"card",style:"margin-top:12px;overflow:auto"},[el("h3",{},[t("cl_members")+" · "+d.members.length]),
      ltable([t("cl_player"),t("cl_role"),t("pd_level"),t("pd_playtime"),t("pd_last_seen"),t("cl_techs"),t("st_resh"),t("pd_res_cur"),t("cl_spec"),t("cl_rating"),"CP"],
        d.members, function(m){ return rows[d.members.indexOf(m)]; })]));

    out.appendChild(techSchemeCard(t("cl_ctech_scheme"), function(nodes){
      return techScheme(nodes,{filter:function(n){ return n.clan; }, done:setOf(d.tech)}); }));

    // личные технологии: покрытие по клану или схема одного участника
    var sel=el("select",{},[el("option",{value:""},[t("cl_cov_all")])].concat(d.members.map(function(m,i){
      return el("option",{value:String(i)},[m.name+" ("+m.tech_count+")"]); })));
    var holder=el("div",{},[]);
    function draw(nodes){
      holder.innerHTML="";
      var personal=function(n){ return !n.clan; };
      if(sel.value===""){
        holder.appendChild(schemeWithPlanner(nodes,{filter:personal, coverage:d.coverage, total:d.members.length}, clanPlanInfo(d.members)));
      } else {
        var dn=setOf(d.members[+sel.value].techs);
        holder.appendChild(schemeWithPlanner(nodes,{filter:personal, done:dn}, playerPlanInfo(dn)));
      }
    }
    var card=techSchemeCard(t("cl_ptech_scheme"), function(nodes){
      sel.addEventListener("change",function(){ draw(nodes); }); draw(nodes);
      return el("div",{},[el("div",{class:"row",style:"gap:8px;margin-bottom:8px"},[sel]), holder]); });
    out.appendChild(card);
  }).catch(function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
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
    var hlName=j.highlight||"astralsigma";
    var rows=j.servers.slice().sort(function(a,b){
      var ah=(a.name||"").toLowerCase().replace(/ /g,"").indexOf(hlName)!==-1;
      var bh=(b.name||"").toLowerCase().replace(/ /g,"").indexOf(hlName)!==-1;
      if(ah!==bh) return ah?-1:1; return (b.players||0)-(a.players||0);
    });
    var tb=el("table",{},[ el("tr",{},[t("col_name"),t("col_players"),t("col_map"),t("col_ver"),t("col_addr"),t("col_mem")].map(function(x){return el("th",{},[x]);})) ]);
    rows.forEach(function(s){
      var hl=(s.name||"").toLowerCase().replace(/ /g,"").indexOf(hlName)!==-1;
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
    pfFindCard(),
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
    [t("pl_online"), el("span",{},[pill(true,String(tt.online_analytics||0)),
      tt.stale_online? el("span",{class:"muted small",style:"margin-left:6px",
        title:t("pl_stale_hint")},["("+t("pl_stale")+" "+tt.stale_online+")"]) : null].filter(Boolean))],
    [t("pl_online_gs"), el("span",{title:tt.online_space? t("pl_space_note"):null},[String(tt.online_game_state||0)+(tt.online_space? "  (космос "+tt.online_space+" ⚠)":"")])]
  ]));
  var bm=(j.by_map||[]);
  sum.appendChild(card(t("pl_bymap"), bm.length? bm.map(function(m){ return [mapName(m.map), String(m.count)]; })
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
      el("td",{class:"small"},[u.map!=null? mapName(u.map) : "—"]),
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
function ttLine(e, noname){
  var who = noname? "" : ""; // имя добавляется вызывающим для server-wide
  var body;
  if(e.kind==="tech_gained") body=[el("span",{class:"chip"},[t("tt_gained")]), " "+(e.techs||[]).join(", ")+" (Σ"+(e.total||"?")+")"];
  else if(e.kind==="booster_spent") body=[el("span",{class:"chip warn"},[t("tt_spent")]), " ×"+e.delta+" → "+e.left];
  else if(e.kind==="booster_gained") body=[el("span",{class:"chip"},[t("tt_bgain")]), " +"+e.delta+" = "+e.total];
  else if(e.kind==="map_changed") body=[el("span",{class:e.space?"chip warn":"chip"},[t("tt_map")]), " "+(e.from===0?"космос":e.from)+" → "+(e.to===0?"космос":e.to)];
  else body=[el("span",{class:"chip"},[t("tt_reschg")]), " "+(e.from||"—")+" → "+e.to];
  var pre=[el("span",{class:"lg-t"},[fshort(e.ts)+" "])];
  if(!noname) pre.push(plLink(e.uid, e.name), " ");
  return el("span",{},pre.concat(body));
}

// ---- player detail modal ----
var pdCurId=null;
function openPlayer(id){
  closePlayer();                 // не плодить модалки при клике из открытой карточки
  pdCurId=String(id);
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
  api("/api/players/"+id).then(function(d){ if(String(id)===pdCurId) renderPlayerModal(d); }).catch(function(e){
    var b=$("#pd-body"); if(b){ b.innerHTML=""; b.appendChild(el("div",{class:"msg err"},[errText(e)])); }
  });
}
function pdEsc(e){ if(e.key==="Escape") closePlayer(); }
function closePlayer(){
  pdCurId=null;
  var os=document.querySelectorAll(".ovl"); for(var i=0;i<os.length;i++) os[i].remove();
  document.removeEventListener("keydown",pdEsc);
}
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
    [t("pd_res_cur"), r.current_name||r.current||"—"],
    r.remaining_min!=null? [t("pd_res_left"), r.remaining_min+" "+t("pd_min")] : null,
    [t("pd_res_done"), r.done_count],
    r.invested_h!=null? [t("st_resh"), "~"+r.invested_h+" "+(S.lang==="ru"?"ч":"h")] : null,
    r.booster!=null? [t("pd_booster"), r.booster] : null
  ].concat([ [t("pd_missions"), (mi.current!=null? mi.current : "—")+(mi.month!=null? " ("+t("pd_mission_month")+" "+mi.month+")":"")] ])));

  var terr=(po.territories||[]);
  g.appendChild(kvcard(t("pd_position"),[
    [t("pl_col_map"), po.map!=null? mapFull(po.map) : "—"],
    [t("pd_coords"), (po.x!=null? po.x+", "+po.y : "—")],
    po.respawn? [t("pd_respawn"), mapName(po.respawn.map)+" · "+po.respawn.x+", "+po.respawn.y] : null,
    [t("pd_territories"), terr.length? String(terr.length) : "—"]
  ]));
  // участки: по картам с названиями, в прокрутке (у крупных игроков их сотни)
  if(terr.length){
    var byMap={}; terr.forEach(function(tt){ (byMap[tt.map]=byMap[tt.map]||[]).push(tt); });
    var mapsT=Object.keys(byMap).sort(function(a,b){ return byMap[b].length-byMap[a].length; });
    g.appendChild(el("div",{class:"card"},[el("h3",{},[t("pd_territories")+" · "+terr.length+" · "+t("pd_terr_maps")+" "+mapsT.length]),
      el("div",{style:"max-height:260px;overflow:auto"},[ltable([t("pl_col_map"),t("pd_terr_n"),t("pd_coords")], mapsT, function(m){
        return [el("a",{class:"pl-link",onclick:function(){ openMapdt(+m); }},[mapFull(m)]), String(byMap[m].length),
          el("span",{class:"small mono"},[byMap[m].slice(0,60).map(function(tt){ return tt.x+","+tt.y; }).join("  ")+(byMap[m].length>60? " …" : "")])]; })])]));
  }

  // paramList/skillLevels.type и long_params.type — enum UnitParamType /
  // UnitParamTypeLong, вытащены 2026-09-17 из живого дампа игры (Il2CppDumper
  // по GameAssembly.dll+global-metadata.dat, класс ZData.UnitParam/
  // UnitSkillLevel/UnitParamLong): 0=energy,1=satiety,2=health,3=accuracy,
  // 4=speedMove,5=speedAction,6=speedAttack,7=genA,8=genB,9=genC,10=genD,
  // 11=oxygen,12=genetic; long: 0=exp,1=level,2=distributionPoints. Раньше
  // (до дампа) тип 3 считался "Стаминой" на глаз, а 4 — "Меткостью" методом
  // исключения — оба были неверны, см. [[sigma-swo-stat-skill-ids]].
  // val==valMax всегда для типов 3-6 (это не депл. ресурс, а растущий
  // навыком стат/множитель) — 0-3 рисуем полосой, 4-6 — как "+X%".
  var PBL={0:"pd_p0",1:"pd_p1",2:"pd_p2",3:"pd_p3",4:"pd_p4",5:"pd_p5",6:"pd_p6",
           7:"pd_p7",8:"pd_p8",9:"pd_p9",10:"pd_p10",11:"pd_p11",12:"pd_p12"};
  var LPL={0:"pd_lp0",1:"pd_lp1",2:"pd_lp2"};
  var BAR_TYPES={0:1,1:1,2:1,3:1}, BONUS_TYPES={4:1,5:1,6:1};
  var params=(av.params||[]).filter(function(pp){ return pp.max>1; }).map(function(pp){
    var lbl=PBL[pp.type]? t(PBL[pp.type]) : (t("pd_skill_pfx")+" #"+pp.type);
    if(BAR_TYPES[pp.type]){
      var pct=Math.max(0,Math.min(100, 100*pp.val/pp.max));
      return [lbl, el("div",{class:"bar",title:pp.val+" / "+pp.max},[
        el("span",{style:"width:"+pct+"%"},[]), el("b",{},[Math.round(pp.val)+" / "+Math.round(pp.max)])])];
    }
    if(BONUS_TYPES[pp.type]){
      var pctBonus=Math.round((pp.val-1)*100);
      return [lbl, el("span",{class:"chip"},["+"+pctBonus+"%"])];
    }
    return [lbl, el("span",{class:"chip",title:t("pd_skill_hint")},[String(pp.val)])];
  });
  var lps=(av.long_params||[]).map(function(pp){
    var l=(LPL[pp.type] && t(LPL[pp.type])) || ("L"+pp.type); return [l, String(pp.val)]; });
  g.appendChild(kvcard(t("pd_avatar"), params.concat(lps).concat([
    [t("pd_skills"), (av.skills&&av.skills.length)? el("div",{class:"chips"}, av.skills.map(function(sk){
      var nameKey=PBL[sk.type], lbl=nameKey? t(nameKey) : (t("pd_skill_pfx")+" #"+sk.type);
      return el("span",{class:"chip",title:nameKey?"":t("pd_skill_hint")},[lbl+": "+sk.val]); })) : "—"],
    [t("pd_abilities"), (av.abilities&&av.abilities.length)? el("div",{class:"chips"}, av.abilities.map(function(a){
      return el("span",{class:"chip"},[a]); })) : "—"],
    [t("pd_buffs"), av.buffs||0]
  ])));

  var canEdit = d.online===false;
  function pdWrite(url, extra, msgEl){
    if(msgEl) msgEl.textContent="…";
    gatedApi(url, extra,
      function(){ if(msgEl) msgEl.textContent="✅"; api("/api/players/"+d.id).then(renderPlayerModal); },
      function(e){ if(msgEl) msgEl.textContent=errText(e); });
  }
  function pdInvOp(op, where, item, count, msgEl){
    return pdWrite("/api/players/"+d.id+"/inventory", {op:op,where:where,item:item,count:count}, msgEl);
  }
  function pdMod(action, params, msgEl){
    return pdWrite("/api/players/"+d.id+"/moderate", Object.assign({action:action}, params||{}), msgEl);
  }
  function invCap(where){
    if(where==="carry"){
      if(av.carry_limited && av.carry_size!=null){
        var extra = av.carry_size>20? " (+"+(av.carry_size-20)+" к базе 20)" : "";
        return " / "+av.carry_size+extra;
      }
      return "";
    }
    return av.stash_limited? (av.stash_size!=null? " / "+av.stash_size : "") : " · ∞";
  }
  function invCard(title, list, where, cnt){
    var head=el("h3",{},[title+" · "+(list?list.length:(cnt||0))+invCap(where)]);
    if(!list || !list.length) return el("div",{class:"card"},[head, el("div",{class:"muted small"},[(cnt||0)+" "+t("pd_items")+invCap(where)])]);
    var rows=list.map(function(it){
      var tds=[el("td",{title:it.name},[it.label||it.name]), el("td",{class:"mono"},[String(it.count!=null?it.count:"")]),
               el("td",{class:"mono muted"},[it.durability!=null? String(it.durability):"—"])];
      if(canEdit) tds.push(el("td",{},[el("button",{class:"small danger",title:t("pd_inv_take"),onclick:function(){
        var n=parseInt(window.prompt(t("pd_inv_take")+" "+it.name+" ×", String(it.count||1)),10);
        if(n>0) pdInvOp("take", where, it.id, n, null);
      }},["–"])]));
      return el("tr",{},tds);
    });
    var hd=[t("sp_item"),"×","dur"]; if(canEdit) hd.push("");
    var tbl=el("table",{}, [el("tr",{},hd.map(function(x){return el("th",{style:"position:sticky;top:0;background:var(--panel)"},[x]);}))].concat(rows));
    return el("div",{class:"card"},[head, el("div",{style:"max-height:360px;overflow:auto"},[tbl])]);
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

  g.appendChild(el("div",{class:"card"},[
    el("h3",{},[t("pd_sessions")]),
    el("div",{class:"kv"},[el("span",{},[t("pd_sess_total")]),el("b",{},[String(s.total||0)])]),
    el("div",{class:"kv"},[el("span",{},[t("pd_sess_hours")]),el("b",{},[String(s.total_h||0)])]),
    el("div",{class:"kv"},[el("span",{},[t("pd_sess_avg")+" / "+t("pd_sess_max")]),el("b",{},[(s.avg_min||0)+" / "+(s.max_min||0)+" "+t("pd_min")])]),
    el("div",{class:"muted small",style:"margin:8px 0 3px"},[t("pd_sess_recent")]),
    el("div",{class:"mono small",style:"max-height:150px;overflow:auto"}, (s.recent||[]).map(function(x){
      return el("div",{},[fshort(x.enter)+" → "+(x.exit? fshort(x.exit):"…")+"  "+(x.secs? fdur(x.secs):"")]); }))
  ]));
  g.appendChild(bigChart(t("pd_sess_byhour"), "bar",
    (s.by_hour||[]).map(function(_,i){ return i+"ч"; }),
    [{name:t("pd_sess_total"), unit:"вх.", data:(s.by_hour||[]), color:CHART_COL[0]}]));

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
  if((ac.land_deletions||[]).length) acRows.push([t("pd_lands"), ac.land_deletions.slice(0,8).map(function(x){return mapName(x.map)+" · "+x.x+","+x.y;}).join("  ")]);
  if((ac.rewards||[]).length) acRows.push([t("pd_rewards"), ac.rewards.map(function(x){return fshort(x.ts).slice(0,5)+":"+x.reward;}).join("  ")]);
  if(acRows.length) g.appendChild(kvcard(t("pd_activity"), acRows));

  var ttCard=el("div",{class:"card"},[el("h3",{},[t("tt_title")]), el("div",{class:"muted small"},["…"])]);
  g.appendChild(ttCard);
  api("/api/tech-track?limit=1500&uid="+d.id).then(function(tj){
    ttCard.innerHTML=""; ttCard.appendChild(el("h3",{},[t("tt_title")+(tj.total!=null?" · "+tj.total:"")]));
    if(!tj.ok || !tj.events || !tj.events.length){ ttCard.appendChild(el("div",{class:"muted small"},[t("tt_none")])); return; }
    var days=[], techByDay={}, boostByDay={};
    for(var i=13;i>=0;i--){
      var k=new Date(Date.now()-i*86400000).toISOString().slice(0,10);
      days.push(k); techByDay[k]=0; boostByDay[k]=0;
    }
    tj.events.forEach(function(e){
      var k=(e.ts||"").slice(0,10);
      if(!(k in techByDay)) return;
      if(e.kind==="tech_gained") techByDay[k]+=(e.count||0);
      else if(e.kind==="booster_spent") boostByDay[k]+=(e.delta||0);
    });
    ttCard.appendChild(bigChart(t("tt_chart_title"), "bar", days.map(function(k){ return k.slice(5); }),
      [{name:t("tt_chart_tech"), unit:"", data:days.map(function(k){ return techByDay[k]; }), color:CHART_COL[1]},
       {name:t("tt_chart_boost"), unit:"", data:days.map(function(k){ return boostByDay[k]; }), color:CHART_COL[2]}]));
    var box=el("div",{class:"mono small",style:"max-height:200px;overflow:auto;margin-top:8px"},[]);
    tj.events.slice(0,80).forEach(function(e){ box.appendChild(el("div",{},[ttLine(e,true)])); });
    ttCard.appendChild(box);
  }).catch(function(){ ttCard.querySelector(".muted").textContent=t("err_net"); });

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

  b.appendChild(techSchemeCard(t("pd_tech_scheme"), function(nodes){
    var dn=setOf(r.tech_list);
    return schemeWithPlanner(nodes,{filter:function(n){ return !n.clan; }, done:dn, current:r.current}, playerPlanInfo(dn)); }));

  b.appendChild(pdToolsCard(d));

  // sensitive blocks (each behind admin password)
  function gate(box, url, render){
    box.innerHTML=""; box.appendChild(el("p",{class:"muted small"},["…"]));
    gatedApi(url, {}, function(res){ box.innerHTML=""; render(box,res); },
      function(e){ box.innerHTML=""; box.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  var secBox=el("div",{class:"card",style:"margin-top:12px"},[]);
  var btnRow=el("div",{class:"row"},[
    el("button",{class:"small danger",onclick:function(){
      gate(secBox, "/api/players/"+d.id+"/secret", function(box,res){
        box.appendChild(el("div",{class:"kv"},[el("span",{},["code"]),el("b",{class:"mono"},[res.code||"—"])]));
        box.appendChild(btnRow);
      });
    }},[t("pd_show_code")]),
    el("button",{class:"small danger",onclick:function(){
      gate(secBox, "/api/players/"+d.id+"/sensitive", function(box,res){
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
var CHART_COL=["#4c8dff","#3fb950","#d29922","#f85149"];
// bigChart(title, kind:"line"|"bar", labels:[str], series:[{name,unit,data:[num],color}], subtitle?)
function bigChart(title, kind, labels, series, subtitle){
  var n=Math.max(1, labels.length);
  var W=640,H=200,mL=40,mR=8,mT=8,mB=20, pw=W-mL-mR, ph=H-mT-mB;
  var allv=[]; series.forEach(function(s){ (s.data||[]).forEach(function(v){ if(v!=null&&isFinite(v)) allv.push(v); }); });
  var mx=Math.max.apply(null,allv.concat([1])); if(!(mx>0)) mx=1;
  var Yc=function(v){ return mT+ph-(Math.max(0,v||0)/mx)*ph; };
  var Xc=function(i){ return mL+(n<=1? pw/2 : i/(n-1)*pw); };
  var s='<svg viewBox="0 0 '+W+' '+H+'">';
  [0,0.25,0.5,0.75,1].forEach(function(f){ var y=mT+ph-f*ph;
    s+='<line class="cg" x1="'+mL+'" y1="'+y.toFixed(1)+'" x2="'+(W-mR)+'" y2="'+y.toFixed(1)+'"/>';
    s+='<text class="cax" x="'+(mL-4)+'" y="'+(y+3).toFixed(1)+'" text-anchor="end">'+Math.round(mx*f)+'</text>'; });
  var step=Math.max(1,Math.ceil(n/7));
  for(var i=0;i<n;i+=step) s+='<text class="cax" x="'+Xc(i).toFixed(1)+'" y="'+(H-6)+'" text-anchor="middle">'+String(labels[i])+'</text>';
  series.forEach(function(ser,si){
    var col=ser.color||CHART_COL[si%4], data=ser.data||[];
    if(kind==="line"){
      var pts=data.map(function(v,i){ return Xc(i).toFixed(1)+","+Yc(v).toFixed(1); }).join(" ");
      s+='<polyline points="'+mL+','+(mT+ph)+' '+pts+' '+(mL+pw)+','+(mT+ph)+'" fill="'+col+'" fill-opacity="0.12" stroke="none"/>';
      s+='<polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="1.6"/>';
    } else {
      var bw=pw/n*0.72/series.length;
      data.forEach(function(v,i){ var x0=mL+(i+0.14)/n*pw+si*bw, y=Yc(v);
        s+='<rect x="'+x0.toFixed(1)+'" y="'+y.toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+(mT+ph-y).toFixed(1)+'" fill="'+col+'" fill-opacity="0.85"/>'; });
    }
  });
  s+='<line class="cguide" x1="0" y1="'+mT+'" x2="0" y2="'+(mT+ph)+'"/></svg>';
  var chart=el("div",{class:"chart",html:s});
  var tip=el("div",{class:"ctip"},[]); chart.appendChild(tip);
  var svg=chart.querySelector("svg"), guide=chart.querySelector(".cguide");
  chart.addEventListener("mousemove",function(ev){
    var r=svg.getBoundingClientRect(); if(!r.width) return;
    var vx=(ev.clientX-r.left)/r.width*W;
    var idx=Math.round((vx-mL)/pw*(n-1));
    if(idx<0||idx>=n||n<2){ tip.style.opacity=0; guide.style.opacity=0; return; }
    var gx=Xc(idx);
    guide.setAttribute("x1",gx); guide.setAttribute("x2",gx); guide.style.opacity=1;
    tip.style.left=(gx/W*r.width)+"px"; tip.style.top=((mT+4)/H*r.height)+"px"; tip.style.opacity=1;
    tip.textContent=labels[idx]+" — "+series.map(function(ss){ var v=(ss.data||[])[idx]; return (v!=null?v:"—")+" "+ss.unit; }).join(" · ");
  });
  chart.addEventListener("mouseleave",function(){ tip.style.opacity=0; guide.style.opacity=0; });
  var legend=el("div",{class:"chart-legend"}, series.map(function(ss,si){
    return el("span",{},[el("b",{style:"background:"+(ss.color||CHART_COL[si%4])},[]), ss.name+" ("+ss.unit+")"]); }));
  return el("div",{class:"card wide"},[ el("h3",{},[title]),
    subtitle? el("div",{class:"row",style:"margin-bottom:4px"},[].concat(subtitle)) : null,
    legend, chart ].filter(Boolean));
}
// ---- нагрузка сервера и панели ----
var ldTimer=null, LD_RANGE=(function(){ try{ return localStorage.getItem("sw_ldrange")||"6h"; }catch(e){ return "6h"; } })();
function ldNum(v,d){ if(v==null||isNaN(v)) return "—"; d=d==null?1:d; var a=Math.abs(v);
  if(a>0 && a<1) return String(Number(v.toPrecision(2)));        // 0.066 МБ/с — не «0»
  return a>=1000? Math.round(v).toLocaleString() : a>=100? String(Math.round(v)) : String(Math.round(v*Math.pow(10,d))/Math.pow(10,d)); }
function ldNice(v){ if(v<=0) return 1; var p=Math.pow(10,Math.floor(Math.log10(v))), n=v/p; return (n<=1?1:n<=2?2:n<=2.5?2.5:n<=5?5:10)*p; }
// Линейный график: одна ось, до 3 серий (цвета --s1..--s3), легенда с текущим значением, перекрестие + подсказка (среднее и максимум).
function mchart(title, unit, ser, o){
  o=o||{};
  var W=560,H=180,ML=44,MR=10,MT=8,MB=22;
  var box=el("div",{class:"card mc"},[el("div",{class:"mc-h"},[el("b",{},[title]), unit? el("span",{class:"muted small"},["  · "+unit]) : null])]);
  ser=ser.filter(function(s){ return s.data && s.data.length; });
  if(!ser.length){ box.appendChild(el("div",{class:"muted small",style:"padding:28px 0"},[t("ld_nodata")])); return box; }
  var t0=Infinity,t1=-Infinity,hi=0;
  ser.forEach(function(s){ s.data.forEach(function(p){ if(p[0]<t0)t0=p[0]; if(p[0]>t1)t1=p[0]; if(p[1]>hi)hi=p[1]; }); });
  if(o.min!=null) hi=Math.max(hi,o.min);
  hi=hi>0? ldNice(hi*1.08) : 1; if(t1===t0) t1=t0+1;
  var step=o.step||60;
  function X(tt){ return ML+(W-ML-MR)*(tt-t0)/(t1-t0); } function Y(v){ return MT+(H-MT-MB)*(1-v/hi); }
  var leg=el("div",{class:"mc-leg"},[]);
  ser.forEach(function(s,i){ var last=s.data[s.data.length-1];
    leg.appendChild(el("span",{},[el("i",{style:"background:var(--s"+(i+1)+")"}), s.name+"  ", el("b",{},[ldNum(last[1])])])); });
  if(ser.length>1 || o.legend) box.appendChild(leg);
  else box.appendChild(el("div",{class:"mc-leg"},[el("span",{},[el("b",{},[ldNum(ser[0].data[ser[0].data.length-1][1])+" "+(unit||"")])])]));
  var kids=[];
  [0,hi/2,hi].forEach(function(v){ kids.push(svgEl("line",{x1:ML,x2:W-MR,y1:Y(v),y2:Y(v),stroke:"var(--line)","stroke-width":"1"}));
    kids.push(svgEl("text",{x:ML-6,y:Y(v)+4,"text-anchor":"end","font-size":"11",fill:"var(--mut)"},[ldNum(v)])); });
  var span=t1-t0, long=span>2*86400;
  for(var k=0;k<=4;k++){ var tt=t0+span*k/4, d=new Date(tt*1000);
    var lab=long? (("0"+d.getDate()).slice(-2)+"."+("0"+(d.getMonth()+1)).slice(-2)) : (("0"+d.getHours()).slice(-2)+":"+("0"+d.getMinutes()).slice(-2));
    kids.push(svgEl("text",{x:X(tt),y:H-6,"text-anchor":k===0?"start":k===4?"end":"middle","font-size":"11",fill:"var(--mut)"},[lab])); }
  ser.forEach(function(s,i){
    var d="", prev=null;
    s.data.forEach(function(p){ d+=((prev==null||p[0]-prev>step*3)?"M":"L")+X(p[0]).toFixed(1)+" "+Y(p[1]).toFixed(1)+" "; prev=p[0]; });
    kids.push(svgEl("path",{d:d,fill:"none",stroke:"var(--s"+(i+1)+")","stroke-width":"2","stroke-linejoin":"round","stroke-linecap":"round"}));
  });
  var cross=svgEl("line",{x1:0,x2:0,y1:MT,y2:H-MB,stroke:"var(--mut)","stroke-width":"1","stroke-dasharray":"3 3",visibility:"hidden"});
  var dots=ser.map(function(s,i){ return svgEl("circle",{r:"4",fill:"var(--s"+(i+1)+")",stroke:"var(--panel)","stroke-width":"2",visibility:"hidden"}); });
  kids.push(cross); dots.forEach(function(c){ kids.push(c); });
  var hit=svgEl("rect",{x:ML,y:MT,width:W-ML-MR,height:H-MT-MB,fill:"transparent"});
  kids.push(hit);
  var svg=svgEl("svg",{viewBox:"0 0 "+W+" "+H,style:"width:100%;height:auto;display:block"},kids);
  var tip=el("div",{class:"mc-tip"},[]);
  function near(s,tt){ var best=null; s.data.forEach(function(p){ if(!best||Math.abs(p[0]-tt)<Math.abs(best[0]-tt)) best=p; }); return best; }
  hit.addEventListener("mousemove",function(e){
    var r=svg.getBoundingClientRect(), x=(e.clientX-r.left)*W/r.width, tt=t0+(x-ML)/(W-ML-MR)*(t1-t0);
    var ref=near(ser[0],tt); if(!ref) return;
    cross.setAttribute("x1",X(ref[0])); cross.setAttribute("x2",X(ref[0])); cross.setAttribute("visibility","visible");
    tip.innerHTML=""; var dt=new Date(ref[0]*1000);
    tip.appendChild(el("div",{class:"muted"},[dt.toLocaleString([], {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"})]));
    ser.forEach(function(s,i){ var p=near(s,ref[0]); if(!p) return;
      dots[i].setAttribute("cx",X(p[0])); dots[i].setAttribute("cy",Y(p[1])); dots[i].setAttribute("visibility","visible");
      tip.appendChild(el("div",{},[el("i",{style:"background:var(--s"+(i+1)+")"}), s.name+": ", el("b",{},[ldNum(p[1])]),
        el("span",{class:"muted"},["  ("+t("ld_max")+" "+ldNum(p[2])+")"])])); });
    tip.style.display="block";
    var bx=box.getBoundingClientRect(), left=e.clientX-bx.left+14;
    if(left+tip.offsetWidth>bx.width-6) left=e.clientX-bx.left-tip.offsetWidth-14;
    tip.style.left=Math.max(4,left)+"px"; tip.style.top=(e.clientY-bx.top+10)+"px";
  });
  hit.addEventListener("mouseleave",function(){ tip.style.display="none"; cross.setAttribute("visibility","hidden");
    dots.forEach(function(c){ c.setAttribute("visibility","hidden"); }); });
  box.appendChild(svg); box.appendChild(tip);
  return box;
}
function tabLoad(v){
  var head=el("div",{class:"card"},[]), tiles=el("div",{class:"ld-tiles"},[]), procs=el("div",{},[]), grid=el("div",{class:"ld-grid"},[]);
  var auto=el("input",{type:"checkbox",checked:"checked"});
  var bar=el("div",{class:"row",style:"gap:6px;flex-wrap:wrap;align-items:center"},[]);
  function btns(){ bar.innerHTML="";
    [["1h","1 ч"],["6h","6 ч"],["24h","24 ч"],["7d","7 д"],["30d","30 д"]].forEach(function(r){
      bar.appendChild(el("button",{class:"small"+(LD_RANGE===r[0]?" pri":""),onclick:function(){ LD_RANGE=r[0]; try{ localStorage.setItem("sw_ldrange",r[0]); }catch(e){} btns(); load(); }},[r[1]])); });
    bar.appendChild(el("label",{class:"small",style:"margin-left:8px"},[auto," "+t("ld_auto")])); }
  var intro=el("p",{class:"muted small",style:"margin:0 0 8px"},[]);
  head.appendChild(el("h3",{},[t("load")])); head.appendChild(intro); head.appendChild(bar);
  v.appendChild(head); v.appendChild(tiles); v.appendChild(procs); v.appendChild(grid);
  function tile(label, val, sub){ return el("div",{class:"ld-tile"},[el("div",{class:"s"},[label]), el("div",{class:"v"},[val]), sub? el("div",{class:"s"},[sub]) : null]); }
  function S_(d,key,name,k){ k=k||1; return {name:name, data:(d.series[key]||[]).map(function(p){ return [p[0],p[1]*k,p[2]*k]; })}; }
  function load(){
    api("/api/metrics?range="+LD_RANGE).then(function(d){
      intro.textContent=t("ld_intro").replace("{n}",d.interval).replace("{c}",d.cores||"?");
      var n=d.now||{}; tiles.innerHTML="";
      tiles.appendChild(tile(t("ld_t_cpu"), ldNum(n.cpu)+" %", t("ld_t_core")+" "+ldNum(n.cpu_max)+" %"));
      tiles.appendChild(tile(t("ld_t_ram"), ldNum(n.ram_pct)+" %", n.ram_used!=null? ldNum(n.ram_used/1024)+" / "+ldNum(n.ram_total/1024)+" "+t("ld_gb") : ""));
      tiles.appendChild(tile(t("ld_t_iops"), ldNum(n.d_r_iops,0)+" / "+ldNum(n.d_w_iops,0), t("ld_read")+" / "+t("ld_write")+", "+t("ld_ops")));
      tiles.appendChild(tile(t("ld_t_disk"), ldNum(n.d_r_mbs,2)+" / "+ldNum(n.d_w_mbs,2), t("ld_read")+" / "+t("ld_write")+", "+t("ld_mbs")));
      tiles.appendChild(tile(t("ld_t_net"), ldNum(n.n_in_mbs,2)+" / "+ldNum(n.n_out_mbs,2), t("ld_in")+" / "+t("ld_out")+", "+t("ld_mbs")));
      tiles.appendChild(tile(t("ld_t_free"), n.disk_free_pct!=null? ldNum(n.disk_free_pct)+" %" : "—", n.disk_free_gb!=null? ldNum(n.disk_free_gb)+" "+t("ld_gb") : ""));
      tiles.appendChild(tile(t("ld_t_online"), n.online!=null? String(n.online) : "—", t("ld_players")));
      procs.innerHTML="";
      var pr=[["g",t("ld_game")],["s",t("ld_steam")],["p",t("ld_panel")]];
      procs.appendChild(el("div",{class:"card",style:"margin-bottom:12px"},[el("h3",{},[t("ld_proc")]),
        scT(ltable([t("ld_col_proc"),t("ld_col_cpu"),t("ld_col_ram"),t("ld_col_iops"),t("ld_col_mbs"),t("ld_col_thr"),t("ld_col_h")], pr, function(x){
          var k=x[0]; if(n[k+"_rss"]==null) return [x[1], el("span",{class:"muted"},[t("ld_notrun")]),"","","","",""];
          return [x[1], ldNum(n[k+"_cpu"])+" %", ldNum(n[k+"_rss"]/1024,2)+" "+t("ld_gb"), ldNum(n[k+"_r_iops"],0)+" / "+ldNum(n[k+"_w_iops"],0),
            ldNum(n[k+"_r_mbs"],2)+" / "+ldNum(n[k+"_w_mbs"],2), n[k+"_thr"]!=null? String(n[k+"_thr"]) : "—", n[k+"_h"]!=null? String(n[k+"_h"]) : "—"]; }))]));
      grid.innerHTML=""; var o={step:d.step};
      grid.appendChild(mchart(t("ld_cpu"),"%",[S_(d,"cpu",t("ld_server")),S_(d,"g_cpu",t("ld_game")),S_(d,"p_cpu",t("ld_panel"))],{step:d.step,min:10}));
      grid.appendChild(mchart(t("ld_cpu_core"),"%",[S_(d,"cpu_max",t("ld_server"))],{step:d.step,min:10}));
      grid.appendChild(mchart(t("ld_ram"),t("ld_gb"),[S_(d,"ram_used",t("ld_server"),1/1024),S_(d,"g_rss",t("ld_game"),1/1024),S_(d,"p_rss",t("ld_panel"),1/1024)],o));
      grid.appendChild(mchart(t("ld_diskio"),t("ld_ops"),[S_(d,"d_r_iops",t("ld_read")),S_(d,"d_w_iops",t("ld_write"))],o));
      grid.appendChild(mchart(t("ld_diskmb"),t("ld_mbs"),[S_(d,"d_r_mbs",t("ld_read")),S_(d,"d_w_mbs",t("ld_write"))],o));
      grid.appendChild(mchart(t("ld_gameio"),t("ld_ops"),[S_(d,"g_r_iops",t("ld_read")),S_(d,"g_w_iops",t("ld_write"))],o));
      grid.appendChild(mchart(t("ld_net"),t("ld_mbs"),[S_(d,"n_in_mbs",t("ld_in")),S_(d,"n_out_mbs",t("ld_out"))],o));
      grid.appendChild(mchart(t("ld_online"),t("ld_players"),[S_(d,"online",t("ld_online"))],o));
      grid.appendChild(mchart(t("ld_req"),t("ld_rpm"),[S_(d,"req_admin",t("ld_admin")),S_(d,"req_player",t("ld_player"))],o));
      grid.appendChild(mchart(t("ld_lat"),t("ld_ms"),[S_(d,"lat_admin",t("ld_admin")),S_(d,"lat_player",t("ld_player"))],o));
    }).catch(function(e){ grid.innerHTML=""; grid.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  btns(); load();
  ldTimer=setInterval(function(){ if(!document.hidden && S.tab==="load" && auto.checked) load(); },30000);
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
function openMapdt(mapId){
  var card=$("#mdt-card"); if(!card) return;
  card.style.display=""; card.innerHTML="";
  card.appendChild(el("h3",{},[t("md_title")+" · "+mapFull(mapId)]));
  card.appendChild(el("p",{class:"muted"},[t("md_parsing")]));
  api("/api/mapdt?map="+mapId).then(function(d){
    card.innerHTML="";
    if(!d.ok){ card.appendChild(el("h3",{},[t("md_title")+" · "+mapFull(mapId)])); card.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
    card.appendChild(el("h3",{},[t("md_title")+" · "+mapFull(mapId)+" · "+d.w+"×"+d.h+" · "+d.file_mb+" МБ · "+d.parse_sec+"s"]));
    var meta=el("div",{class:"chart-legend"},[
      el("span",{},[t("md_ground")+": "+d.ground.land+" / "+d.ground.water]),
      el("span",{},[t("md_blocks")+": "+d.blocks_total]),
      el("span",{},[t("md_machines")+": "+d.machines_total]),
      el("span",{},[t("md_containers")+": "+d.containers]),
      el("span",{},[t("md_ore")+": "+d.stone_points]),
      el("span",{},[d.oxygen_map? "O₂ map":"—"]),
      (d.gas_tiles||d.infection_tiles)? el("span",{},["газ "+d.gas_tiles+" · зараж. "+d.infection_tiles]) : null,
      (d.vehicles)? el("span",{},["транспорт "+d.vehicles+" (юнитов "+d.vehicle_units+")"]) : null
    ].filter(Boolean));
    card.appendChild(meta);
    card.appendChild(mapImageBlock(mapId));
    var grid=el("div",{class:"grid",style:"grid-template-columns:repeat(auto-fit,minmax(260px,1fr))"},[]);
    function tblcard(title, rows, valkey){
      return el("div",{class:"card"},[el("h3",{},[title+" · "+(rows||[]).length]),
        ltable([t("col_name"),"n"], (rows||[]).slice(0,20), function(r){ return [r.name||("#"+r.type), String(r[valkey]||r.n)]; })]);
    }
    grid.appendChild(tblcard(t("md_blocks"), d.blocks_by_type, "n"));
    grid.appendChild(tblcard(t("md_ore"), d.stone_types, "n"));
    grid.appendChild(tblcard(t("md_containers"), d.container_items, "n"));
    grid.appendChild(tblcard(t("md_machines"), d.machines, "n"));
    grid.appendChild(el("div",{class:"card"},[el("h3",{},[t("md_landowners")+" · "+d.land_owned_blocks8+" / "+d.land_total_blocks8]),
      ltable(["#",t("col_name"),"8×8"], (d.land_owners||[]).slice(0,20), function(r){
        return [String(r.owner), plLink(r.owner, r.name), String(r.blocks8)]; })]));
    card.appendChild(grid);
    card.appendChild(mapdtContainersCard(mapId));
  }).catch(function(e){ card.innerHTML=""; card.appendChild(el("div",{class:"msg err"},[errText(e)])); });
}
function mapdtContainersCard(mapId){
  var body=el("div",{},[]);
  var filt=el("input",{placeholder:t("md_containers_filter"),style:"max-width:220px"},[]);
  var CONT=null;
  function draw(){
    body.innerHTML="";
    if(!CONT){ body.appendChild(el("p",{class:"muted"},["…"])); return; }
    var q=(filt.value||"").trim().toLowerCase();
    var rows=CONT.containers||[];
    if(q) rows=rows.filter(function(c){ return c.items.some(function(it){ return (it.name||"").toLowerCase().indexOf(q)>=0; }); });
    body.appendChild(el("p",{class:"muted"},[
      CONT.total_spots+" · "+CONT.total_items+(CONT.capped? " ("+t("md_containers_capped")+")":"")
    ]));
    body.appendChild(ltable(["X","Y",t("md_containers_col_where"),t("md_containers_col_items")],
      rows.slice(0,300), function(c){
        return [String(c.x), String(c.y), c.slot,
          c.items.map(function(it){ return (it.name||("#"+it.type))+" ×"+it.count; }).join(", ")];
      }));
  }
  filt.addEventListener("input", draw);
  function load(){
    body.innerHTML=""; body.appendChild(el("p",{class:"muted"},["…"]));
    api("/api/mapdt-containers?map="+mapId).then(function(d){
      if(!d.ok){ body.innerHTML=""; body.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
      CONT=d; draw();
    }).catch(function(e){ body.innerHTML=""; body.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  return el("div",{class:"card wide"},[
    el("h3",{},[t("md_containers_list")]),
    el("div",{class:"row",style:"margin-bottom:8px"},[filt,
      el("button",{class:"small",onclick:load},[t("tw_show")])]),
    body,
  ]);
}
function attachDragPan(wrap, info, zoom){
  // тащим карту зажатой ЛКМ (как рукой), не только скроллбарами
  var img=wrap.querySelector("img");
  if(img){ img.draggable=false; img.style.userSelect="none"; }
  wrap.style.cursor="grab";
  var sx=0, sy=0, sl=0, st=0;
  function onUp(){
    wrap._panning=false;
    document.removeEventListener("mousemove",onMove);
    document.removeEventListener("mouseup",onUp);
    wrap.style.cursor="grab";
  }
  function onMove(e){
    if(e.buttons===0){ onUp(); return; }   // кнопку отпустили вне окна — самовосстановление
    wrap._panning=true;
    if(info) info.clear();
    wrap.scrollLeft=sl-(e.clientX-sx);
    wrap.scrollTop=st-(e.clientY-sy);
  }
  wrap.addEventListener("mousedown",function(e){
    if(e.button!==0) return;
    sx=e.clientX; sy=e.clientY; sl=wrap.scrollLeft; st=wrap.scrollTop;
    wrap.style.cursor="grabbing";
    wrap._panning=true;
    if(info) info.clear();
    document.addEventListener("mousemove",onMove);
    document.addEventListener("mouseup",onUp);
    e.preventDefault();
  });
  window.addEventListener("blur",onUp);
  if(zoom){
    wrap.addEventListener("wheel",function(e){
      e.preventDefault();
      var old=parseFloat(zoom.value), mn=parseFloat(zoom.min), mx=parseFloat(zoom.max);
      var next=Math.min(mx, Math.max(mn, old*(e.deltaY<0? 1.15 : 1/1.15)));
      if(Math.abs(next-old)<0.01) return;
      var r=wrap.getBoundingClientRect();
      var offX=e.clientX-r.left+wrap.scrollLeft, offY=e.clientY-r.top+wrap.scrollTop;
      var ratio=next/old;
      zoom.value=Math.round(next);
      if(zoom.oninput) zoom.oninput();
      wrap.scrollLeft=offX*ratio-(e.clientX-r.left);
      wrap.scrollTop=offY*ratio-(e.clientY-r.top);
    }, {passive:false});
  }
}
// Зафиксированная (не бегущая за курсором) панель с инфо о наведении на карту —
// в отличие от плавающей подсказки внутри скроллируемого wrap, её позиция не
// зависит от scrollLeft/scrollTop (панорамирование/зум) и её не обрезает край
// экрана — раньше именно это и делало подсказку "непонятно где".
function mapInfoBar(placeholder){
  var box=el("div",{class:"mapinfo muted"},[placeholder]);
  return {
    el: box,
    show: function(lines){
      box.className="mapinfo";
      box.innerHTML="";
      (Array.isArray(lines)?lines:[lines]).forEach(function(l){ box.appendChild(el("div",{},[l])); });
    },
    clear: function(){ box.className="mapinfo muted"; box.textContent=placeholder; }
  };
}
function mapImageBlock(mapId){
  var img=el("img",{alt:"map "+mapId, style:"image-rendering:pixelated;display:block;border:1px solid var(--line);border-radius:6px;background:var(--panel2);width:100%;transition:transform .1s"});
  var stage=el("div",{style:"display:flex;align-items:center;justify-content:center;margin:0 auto"},[img]);
  var info=mapInfoBar(t("mi_hover_hint"));
  var wrap=el("div",{style:"position:relative;overflow:auto;max-height:74vh;border:1px solid var(--line);border-radius:8px;padding:2px"},[stage]);
  var ownIn=el("input",{type:"number",placeholder:t("mi_owner"),style:"padding:4px 7px;width:100px"});
  var claimsCb=el("input",{type:"checkbox",checked:"checked"});
  var clansCb=el("input",{type:"checkbox"});
  var shopsCb=el("input",{type:"checkbox"});
  var clanLeg=el("div",{class:"chart-legend",style:"margin-top:4px"},[]);
  var rot=315;
  var rotLbl=el("span",{class:"muted small",style:"min-width:34px;display:inline-block;text-align:center"},["315°"]);
  var rotCcw=el("button",{class:"small",title:t("mi_rot_ccw"),onclick:function(){ rot-=45; applyView(); }},["↺"]);
  var rotCw=el("button",{class:"small",title:t("mi_rot_cw"),onclick:function(){ rot+=45; applyView(); }},["↻"]);
  var zoom=el("input",{type:"range",min:"25",max:"400",step:"5",value:"50",style:"width:150px"});
  var stat=el("span",{class:"muted small"},[t("mi_wait")]);
  var OW=null;   // сетка владения {w,h,um_w,um_h,grid,names}
  function rotDeg(){ return rot; }
  function applyView(){
    // картинка позиционируется в px (не в % от контейнера) и центрируется в
    // «сцене» размером с диагональ — иначе повёрнутый угол обрезается/
    // прилипает к краю контейнера с overflow:auto
    var d=rotDeg();
    var zPct=parseFloat(zoom.value)||100;
    rotLbl.textContent=(((d%360)+360)%360)+"°";
    img.style.transform=d? "rotate("+d+"deg)" : "";
    if(img.naturalWidth){
      var rw=img.naturalWidth*zPct/100, rh=img.naturalHeight*zPct/100;
      img.style.width=rw+"px"; img.style.height=rh+"px";
      var diag=Math.ceil(Math.sqrt(rw*rw+rh*rh));
      stage.style.width=diag+"px"; stage.style.height=diag+"px";
    } else {
      img.style.width=zPct+"%";
    }
  }
  zoom.oninput=applyView;
  applyView();
  function reload(force){
    stat.textContent=t("mi_wait");
    var u="/api/mapdt-image?map="+mapId+"&claims="+(claimsCb.checked?1:0)+(ownIn.value?"&owner="+encodeURIComponent(ownIn.value.trim()):"")
      +(clansCb.checked?"&clans=1":"")+(shopsCb.checked?"&shops=1":"")+(force?"&force=1":"")+"&_="+Date.now();
    img.onload=function(){
      stat.textContent=img.naturalWidth+"×"+img.naturalHeight+" px"; applyView();
      clanLeg.innerHTML="";
      if(clansCb.checked) api("/api/map-clans?map="+mapId).then(function(c){
        if(!c.ok) return;
        if(!c.clans.length) clanLeg.appendChild(el("span",{class:"muted"},[t("mi_no_clans")]));
        c.clans.forEach(function(x){ clanLeg.appendChild(lgSwatch(x.rgb.join(","), x.name+" · "+x.blocks8+" "+t("mi_blocks")+" · "+x.owners+" "+t("mi_owners"))); });
        if(c.no_clan_blocks8) clanLeg.appendChild(lgSwatch("205,205,205", t("mi_noclan")+" · "+c.no_clan_blocks8+" "+t("mi_blocks")));
      }).catch(function(){});
      // рендер картинки на сервере попутно обновляет и кэш сетки владения —
      // к моменту onload он уже свежий, тянем заново только при форс-пересмотре
      if(force) api("/api/mapdt-owners?map="+mapId+"&_="+Date.now()).then(function(d){ if(d.ok) OW=d; }).catch(function(){});
    };
    img.onerror=function(){ stat.textContent=t("err_net"); };
    img.src=u;
  }
  var reviewBtn=el("button",{class:"small",title:t("mi_review_hint"),onclick:function(){ reload(true); }},["⟳ "+t("mi_review")]);
  claimsCb.onchange=function(){ reload(); };
  clansCb.onchange=function(){ reload(); };
  shopsCb.onchange=function(){ reload(); };
  ownIn.addEventListener("keydown",function(e){ if(e.key==="Enter") reload(); });
  api("/api/mapdt-owners?map="+mapId).then(function(d){ if(d.ok) OW=d; }).catch(function(){});
  img.addEventListener("mousemove",function(e){
    if(wrap._panning || !img.naturalWidth){ info.clear(); return; }
    var r=img.getBoundingClientRect();
    var cx=(r.left+r.right)/2, cy=(r.top+r.bottom)/2;
    var dx=e.clientX-cx, dy=e.clientY-cy;
    var rd=rotDeg();
    if(rd){ var a=-rd*Math.PI/180, cs=Math.cos(a), sn=Math.sin(a);
      var nx=dx*cs-dy*sn, ny=dx*sn+dy*cs; dx=nx; dy=ny; }
    var halfW=img.offsetWidth/2, halfH=img.offsetHeight/2;   // размер БЕЗ transform
    var fx=(dx/halfW+1)/2, fy=(dy/halfH+1)/2;                // 0..1 по картинке
    if(fx<0||fx>1||fy<0||fy>1||!OW){ info.clear(); return; }
    var gx=Math.floor(fx*OW.w), gy=OW.h-1-Math.floor(fy*OW.h);   // картинка зеркалена по Y
    var bx=Math.floor(gx/8), by=Math.floor(gy/8);
    var oi=bx*OW.um_h+by, o=(oi>=0&&oi<OW.grid.length)? OW.grid[oi] : 0;
    var cl=o && OW.clans && OW.clans[o];
    info.show((o? "👤 "+(OW.names[o]||("id "+o))+(cl? "  ⚑ "+cl : "") : t("mi_free"))+"  ("+gx+","+gy+")");
  });
  img.addEventListener("mouseleave",function(){ info.clear(); });
  var leg=el("div",{class:"chart-legend",style:"margin-top:6px"},[
    lgSwatch("38,88,150",t("mi_water")), lgSwatch("176,160,126",t("mi_land")),
    lgSwatch("86,148,66",t("mi_grass")), lgSwatch("128,128,134",t("mi_mtn")),
    lgSwatch("122,108,92",t("mi_ore")), lgSwatch("228,202,72",t("mi_wall")),
    lgSwatch("222,138,46",t("mi_built")),
    el("span",{class:"muted"},[t("mi_claim")]) ]);
  setTimeout(reload,0);
  attachDragPan(wrap, info, zoom);
  return el("div",{class:"card wide",style:"margin:8px 0"},[
    el("div",{class:"row",style:"gap:8px;flex-wrap:wrap;margin-bottom:6px;align-items:center"},[
      el("b",{},["🗺 "+t("mi_title")]),
      el("label",{class:"small"},[claimsCb," "+t("mi_claims")]),
      el("label",{class:"small"},[clansCb," "+t("mi_by_clan")]),
      el("label",{class:"small",title:t("mi_shops_hint")},[shopsCb," "+t("mi_shops")]),
      rotCcw, rotLbl, rotCw, reviewBtn,
      el("span",{class:"muted small"},["🔍"]), zoom,
      ownIn, el("button",{class:"small",onclick:function(){ reload(); }},[t("mi_show")]), stat ]),
    wrap, info.el, leg, clanLeg ]);
}
function lgSwatch(rgb, label){
  return el("span",{},[el("b",{style:"background:rgb("+rgb+")"},[]), label]);
}
function lgToggle(rgb, label, checked, onchange){
  var cb=el("input",{type:"checkbox",checked:checked},[]);
  cb.addEventListener("change",function(){ onchange(cb.checked); });
  return el("label",{style:"display:inline-flex;align-items:center;gap:5px;cursor:pointer"},[cb, el("b",{style:"background:rgb("+rgb+")"},[]), label]);
}
var SPACE_MAP_SIZE=760;
function spaceMapBlock(starId){
  starId=starId||1;
  var sz=SPACE_MAP_SIZE;
  var img=el("img",{alt:"star system", style:"image-rendering:pixelated;display:block;width:100%;margin:0 auto;border:0;background:#08090f"});
  var info=mapInfoBar(t("su_hover_hint"));
  var hl=el("div",{style:"position:absolute;width:22px;height:22px;border-radius:50%;border:2px solid #ff3b6f;box-shadow:0 0 10px 2px rgba(255,59,111,.65);pointer-events:none;opacity:0;transform:translate(-50%,-50%);transition:opacity .2s"},[]);
  var wrap=el("div",{style:"position:relative;overflow:auto;max-height:70vh;border:1px solid var(--line);border-radius:8px;padding:2px"},[img,hl]);
  var zoom=el("input",{type:"range",min:"25",max:"400",step:"5",value:"50",style:"width:150px"});
  var stat=el("span",{class:"muted small"},[t("mi_wait")]);
  var DATA=null, hlFrac=null;
  var FILTER={planet:true,satellite:true,asteroid:true,ship:true,meteorite:true,pod:true};
  function refreshShot(){
    var on=Object.keys(FILTER).filter(function(k){ return FILTER[k]; });
    img.src="/api/space-map-image?size="+sz+"&star="+starId+"&show="+(on.length?on.join(","):"_none_")+"&_="+Date.now();
  }
  function updateHl(){
    if(!hlFrac || !img.naturalWidth){ hl.style.opacity=0; return; }
    hl.style.left=(img.offsetLeft+hlFrac.fx*img.offsetWidth)+"px";
    hl.style.top=(img.offsetTop+hlFrac.fy*img.offsetHeight)+"px";
    hl.style.opacity=1;
  }
  function highlightAt(x,y){
    if(!DATA || !img.naturalWidth) return;
    var pad=sz*DATA.pad_frac;
    var xy=toPx(DATA.bounds,pad,x,y);
    hlFrac={fx:xy[0]/sz, fy:xy[1]/sz};
    updateHl();
    var left=img.offsetLeft+hlFrac.fx*img.offsetWidth, top=img.offsetTop+hlFrac.fy*img.offsetHeight;
    wrap.scrollLeft=left-wrap.clientWidth/2;
    wrap.scrollTop=top-wrap.clientHeight/2;
  }
  zoom.oninput=function(){ img.style.width=zoom.value+"%"; updateHl(); };
  zoom.oninput();
  img.onload=function(){ stat.textContent=img.naturalWidth+"×"+img.naturalHeight+" px"; updateHl(); };
  img.onerror=function(){ stat.textContent=t("err_net"); };
  refreshShot();
  api("/api/space-map-data?star="+starId).then(function(d){ if(d.ok){ DATA=d;
    pcount.textContent="🪐 "+d.planet_count+" · 🌙 "+d.satellite_count+" · 🪨 "+d.asteroid_count+
      (d.ships_hidden? " · "+t("su_hidden")+" "+d.ships_hidden+" 🚀":"");
  } }).catch(function(){});
  function kindIcon(k){ return {star:"★",ship:"🚀",meteorite:"☄",pod:"📦",planet:"🪐",satellite:"🌙",asteroid:"🪨"}[k]||"?"; }
  function toPx(bd,pad,x,y){
    var spanx=Math.max(bd.maxx-bd.minx,1), spany=Math.max(bd.maxy-bd.miny,1);
    return [ pad+(x-bd.minx)/spanx*(sz-2*pad), pad+(bd.maxy-y)/spany*(sz-2*pad) ];
  }
  img.addEventListener("mousemove", function(e){
    if(wrap._panning || !DATA || !img.naturalWidth){ info.clear(); return; }
    var r=img.getBoundingClientRect();
    var sx=img.naturalWidth/r.width, sy=img.naturalHeight/r.height;
    var ix=(e.clientX-r.left)*sx, iy=(e.clientY-r.top)*sy;
    var pad=sz*DATA.pad_frac;
    var best=null, bestD=16;
    DATA.points.forEach(function(p){
      if(p.kind!=="star" && FILTER[p.kind]===false) return;
      var xy=toPx(DATA.bounds,pad,p.x,p.y);
      var d=Math.hypot(xy[0]-ix, xy[1]-iy);
      if(d<bestD){ bestD=d; best=p; }
    });
    if(!best){ info.clear(); return; }
    var named=(best.kind==="planet"||best.kind==="satellite"||best.kind==="asteroid");
    var lines=[kindIcon(best.kind)+(named?" #"+best.id+" "+best.name:" #"+best.id)+(best.kind==="star"?"":"  ("+Math.round(best.x)+", "+Math.round(best.y)+")")];
    if(best.kind==="ship"){
      lines.push(best.name||"?");
      lines.push("HP "+best.health+" · "+t("su_cargo")+" "+best.cargo_items);
      lines.push(best.moving? "v=("+best.vx+", "+best.vy+")" : t("su_stopped"));
    } else if(best.kind==="meteorite"||best.kind==="pod"){
      lines.push(t("su_cargo")+" "+best.cargo_items);
      lines.push(best.moving? "v=("+best.vx+", "+best.vy+")" : t("su_stopped"));
    }
    info.show(lines);
  });
  img.addEventListener("mouseleave", function(){ info.clear(); });
  var pcount=el("span",{class:"muted small"},["🪐 …"]);
  function toggle(kind){ return function(v){ FILTER[kind]=v; refreshShot(); }; }
  var legend=el("div",{class:"chart-legend",style:"margin-top:6px"},[
    lgSwatch("255,225,140",t("su_star")),
    lgToggle("190,175,230","🪐 "+t("su_planets"), FILTER.planet, toggle("planet")),
    lgToggle("140,205,235","🌙 "+t("su_satellites"), FILTER.satellite, toggle("satellite")),
    lgToggle("170,125,80","🪨 "+t("su_asteroids"), FILTER.asteroid, toggle("asteroid")),
    lgToggle("90,200,255",t("su_ships"), FILTER.ship, toggle("ship")),
    lgToggle("150,140,128","☄ "+t("su_meteorites"), FILTER.meteorite, toggle("meteorite")),
    lgToggle("230,195,60","📦 "+t("su_pods"), FILTER.pod, toggle("pod")) ]);
  // поиск объекта по имени (Data/world/star<N>.json)
  var findIn=el("input",{placeholder:t("su_find_ph2")+" star"+starId,style:"padding:5px 8px;flex:1;min-width:140px"});
  var findOut=el("div",{class:"muted small",style:"margin-top:4px"},[]);
  function runFind(){
    var qv=findIn.value.trim(); if(!qv) return;
    findOut.innerHTML=""; findOut.appendChild(el("span",{},[t("mi_wait")]));
    api("/api/space-object-find?star="+starId+"&q="+encodeURIComponent(qv)).then(function(d){
      findOut.innerHTML="";
      if(!d.ok){ findOut.appendChild(el("span",{},[d.error||"error"])); return; }
      if(!d.matches.length){ hlFrac=null; updateHl(); findOut.appendChild(el("span",{},[t("su_find_none")])); return; }
      findOut.appendChild(el("span",{},[t("su_find_hits")+" ("+d.total_in_star+" "+t("su_objects")+"): "]));
      d.matches.forEach(function(m){ findOut.appendChild(el("span",{class:"pill",style:"margin:2px 4px 2px 0;cursor:pointer",
        title:t("su_find_click"), onclick:(function(mm){ return function(){ highlightAt(mm.x,mm.y); }; })(m)},[
        kindIcon(m.kind)+" #"+m.id+" "+m.name+"  ("+m.x+", "+m.y+")"])); });
      findOut.appendChild(el("div",{class:"muted small",style:"margin-top:4px"},[d.note]));
      highlightAt(d.matches[0].x, d.matches[0].y);
    }).catch(function(e){ findOut.innerHTML=""; findOut.appendChild(el("span",{},[errText(e)])); });
  }
  findIn.addEventListener("keydown",function(e){ if(e.key==="Enter") runFind(); });
  attachDragPan(wrap, info, zoom);
  return el("div",{},[
    el("div",{class:"row",style:"gap:8px;flex-wrap:wrap;margin-bottom:6px;align-items:center"},[
      el("span",{class:"muted small"},["🔍"]), zoom, stat, pcount ]),
    el("div",{class:"row",style:"gap:6px;flex-wrap:wrap;margin-bottom:6px"},[
      findIn, el("button",{class:"small",onclick:runFind},[t("mf_go")]) ]),
    findOut,
    wrap, info.el, legend,
    el("div",{class:"muted small",style:"margin-top:4px"},[t("su_scatter_note")]) ]);
}
function pfFindCard(){
  ensureItemList();
  var inp=el("input",{list:"mf-itemlist",placeholder:t("pf_ph"),style:"padding:5px 8px;flex:1;min-width:160px"});
  var out=el("div",{id:"pf-out"},[]);
  function run(){
    var q=inp.value.trim(); if(!q) return;
    out.innerHTML=""; out.appendChild(el("p",{class:"muted"},[t("pf_wait")]));
    api("/api/player-item-find?item="+encodeURIComponent(q)).then(function(d){
      out.innerHTML="";
      if(!d.ok){ out.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
      if(d.matched && d.matched.length>1) out.appendChild(el("div",{class:"muted small",style:"margin-bottom:4px"},[
        t("pf_matched")+": "+d.matched.map(function(m){return m.name+" #"+m.id;}).join(", ")]));
      var tt=d.totals||{};
      out.appendChild(el("div",{class:"chart-legend"},[
        el("span",{},[t("pf_players")+": "+tt.players]),
        el("span",{},[t("pf_stash")+": "+tt.stash]),
        el("span",{},[t("pf_carry")+": "+tt.carry]),
        el("span",{},[t("pf_total")+": "+tt.total]) ]));
      if(!(d.players||[]).length){ out.appendChild(el("p",{class:"muted"},[t("pf_none")])); return; }
      out.appendChild(scT(ltable(["#",t("col_name"),t("pl_col_status"),t("pf_stash"),t("pf_carry"),t("pf_total")], d.players, function(r){
        return [String(r.id), plLink(r.id,r.name),
          r.online? pill(true,t("running")) : el("span",{class:"muted"},["off"]),
          String(r.stash), String(r.carry), el("b",{},[String(r.total)])]; })));
    }).catch(function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  inp.addEventListener("keydown",function(e){ if(e.key==="Enter") run(); });
  return el("div",{class:"card",style:"margin-bottom:12px"},[ el("h3",{},["🔎 "+t("pf_title")]),
    el("div",{class:"row",style:"gap:6px;flex-wrap:wrap"},[ inp, el("button",{class:"small",onclick:run},[t("pf_go")]) ]),
    out ]);
}
function ensureItemList(){
  if($("#mf-itemlist")) return;
  var dl=el("datalist",{id:"mf-itemlist"},[]);
  document.body.appendChild(dl);
  api("/api/items").then(function(ij){ if(ij.ok) (ij.items||[]).forEach(function(it){
    dl.appendChild(el("option",{value:(it.label&&it.label!==it.name? it.label+" · " : "")+it.name+" #"+it.id})); }); }).catch(function(){});
}
function mdtFindCard(maps){
  ensureItemList();
  var inp=el("input",{list:"mf-itemlist",placeholder:t("mf_ph"),style:"padding:5px 8px;flex:1;min-width:160px"});
  var sel=el("select",{style:"padding:5px 8px"},[el("option",{value:"all"},[t("mf_all")])].concat(
    (maps||[]).filter(function(r){return r.map!=null && !r.space;}).map(function(r){
      return el("option",{value:String(r.map)},[mapName(r.map)+" ("+(r.size||"?")+")"]); })));
  var out=el("div",{id:"mf-out"},[]);
  function run(){
    var q=inp.value.trim(); if(!q) return;
    out.innerHTML=""; out.appendChild(el("p",{class:"muted"},[t("mf_wait")]));
    api("/api/mapdt-find?map="+encodeURIComponent(sel.value)+"&item="+encodeURIComponent(q)).then(function(d){
      out.innerHTML="";
      if(!d.ok){ out.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
      if(d.matched && d.matched.length>1) out.appendChild(el("div",{class:"muted small",style:"margin-bottom:4px"},[
        t("mf_matched")+": "+d.matched.map(function(m){return m.name+" #"+m.id;}).join(", ")]));
      out.appendChild(el("div",{class:"chart-legend"},[
        el("span",{},[t("mf_total")+": "+d.total_count]),
        el("span",{},[t("mf_spots")+": "+d.spots]),
        el("span",{},[t("mf_scanned")+": "+d.scanned+(d.skipped&&d.skipped.length? " (−"+d.skipped.length+")":"")]),
        el("span",{},[d.elapsed_sec+"s"]) ]));
      if(d.note) out.appendChild(el("div",{class:"muted small"},["⚠ "+d.note]));
      if(!d.spots){ out.appendChild(el("p",{class:"muted"},[t("mf_nomatch")])); return; }
      out.appendChild(scT(ltable([t("pl_map"),t("mf_total"),t("mf_spots"),t("mf_where")], d.per_map, function(r){
        return [ el("a",{class:"pl-link",onclick:(function(m){return function(){ openMapdt(m); };})(r.map)},[mapName(r.map)]),
          String(r.total_count), String(r.spots),
          el("span",{class:"small"},[r.by_where.map(function(w){return w.where+" ×"+w.count;}).join(", ")]) ]; })));
      if((d.by_owner||[]).length){
        out.appendChild(el("div",{class:"muted small",style:"margin:8px 0 2px"},[t("mf_byowner")+":"]));
        out.appendChild(scT(ltable([t("mf_owner"),t("mf_spots"),t("mf_total")], d.by_owner, function(o){
          return [ o.owner? plLink(o.owner, o.owner_name) : el("span",{class:"muted"},[t("mf_nobody")]),
            String(o.spots), el("b",{},[String(o.count)]) ]; })));
      }
      out.appendChild(el("div",{class:"muted small",style:"margin:8px 0 2px"},[t("mf_spots")+":"]));
      out.appendChild(scT(ltable([t("pl_map"),t("pd_coords"),t("mf_where"),t("st_tech"),t("mf_total"),t("mf_owner")],
        d.hits.slice(0,600), function(hh){
          return [ el("a",{class:"pl-link",onclick:(function(m){return function(){ openMapdt(m); };})(hh.map)},[mapName(hh.map)]),
            el("span",{class:"mono"},[hh.x+", "+hh.y]),
            el("span",{class:"small"},[hh.where]),
            (hh.name||("#"+hh.type))+(hh.durability? " ["+hh.durability+"]":""),
            el("b",{},["×"+hh.count]),
            hh.owner? plLink(hh.owner, hh.owner_name) : el("span",{class:"muted"},[t("mf_nobody")]) ]; })));
      if(d.hits.length>600) out.appendChild(el("div",{class:"muted small"},["… "+d.hits.length+" точек, показаны 600"]));
    }).catch(function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  inp.addEventListener("keydown",function(e){ if(e.key==="Enter") run(); });
  return el("div",{class:"card wide"},[ el("h3",{},["🔎 "+t("mf_title")]),
    el("div",{class:"row",style:"gap:6px;margin-bottom:8px;flex-wrap:wrap"},[
      inp, el("span",{class:"muted small"},[t("mf_map")]), sel,
      el("button",{class:"small",onclick:run},[t("mf_go")]) ]),
    out ]);
}
function backupDownload(scope, msg){
  msg.textContent=t("ex_wait");
  gatedFetchBlob("/api/world-backup", {scope:scope}, function(r){
    var fn=(r.headers.get("Content-Disposition")||"").match(/filename="?([^"]+)"?/); fn=fn?fn[1]:"world_backup.zip";
    return r.blob().then(function(bl){ var a=document.createElement("a"); a.href=URL.createObjectURL(bl); a.download=fn; a.click();
      setTimeout(function(){URL.revokeObjectURL(a.href);},4000); msg.textContent="✅ "+fn; });
  }, function(e){ msg.textContent=(e&&(e.error||e.detail))||t("err_net"); });
}
function scT(node, lg){ return el("div",{class:"sc"+(lg?" sc-lg":"")},[node]); }
function drawStats(j){
  var b=$("#stbody"); if(!b) return; b.innerHTML="";
  if(!j.ok){ b.appendChild(el("div",{class:"msg err"},[j.error||"error"])); return; }
  var g=el("div",{class:"grid",style:"grid-template-columns:repeat(auto-fit,minmax(300px,1fr))"},[]);

  var on=j.online||{};
  var olab=(on.series||[]).map(function(p){ var d=new Date(p.t*1000);
    return ("0"+d.getDate()).slice(-2)+"."+("0"+(d.getMonth()+1)).slice(-2)+" "+("0"+d.getHours()).slice(-2)+"h"; });
  b.appendChild(bigChart(t("st_online"), "line", olab,
    [{name:t("st_online"), unit:"игроков", data:(on.series||[]).map(function(p){return p.n;}), color:CHART_COL[0]}],
    [pill(true,t("st_now")+" "+on.now), el("span",{class:"pill"},[t("st_peak")+" "+on.peak7])]));

  var gr=j.growth||{}, days=(gr.days||[]);
  var rt=gr.retention||{d1:[0,0],d7:[0,0]};
  b.appendChild(bigChart(t("st_growth"), "bar", days.map(function(x){return x.d.slice(0,5);}),
    [{name:t("st_reg"), unit:"", data:days.map(function(x){return x.reg;}), color:CHART_COL[2]},
     {name:t("st_dau"), unit:"", data:days.map(function(x){return x.dau;}), color:CHART_COL[0]}],
    [el("span",{class:"pill"},[t("st_ret")+" D1 "+(rt.d1[1]? Math.round(100*rt.d1[0]/rt.d1[1]):0)+"% / D7 "+(rt.d7[1]? Math.round(100*rt.d7[0]/rt.d7[1]):0)+"%  (n="+rt.d1[1]+")"])]));

  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_toplvl")]),
    scT(ltable(["#",t("col_name"),t("pd_level")], (j.top_level||[]).slice(0,30), function(r){ return [String(r.id), plLink(r.id,r.name), String(r.level)]; }))]));
  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_toptime")]),
    scT(ltable(["#",t("col_name"),"h"], (j.top_time||[]).slice(0,30), function(r){ return [String(r.id), plLink(r.id,r.name), String(r.playtime_h)]; }))]));
  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_toptechp")]),
    scT(ltable(["#",t("col_name"),t("st_techs"),t("st_resh"),t("st_researching")], (j.top_tech_players||[]).slice(0,30), function(r){
      return [String(r.id), plLink(r.id,r.name), String(r.tech_count), "~"+(r.research_h||0)+"ч", r.research_name||r.research||"—"]; })),
    el("div",{class:"muted small",style:"margin-top:4px"},[t("st_resh_note")])]));

  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_clans")+" · "+(j.clan_board||[]).length]),
    scT(ltable([t("col_name"),"rating","size"], (j.clan_board||[]).slice(0,30), function(c){
      return [c.name+"", String(c.rating), c.size+"/"+(c.max||"?")]; }))]));

  var bans=j.banned||[];
  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_bans")+" · "+bans.length]),
    bans.length? scT(ltable(["#",t("col_name")], bans, function(r){ return [String(r.id), plLink(r.id,r.name)]; })) : el("div",{class:"muted small"},["—"])]));

  var staff=j.staff||[];
  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_staff")+" · "+staff.length]),
    scT(ltable(["#",t("col_name"),t("pl_col_role")], staff, function(r){
      return [String(r.id), plLink(r.id,r.name), t({0:"pl_role_player",1:"pl_role_mod",2:"pl_role_admin",3:"pl_role_gm"}[r.role]||"pl_role_staff")]; })),
    (j.role_history&&j.role_history.length)? el("div",{class:"muted small",style:"margin-top:8px"},[t("st_hist")+": "+j.role_history.map(function(x){return x.target+"→"+x.role;}).join(", ")]) : null
  ].filter(Boolean)));

  var months=j.months||[];
  if(months.length) g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_month")]),
    scT(el("div",{style:"padding:4px"}, months.map(function(mo){ return el("div",{style:"margin-bottom:6px"},[
      el("b",{},[mo.date]), " — ",
      el("span",{class:"small"},[(mo.rewards||[]).map(function(r){return r.name+"("+r.reward+")";}).join(", ")])]); })))]));

  var lh=j.level_hist||[];
  b.appendChild(bigChart(t("st_lvldist"), "bar", lh.map(function(x){return x.bucket+"+";}),
    [{name:t("st_lvldist"), unit:"игроков", data:lh.map(function(x){return x.n;}), color:CHART_COL[1]}]));
  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_countries")]),
    scT(ltable([t("st_countries"),"n"], (j.country_hist||[]), function(r){ return [r.country, String(r.n)]; }))]));
  g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_toptech")+" · "+(j.top_tech||[]).length]),
    scT(ltable([t("st_tech"),t("st_branch"),"игроков",t("st_resh")], (j.top_tech||[]).slice(0,30), function(r){
      return [r.tech, el("span",{class:"small"},[r.name||"—"]), String(r.n), r.cost_h!=null? "~"+r.cost_h+"ч":"—"]; })),
    el("div",{class:"muted small",style:"margin-top:4px"},[t("st_technote")])]));
  if((j.researching||[]).length) g.appendChild(el("div",{class:"card"},[el("h3",{},[t("st_toptech")+" · "+t("st_researching")]),
    scT(ltable([t("st_tech"),t("st_branch"),"игроков",t("st_resh")], (j.researching||[]).slice(0,30), function(r){
      return [r.tech, el("span",{class:"small"},[r.name||"—"]), String(r.n), r.cost_h!=null? "~"+r.cost_h+"ч":"—"]; }))]));

  var ttc=el("div",{class:"card"},[el("h3",{},[t("tt_title")]), el("div",{class:"muted small"},["…"])]);
  g.appendChild(ttc);
  api("/api/tech-track?limit=120").then(function(tj){
    ttc.innerHTML=""; ttc.appendChild(el("h3",{},[t("tt_title")+(tj.total!=null?" · "+tj.total:"")]));
    if(!tj.ok || !tj.events || !tj.events.length){ ttc.appendChild(el("div",{class:"muted small"},[t("tt_none")])); return; }
    var box=el("div",{class:"mono small",style:"max-height:280px;overflow:auto"},[]);
    tj.events.forEach(function(e){ box.appendChild(el("div",{},[ttLine(e,false)])); });
    ttc.appendChild(box);
  }).catch(function(){ ttc.querySelector(".muted").textContent=t("err_net"); });

  b.appendChild(g);
  b.appendChild(el("p",{class:"muted small",style:"margin-top:8px"},[
    "рег "+j.totals.registered+" • профилей "+j.totals.with_profile+" • кланов "+j.totals.clans+
    " • "+t("st_resh")+" суммарно ~"+(j.total_research_h||0)+"ч • "+(j.cached_age||0)+"s • "+j.generated]));

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
    var ce=hj.conn_errors||{};
    hg.appendChild(el("div",{class:"card"},[el("h3",{},[t("hh_connerr")+" · "+(ce.total||0)]),
      (ce.by_player&&ce.by_player.length)? ltable(["#",t("col_name"),"n"], ce.by_player.slice(0,15), function(r){ return [String(r.id), plLink(r.id,r.name), String(r.n)]; }) : el("div",{class:"muted small"},["—"])]));
    hbox.appendChild(hg);
    var lg=hj.lag||{};
    hbox.appendChild(bigChart(t("hh_lag")+" · "+(lg.total||0), "bar",
      (lg.per_day||[]).map(function(x){return x.d.slice(0,5);}),
      [{name:t("hh_lag"), unit:"событий", data:(lg.per_day||[]).map(function(x){return x.n;}), color:CHART_COL[3]}],
      [el("span",{class:"muted small"},[t("hh_byfunc")+": "+(lg.by_func||[]).slice(0,8).map(function(f){return f.func+"×"+f.n+"(max "+f.max+")";}).join(", ")])]));
  }).catch(function(){});
}

// ---- map tab (world / item search / territories / space) ----
function tabMap(v){
  var wrap=el("div",{},[
    el("div",{class:"row",style:"margin-bottom:10px"},[el("button",{class:"small",onclick:function(){ loadMap(true); }},[t("refresh")])]),
    el("div",{id:"mapbody"},[el("p",{class:"muted"},["…"])])
  ]);
  v.appendChild(wrap); loadMap(false);
  if(isAdmin()) v.appendChild(spaceGenCard());
}
// ---- анализ генерации космоса (что появилось после генерации мира) ----
function spaceGenCard(){
  var out=el("div",{},[el("p",{class:"muted small"},[t("sg_intro")])]);
  function run(force){
    out.innerHTML=""; out.appendChild(el("p",{class:"muted"},["…"]));
    api("/api/space-gen"+(force?"?force=1":"")).then(function(d){ out.innerHTML="";
      if(!d.ok){ out.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
      var kvRows=[[t("sg_world"),d.world],[t("sg_gen"),d.generation.start+" — "+d.generation.end],
        [t("sg_stars"),d.stars_total+" (max #"+d.max_star+")"+(d.missing.length? " · "+t("sg_missing")+" "+d.missing.length : "")],
        [t("sg_clusters"),String(d.clusters_total)],
        [t("sg_start"),d.start_map? String(d.start_map.map)+(d.start_map.was!=null&&d.start_map.was!==d.start_map.map? " ("+t("sg_was")+" "+d.start_map.was+")" : "") : "—"],
        [t("sg_steam"),d.steam&&d.steam.time? d.steam.time+" · build "+d.steam.build : "—"],[t("sg_backups"),String(d.backups)]];
      out.appendChild(el("table",{style:"margin-bottom:10px;width:auto"},kvRows.map(function(r){
        return el("tr",{},[el("td",{class:"muted",style:"padding-right:16px"},[r[0]]),el("td",{},[r[1]])]); })));
      out.appendChild(el("h3",{},[t("sg_concl")]));
      out.appendChild(el("ul",{},d.conclusions.map(function(c){ return el("li",{},[c]); })));
      var rows=d.late_stars.concat(d.changed_stars);
      if(rows.length){
        out.appendChild(el("h3",{style:"margin-top:14px"},[t("sg_late")+" · "+rows.length]));
        out.appendChild(scT(ltable(["#",t("sg_what"),t("sg_when"),t("sg_cluster"),t("sg_objs"),t("sg_planets")], rows, function(r){
          return ["#"+r.id+(r.name? " "+r.name : " "+t("sg_noname")), (r.why==="created"? t("sg_created") : t("sg_modified"))+(r.out_of_order? " ⚠ "+t("sg_ooo") : ""),
            (r.why==="created"? r.created : r.modified)+(r.after_update? " · "+r.after_update : ""),
            r.cluster==null? "—" : String(r.cluster)+(r.start_cluster? " ★ "+t("sg_startcl") : ""),
            r.objects+" ("+r.planets+" / "+r.satellites+" / "+r.asteroids+")", r.planet_names.join(", ")]; })));
      }
      if(d.clusters_changed.length){
        out.appendChild(el("h3",{style:"margin-top:14px"},[t("sg_clch")]));
        out.appendChild(scT(ltable(["#",t("sg_created"),t("sg_modified"),t("sg_stars")], d.clusters_changed, function(c){
          return [String(c.id)+(c.new? " 🆕" : ""), c.created, c.modified, String(c.stars)]; })));
      }
      if(d.start_map && d.start_map.files.length){
        out.appendChild(el("h3",{style:"margin-top:14px"},[t("sg_startfiles")+" "+d.start_map.map]));
        out.appendChild(scT(ltable([t("sg_file"),t("sg_created")], d.start_map.files, function(f){ return [f.file, f.created]; })));
      }
      if(d.history.length){
        out.appendChild(el("h3",{style:"margin-top:14px"},[t("sg_hist")]));
        out.appendChild(scT(ltable([t("sg_backup"),t("sg_when"),"curStarId","stars","startMapId","curObjectId",t("sg_diff")], d.history, function(h){
          var st=h.state||{};
          return [h.backup, h.time, String(st.curStarId), String(st.stars), String(st.startMapId), String(st.curObjectId),
            Object.keys(h.diff||{}).map(function(k){ return k+": "+h.diff[k][0]+" → "+h.diff[k][1]; }).join("; ")||"—"]; })));
      }
    }).catch(function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  return el("div",{class:"card",style:"margin-top:12px"},[
    el("div",{class:"row",style:"align-items:center;gap:8px"},[el("h3",{style:"margin:0"},[t("sg_title")]),
      el("button",{class:"small pri",onclick:function(){ run(false); }},[t("sg_run")]),
      el("button",{class:"small",onclick:function(){ run(true); }},[t("refresh")])]), out]);
}
function loadMap(force){
  var b=$("#mapbody"); if(!b) return; b.innerHTML=""; b.appendChild(el("p",{class:"muted"},["…"]));
  Promise.all([
    api("/api/world"+(force?"?_="+Date.now():"")),
    api("/api/space").catch(function(){ return {ok:false}; })
  ]).then(function(res){ drawMap(res[0], res[1]); })
   .catch(function(e){ b.innerHTML=""; b.appendChild(el("div",{class:"msg err"},[errText(e)])); });
}
function drawMap(w, sj){
  var b=$("#mapbody"); if(!b) return; b.innerHTML="";
  if(!w || !w.ok){ b.appendChild(el("div",{class:"msg err"},[(w&&w.error)||"error"])); return; }
  var wg=el("div",{class:"grid",style:"grid-template-columns:repeat(auto-fit,minmax(300px,1fr))"},[]);
  wg.appendChild(el("div",{class:"card wide"},[el("h3",{},[t("st_world")+" · "+w.totals.maps]),
    el("div",{class:"muted small",style:"margin-bottom:6px"},[t("st_avatars")+" "+w.totals.avatars+" • bots "+w.totals.bots+" • "+t("st_terr")+" "+w.totals.territories]),
    scT(ltable([t("pl_map"),t("md_spacename"),t("st_size"),t("pl_online"),t("st_avatars"),t("st_terr"),""], (w.maps||[]).slice(0,80),
      function(r){ return [
        r.space? "0 · космос ⚠" : mapName(r.map),
        r.space_name? el("span",{class:"small",title:t("md_offworld_hint")},["🪐 "+r.space_name+"  ("+r.space_x+", "+r.space_y+")"])
          : (r.is_offworld? el("span",{class:"muted small",title:t("md_offworld_hint")},["🪐 —"]) : "—"),
        r.size||"—", String(r.online), String(r.avatars), String(r.territories),
        r.map!=null && !r.space? el("a",{class:"pl-link",onclick:(function(m){return function(){ openMapdt(m); };})(r.map)},[t("md_open")]) : ""]; })),
    w.space_note? el("div",{class:"muted small",style:"margin-top:6px"},["⚠ "+w.space_note]) : null
  ].filter(Boolean)));
  wg.appendChild(el("div",{class:"card wide",id:"mdt-card",style:"display:none"},[]));
  wg.appendChild(mdtFindCard(w.maps));

  var mf=el("input",{type:"number",placeholder:t("st_terrfilter"),style:"padding:5px 8px;width:90px"});
  var tt=el("div",{id:"terrtab"},[]);
  function drawTerr(){
    var f=mf.value.trim();
    var rows=(w.territories||[]).filter(function(x){ return !f || String(x.map)===f; });
    tt.innerHTML="";
    tt.appendChild(scT(ltable([t("pl_map"),t("pd_coords"),t("st_owner")], rows.slice(0,500), function(x){
      return [mapName(x.map), x.x+","+x.y, plLink(x.owner_id, x.owner)]; })));
    tt.appendChild(el("p",{class:"muted small"},[rows.length+" / "+(w.territories||[]).length]));
  }
  mf.oninput=drawTerr;
  wg.appendChild(el("div",{class:"card wide"},[el("h3",{},[t("st_terr")]), el("div",{class:"row",style:"margin-bottom:6px"},[mf]), tt]));
  b.appendChild(wg);
  drawTerr();

  if(sj && sj.ok){
    var sg=el("div",{class:"grid",style:"grid-template-columns:repeat(auto-fit,minmax(300px,1fr));margin-top:14px"},[]);
    sg.appendChild(el("div",{class:"card"},[el("h3",{},["🛰 "+t("sp_title")]),
      el("div",{class:"kv"},[el("span",{},[t("sp_inspace")]),el("b",{},[String(sj.in_space_count)+(sj.stuck_offline? "  ("+t("sp_stuck")+" "+sj.stuck_offline+" ⚠)":"")])]),
      el("div",{class:"kv"},[el("span",{},[t("sp_units")]),el("b",{},[String(sj.space_units_total!=null?sj.space_units_total:"—")])]),
      el("div",{class:"kv"},[el("span",{},[t("sp_ship")]),el("b",{},[sj.has_ship+" / "+sj.registered])]),
      (sj.in_space||[]).length? scT(el("div",{style:"margin-top:6px;padding:4px"}, sj.in_space.map(function(p){
        return el("div",{class:"small"},[p.online? "🟢 " : "⚪ ", plLink(p.id,p.name), " · lvl "+(p.level||"?")+" · ship#"+(p.space_unit||"?"), p.stuck? " ⚠":""]); }))) : null,
      el("div",{class:"muted small",style:"margin-top:6px"},[sj.note])
    ].filter(Boolean)));
    if((sj.planets||[]).length){
      sg.appendChild(el("div",{class:"card"},[el("h3",{},[t("sp_planets")+" · "+sj.planets.length]),
        scT(ltable([t("pl_map"),t("sp_plots"),t("sp_owners"),t("st_owner")], sj.planets.slice(0,40), function(p){
          return [mapFull(p.map), String(p.plots), String(p.owner_count),
                  el("span",{class:"small"},[p.owners.slice(0,6).map(function(o){return o.name+"("+o.plots+")";}).join(", ")])]; }))]));
    }
    b.appendChild(sg);
  }

  var subox=el("div",{style:"margin-top:14px"},[]);
  b.appendChild(subox);
  adminSpace(subox);
}
// ---- космос для админки: галактика (все системы) + система (все объекты, участки, корабли) ----
// Карта с масштабом колёсиком, сдвигом мышью, двойной клик — сброс; координаты игры (y вверх);
// значки — «маркеры» с обратным масштабом (на экране одного размера).
function pzMap(R, cx, cy, aspect){
  cx=cx||0; cy=cy||0; aspect=aspect||1;
  var vb0={x:cx-R*aspect,y:-cy-R,w:2*R*aspect}, vb={x:vb0.x,y:vb0.y,w:vb0.w}, markers=[];
  function vh(){ return vb.w/aspect; }
  var svg=svgEl("svg",{viewBox:vb.x+" "+vb.y+" "+vb.w+" "+vh(),
    style:"width:100%;height:auto;aspect-ratio:"+aspect+"/1;display:block;background:var(--panel2);border-radius:10px;cursor:grab;touch-action:none;user-select:none"});
  var layer=svgEl("g",{}); svg.appendChild(layer);
  var zl=el("span",{class:"muted small"},[]);
  function mk(x,y,kids,upd){ var g=svgEl("g",{},kids.filter(Boolean)); layer.appendChild(g); markers.push({g:g,x:x,y:-y,upd:upd}); return g; }
  function label(txt,dx,dy,anchor,color,size){ return svgEl("text",{x:dx,y:dy,"text-anchor":anchor||"start","font-size":size||"12",fill:color||"var(--fg)",
    stroke:"var(--panel2)","stroke-width":"3","paint-order":"stroke"},[txt]); }
  var onzoom=[];
  function redraw(){
    svg.setAttribute("viewBox",vb.x+" "+vb.y+" "+vb.w+" "+vh());
    var w=svg.getBoundingClientRect().width||520, sc=vb.w/w;
    markers.forEach(function(m){ m.g.setAttribute("transform","translate("+m.x+" "+m.y+") scale("+sc+")"); if(m.upd) m.upd(sc); });
    var z=vb0.w/vb.w; zl.textContent="×"+(z<10? z.toFixed(1) : Math.round(z));
    onzoom.forEach(function(f){ f(z,sc); });
  }
  function zoomAt(mx,my,f){ var nw=Math.min(4*vb0.w,Math.max(vb0.w/20000,vb.w*f)), k=nw/vb.w;
    vb.x=mx-(mx-vb.x)*k; vb.y=my-(my-vb.y)*k; vb.w=nw; redraw(); }
  function reset(){ vb={x:vb0.x,y:vb0.y,w:vb0.w}; redraw(); }
  function center(x,y,w){ vb={x:x-w/2,y:-y-w/(2*aspect),w:w}; redraw(); }
  var drag=null, moved=false;
  svg.addEventListener("pointerdown",function(e){ drag={x:e.clientX,y:e.clientY,vx:vb.x,vy:vb.y}; moved=false; });
  svg.addEventListener("pointermove",function(e){ if(!drag) return; if(Math.abs(e.clientX-drag.x)+Math.abs(e.clientY-drag.y)>3){ moved=true; svg.style.cursor="grabbing";
      try{ svg.setPointerCapture(e.pointerId); }catch(_){} }
    if(!moved) return; var k=vb.w/(svg.getBoundingClientRect().width||1); vb.x=drag.vx-(e.clientX-drag.x)*k; vb.y=drag.vy-(e.clientY-drag.y)*k; redraw(); });
  function up(){ drag=null; svg.style.cursor="grab"; }
  svg.addEventListener("pointerup",up); svg.addEventListener("pointercancel",up);
  svg.addEventListener("wheel",function(e){ e.preventDefault(); var r=svg.getBoundingClientRect();
    zoomAt(vb.x+(e.clientX-r.left)/r.width*vb.w, vb.y+(e.clientY-r.top)/r.height*vh(), e.deltaY<0? 1/1.25 : 1.25); },{passive:false});
  svg.addEventListener("dblclick",reset);
  function ctr(f){ return function(){ zoomAt(vb.x+vb.w/2, vb.y+vh()/2, f); }; }
  var bar=el("div",{class:"row small",style:"gap:6px;margin:6px 0;align-items:center"},[
    el("button",{class:"small",onclick:ctr(1/1.5)},["+"]), el("button",{class:"small",onclick:ctr(1.5)},["−"]),
    el("button",{class:"small",onclick:reset},["⟲"]), zl, el("span",{class:"muted small"},[t("pz_hint")])]);
  setTimeout(redraw,0);
  if(window.ResizeObserver) new ResizeObserver(function(){ redraw(); }).observe(svg);
  return {svg:svg, mk:mk, label:label, bar:bar, redraw:redraw, center:center, wasDrag:function(){ return moved; }, onzoom:onzoom};
}
function adminSpace(box){
  box.innerHTML="";
  var gc=el("div",{},[el("p",{class:"muted"},["…"])]), sc=el("div",{style:"margin-top:12px"},[]);
  box.appendChild(gc); box.appendChild(sc);
  var selMark=null, G=null;
  function openSys(id){ sc.innerHTML=""; sc.appendChild(el("p",{class:"muted"},["…"]));
    api("/api/space-system?star="+id).then(function(d){ drawAdminSystem(sc,d); sc.scrollIntoView({behavior:"smooth",block:"start"}); })
      .catch(function(e){ sc.innerHTML=""; sc.appendChild(el("div",{class:"msg err"},[errText(e)])); }); }
  api("/api/space-galaxy").then(function(g){
    gc.innerHTML="";
    if(!g.ok){ gc.appendChild(el("div",{class:"msg err"},["🌌 "+(g.error||"error")])); openSys(1); return; }
    G=g; var R=1; g.stars.forEach(function(s){ R=Math.max(R,Math.abs(s[1]),Math.abs(s[2])); }); R*=1.03;
    var pz=pzMap(R,0,0,2.2), mk=pz.mk, label=pz.label;
    g.stars.forEach(function(s){
      var id=s[0], nm=s[3]||("#"+id), claims=s[5], owners=s[6], ships=s[7];
      var tip=svgEl("title",{},[nm+" #"+id+" · "+t("sp_cluster")+" "+s[4]+" · "+t("sp_objs")+" "+s[8]+(claims? " · "+t("sp_plots")+" "+claims+" / "+t("sp_owners")+" "+owners : "")+(ships? " · "+t("su_ships")+" "+ships : "")]);
      var c;
      if(claims) c=svgEl("circle",{r:3+Math.min(9,Math.sqrt(claims)),fill:"var(--s1)",stroke:"var(--panel2)","stroke-width":"1.5"},[tip]);
      else if(ships) c=svgEl("circle",{r:3,fill:"var(--s2)"},[tip]);
      else c=svgEl("circle",{r:1.6,fill:"var(--mut)",opacity:"0.55"},[tip]);
      var g2=mk(s[1],s[2],[c, (claims||ships)? label(nm,9,4,"start","var(--fg)","11") : null]);
      g2.style.cursor="pointer";
      g2.addEventListener("click",function(){ if(!pz.wasDrag()) openSys(id); });
    });
    var find=el("input",{list:"gal-stars",placeholder:t("sp_find_ph"),style:"min-width:240px"});
    var dl=el("datalist",{id:"gal-stars"},g.stars.filter(function(s){ return s[3]; }).map(function(s){ return el("option",{value:s[3]+" #"+s[0]}); }));
    find.addEventListener("change",function(){ var m=find.value.match(/#(\d+)\s*$/); if(m){ var s=g.stars.filter(function(x){ return x[0]===+m[1]; })[0];
      if(s){ pz.center(s[1],s[2],R*2*2.2/20); openSys(s[0]); } } });
    var busy=g.busiest.length? scT(ltable([t("sp_system"),t("sp_plots"),t("su_ships"),t("sp_owners")],g.busiest,function(b){
      return [el("a",{class:"pl-link",onclick:function(){ openSys(b.star); }},[b.name+" #"+b.star]), String(b.claims), String(b.ships), el("span",{class:"small"},[b.owners.join(", ")])]; })) : null;
    gc.appendChild(el("div",{class:"card wide"},[el("h3",{},["🌌 "+t("sp_galaxy")+" · "+g.stars.length]),
      el("p",{class:"muted small"},[t("sp_galaxy_hint")]),
      el("div",{class:"row",style:"gap:8px;margin-bottom:6px"},[find, dl]),
      pz.svg, pz.bar,
      el("div",{class:"row small",style:"gap:14px;margin:4px 0 8px"},[
        el("span",{},[legendDotA("var(--s1)",10),t("sp_leg_claims")]), el("span",{},[legendDotA("var(--s2)",8),t("sp_leg_ships")]),
        el("span",{},[legendDotA("var(--mut)",5),t("sp_leg_other")])]),
      busy? el("details",{},[el("summary",{class:"small"},[t("sp_busiest")]), busy]) : null].filter(Boolean)));
    openSys(1);
  }).catch(function(e){ gc.innerHTML=""; gc.appendChild(el("div",{class:"msg err"},[errText(e)])); openSys(1); });
}
function legendDotA(col,sz){ return el("span",{style:"display:inline-block;margin-right:6px;width:"+sz+"px;height:"+sz+"px;border-radius:50%;background:"+col}); }
var ASYS_F={planet:true,satellite:true,asteroid:true,claimed:false,ship:true,meteorite:false,station:true};
function drawAdminSystem(box,d){
  box.innerHTML="";
  if(!d.ok){ box.appendChild(el("div",{class:"msg err"},[d.error||"error"])); return; }
  var R=1; d.objects.forEach(function(o){ R=Math.max(R,Math.hypot(o.x,o.y)); }); R*=1.1;
  var pz=pzMap(R), mk=pz.mk, label=pz.label, KR={planet:t("sp_k1_planet"),satellite:t("sp_k1_satellite"),asteroid:t("sp_k1_asteroid")};
  var ST={landed:t("sp_st_landed"),parked:t("sp_st_parked"),flight:t("sp_st_flight"),open:t("sp_st_open")};
  mk(0,0,[svgEl("circle",{r:14,fill:"#f2c14e",opacity:"0.25"}), svgEl("circle",{r:7,fill:"#f2c14e"},[svgEl("title",{},[d.name||""])]), label(d.name||("#"+d.star),0,26,"middle","var(--mut)")]);
  var lateLabels=[];
  if(ASYS_F.meteorite) d.meteorites.forEach(function(m){ mk(m[0],m[1],[svgEl("circle",{r:1.6,fill:"#9aa4ad",opacity:"0.7"},[svgEl("title",{},[t("su_meteorites")+" · "+Math.round(m[0])+", "+Math.round(m[1])+" · "+m[2]])])]); });
  d.objects.forEach(function(o){
    if(!ASYS_F[o.kind] || (ASYS_F.claimed && !o.claims)) return;
    var r=o.kind==="planet"?7:o.kind==="satellite"?4:2.6, col=o.claims? "var(--s1)" : (o.kind==="asteroid"? "var(--mut)" : "#8a93a0");
    var tip=svgEl("title",{},[o.name+" #"+o.id+" ("+KR[o.kind]+") · "+Math.round(o.x)+", "+Math.round(o.y)+(o.claims? " · "+t("sp_plots")+" "+o.claims+": "+o.owners.slice(0,6).map(function(w){ return w.name+"("+w.n+")"; }).join(", ") : "")]);
    var txt=null;
    if(o.kind==="planet" || o.claims) txt=label(o.name+(o.claims? " · "+o.claims : ""),r+4,4,"start",o.claims? "var(--fg)" : "var(--mut)","11");
    else{ txt=label(o.name,r+4,4,"start","var(--mut)","10"); lateLabels.push(txt); }
    mk(o.x,o.y,[svgEl("circle",{r:r,fill:col,stroke:"var(--panel2)","stroke-width":"1.5"},[tip]), txt]);
  });
  // подписи мелких объектов — только при приближении
  pz.onzoom.push(function(z){ lateLabels.forEach(function(x){ x.setAttribute("visibility", z>=6? "visible" : "hidden"); }); });
  if(ASYS_F.station) d.stations.forEach(function(st){ mk(st.x,st.y,[svgEl("rect",{x:-5,y:-5,width:10,height:10,fill:"var(--s3)",stroke:"var(--panel2)","stroke-width":"1.5"},
    [svgEl("title",{},[(st.name||"")+" · "+(st.owner||{}).name+(st.clan? " · "+st.clan : "")])]), label(st.name||"",8,4,"start","var(--s3)","11")]); });
  if(ASYS_F.ship) d.ships.forEach(function(s){
    var far=Math.hypot(s.x,s.y)>R;
    var tip=svgEl("title",{},[s.owner_name+" · "+s.model+" #"+s.id+" · "+ST[s.status]+(s.near? " "+s.near : "")+" · "+Math.round(s.x)+", "+Math.round(s.y)]);
    var x=s.x, y=s.y; if(far){ var a=Math.atan2(s.y,s.x); x=Math.cos(a)*R*0.93; y=Math.sin(a)*R*0.93; }
    mk(x,y,[svgEl("path",{d:"M0 -7 L6 5 L-6 5 Z",fill:"var(--s2)",stroke:"var(--panel2)","stroke-width":"1.5"},[tip]),
      label(s.owner_name+(s.status==="landed"? " 🛬" : "")+(far? " →" : ""),-8,16,"end","var(--s2)","11")]);
  });
  var filt=el("div",{class:"row small",style:"gap:10px;flex-wrap:wrap;margin:4px 0"},[["planet","sp_k_planet"],["satellite","sp_k_satellite"],["asteroid","sp_k_asteroid"],
    ["claimed","sp_only_claimed"],["ship","su_ships"],["meteorite","su_meteorites"],["station","sp_stations"]].map(function(f){
      var cb=el("input",{type:"checkbox"}); cb.checked=!!ASYS_F[f[0]]; cb.onchange=function(){ ASYS_F[f[0]]=cb.checked; drawAdminSystem(box,d); };
      return el("label",{},[cb," "+t(f[1])]); }));
  var claimed=d.objects.filter(function(o){ return o.claims; }).sort(function(a,b){ return b.claims-a.claims; });
  var right=el("div",{style:"flex:1;min-width:300px"},[
    el("h3",{},[t("sp_claimed_objs")+" · "+claimed.length]),
    claimed.length? el("div",{style:"max-height:300px;overflow:auto"},[ltable([t("sp_obj"),t("sp_type"),t("pd_coords"),t("sp_plots"),t("sp_owners")],claimed,function(o){
      return [el("a",{class:"pl-link",onclick:function(){ pz.center(o.x,o.y,R/10); }},[o.name+" #"+o.id]), KR[o.kind], Math.round(o.x)+", "+Math.round(o.y), String(o.claims),
        el("span",{class:"small"},o.owners.slice(0,5).map(function(w,i){ return el("span",{},[i? ", " : "", plLink(w.id,w.name), " ("+w.n+")"]); }))]; })]) : el("div",{class:"muted small"},["—"]),
    el("h3",{style:"margin-top:12px"},[t("su_ships")+" · "+d.ships.length]),
    d.ships.length? el("div",{style:"max-height:260px;overflow:auto"},[ltable([t("st_owner"),t("sp_model"),t("sp_status"),t("pd_coords"),t("su_hp")],d.ships,function(s){
      return [s.owner? plLink(s.owner,s.owner_name) : "—", s.model+" #"+s.id, (s.status==="landed"? "🛬 " : "")+ST[s.status]+(s.near? " "+s.near : ""),
        el("a",{class:"pl-link",onclick:function(){ pz.center(s.x,s.y,R/10); }},[Math.round(s.x)+", "+Math.round(s.y)]), String(s.health!=null? s.health : "—")]; })]) : el("div",{class:"muted small"},["—"]),
    el("div",{class:"muted small",style:"margin-top:8px"},[t("su_meteorites")+": "+d.meteorite_count+" · "+t("sp_stations")+": "+d.stations.length])]);
  box.appendChild(el("div",{class:"card wide"},[
    el("h3",{},["🪐 "+t("sp_system")+" "+(d.name||"")+" #"+d.star+(d.cluster!=null? " · "+t("sp_cluster")+" "+d.cluster : "")+" · "+t("sp_objs")+" "+d.objects.length]),
    filt,
    el("div",{class:"row",style:"align-items:flex-start;gap:16px;flex-wrap:wrap"},[el("div",{style:"flex:1;min-width:320px;max-width:720px"},[pz.svg,pz.bar]), right])]));
}
var GALAXY=null;   // {clusters[{cluster_id,x,y,star_count,stars}]} — грузится один раз
function loadSystem(subox, starId){
  subox.innerHTML=""; subox.appendChild(el("p",{class:"muted"},["…"]));
  Promise.all([
    api("/api/space-units?star="+starId),
    GALAXY? Promise.resolve(GALAXY) : api("/api/space-clusters").then(function(g){ if(g.ok) GALAXY=g; return g; }).catch(function(){ return {ok:false}; })
  ]).then(function(res){ drawSystem(subox, starId, res[0], res[1]); })
   .catch(function(e){ subox.innerHTML=""; subox.appendChild(el("div",{class:"msg err"},[errText(e)])); });
}
function drawSystem(subox, starId, su, gal){
    subox.innerHTML="";
    if(!su.ok){ subox.appendChild(el("div",{class:"msg err"},[su.error||"space/units.dt —"])); return; }
    var bd=su.bounds||{};
    var cur=(gal&&gal.ok)? gal.clusters.filter(function(c){return c.stars.indexOf(starId)>=0;})[0] : null;
    var clSel=el("select",{style:"padding:4px 6px"}, ((gal&&gal.ok)? gal.clusters:[]).map(function(c){
      return el("option",{value:String(c.cluster_id),selected:(cur&&c.cluster_id===cur.cluster_id)?"selected":null},
        ["#"+c.cluster_id+" ("+c.star_count+")"]); }));
    var stSel=el("select",{style:"padding:4px 6px"}, (cur?cur.stars:[starId]).map(function(sid){
      return el("option",{value:String(sid),selected:sid===starId?"selected":null},["star"+sid]); }));
    function fillStars(clusterId){
      var c=(gal&&gal.ok)? gal.clusters.filter(function(x){return x.cluster_id===clusterId;})[0] : null;
      stSel.innerHTML="";
      (c?c.stars:[starId]).forEach(function(sid){ stSel.appendChild(el("option",{value:String(sid)},["star"+sid])); });
    }
    clSel.onchange=function(){ fillStars(parseInt(clSel.value,10)); };
    stSel.onchange=function(){ loadSystem(subox, parseInt(stSel.value,10)); };
    subox.appendChild(el("div",{class:"card wide"},[
      el("h3",{},["🚀 "+t("su_title")+" · star"+starId+(cur? " · "+t("su_cluster")+" #"+cur.cluster_id:"")]),
      (gal&&gal.ok)? el("div",{class:"row",style:"gap:6px;flex-wrap:wrap;margin-bottom:8px;align-items:center"},[
        el("span",{class:"muted small"},[t("su_cluster")+":"]), clSel,
        el("span",{class:"muted small"},[t("su_system")+":"]), stSel,
        el("span",{class:"muted small"},["("+gal.clusters.length+" "+t("su_clusters")+" · "+gal.star_count+" "+t("su_systems")+")"]) ])
        : null,
      el("div",{class:"chart-legend"},[
        el("span",{},[t("su_ships")+": "+su.ships.length]),
        el("span",{},["☄ "+t("su_meteorites")+": "+su.meteorite_count]),
        el("span",{},["📦 "+t("su_pods")+": "+su.pod_count]),
        el("span",{class:"muted"},["X "+bd.minx+"…"+bd.maxx+" · Y "+bd.miny+"…"+bd.maxy]) ]),
      el("h3",{style:"margin-top:10px"},[t("su_starmap")]),
      spaceMapBlock(starId),
      su.ships.length? scT(ltable(["#",t("col_name"),t("pd_coords"),t("su_vel"),t("su_hp"),t("su_cargo"),""], su.ships, function(s){
        return [ el("span",{class:"mono"},[String(s.id)]),
          s.user_id? plLink(s.user_id, s.name) : el("span",{class:"muted"},["—"]),
          el("span",{class:"mono"},[Math.round(s.x)+", "+Math.round(s.y)]),
          el("span",{class:"mono small"},[s.moving? (s.vx+", "+s.vy) : "—"]),
          String(s.health), String(s.cargo_items),
          s.moving? el("span",{class:"pill"},[t("su_moving")]) : el("span",{class:"muted small"},["·"]) ]; }))
        : el("div",{class:"muted small"},["—"]),
      (su.meteorites||[]).length? el("details",{style:"margin-top:8px"},[
        el("summary",{class:"small"},["☄ "+t("su_meteorites")+" ("+su.meteorite_count+(su.meteorite_count>su.meteorites.length? ", показаны "+su.meteorites.length:"")+")"]),
        scT(ltable(["#",t("pd_coords"),t("su_vel"),t("su_cargo")], su.meteorites, function(m){
          return [ el("span",{class:"mono"},[String(m.id)]), el("span",{class:"mono"},[Math.round(m.x)+", "+Math.round(m.y)]),
            el("span",{class:"mono small"},[m.moving? (m.vx+", "+m.vy):"—"]), String(m.cargo_items) ]; })) ]) : null,
      el("div",{class:"muted small",style:"margin-top:6px"},["⚠ "+su.note])
    ].filter(Boolean)));
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
  var qq=el("input",{placeholder:t("sc_search"),style:"padding:6px 8px"});
  var out=el("div",{id:"pvout",style:"margin-top:10px"},[]);
  function run(){
    out.innerHTML=""; out.appendChild(el("p",{class:"muted"},["…"]));
    gatedApi("/api/server-chat", {q:qq.value, limit:800}, function(j){
      out.innerHTML="";
      var box=el("div",{class:"mono small",style:"max-height:62vh;overflow:auto"},[]);
      (j.messages||[]).forEach(function(m){ box.appendChild(el("div",{},[
        el("span",{class:"lg-t"},[fshort(m.ts)+" "]),
        m.from_id!=null? plLink(m.from_id,m.from):el("b",{},[m.from]), " → ",
        m.to_id!=null? plLink(m.to_id,m.to):el("b",{},[m.to]), ": "+m.text ])); });
      out.appendChild(el("p",{class:"muted small"},[String(j.total||0)])); out.appendChild(box);
    }, function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  body.appendChild(el("p",{class:"muted small"},[t("sc_priv_note")]));
  body.appendChild(el("div",{class:"row"},[qq, el("button",{class:"small pri",onclick:run},[t("tw_show")])]));
  body.appendChild(out);
}

// ---- twinks (same-IP account detector) ----
function tabTwinks(v){
  var mn=el("input",{type:"number",value:"2",min:"2",max:"20",style:"padding:6px 8px;width:80px"});
  var out=el("div",{id:"twout",style:"margin-top:12px"},[]);
  function run(){
    out.innerHTML=""; out.appendChild(el("p",{class:"muted"},["…"]));
    gatedApi("/api/twinks", {min_accounts:parseInt(mn.value,10)||2}, function(j){
      out.innerHTML="";
      out.appendChild(el("p",{class:"muted small"},[
        t("tw_bycode")+": "+j.code_flagged+" ("+j.code_accounts+" акк.) • "+t("tw_byip")+": "+j.flagged_ips+"/"+j.ip_count+
        (j.ignored&&j.ignored.length? " (игнор "+j.ignored.join(", ")+")":"")+" • "+j.generated]));
      function acctable(accs, extra){
        var head=["ID",t("col_name")].concat(extra||[]);
        var tb=el("table",{},[el("tr",{},head.map(function(x){return el("th",{},[x]);}))]);
        accs.forEach(function(a){
          var tds=[el("td",{class:"mono"},[a.id!=null?String(a.id):"—"]),
                   el("td",{},[a.id!=null? plLink(a.id,a.name) : a.name])];
          if(a.connects!=null) tds.push(el("td",{class:"mono"},[String(a.connects)]),
            el("td",{class:"mono muted"},[fshort(a.first_seen)]), el("td",{class:"mono muted"},[fshort(a.last_seen)]),
            el("td",{class:"mono muted"},[a.other_ips&&a.other_ips.length?a.other_ips.join(" "):"—"]));
          tb.appendChild(el("tr",{},tds));
        });
        return tb;
      }
      out.appendChild(el("h3",{},["🔑 "+t("tw_bycode")]));
      if(!(j.code_groups||[]).length) out.appendChild(el("p",{class:"muted small"},[t("tw_none")]));
      (j.code_groups||[]).forEach(function(gr){
        out.appendChild(el("div",{class:"card",style:"margin-bottom:8px"},[
          el("h3",{},["🔑 "+t("tw_samepw")+" · "+gr.count+" ("+gr.hint+")"]), acctable(gr.accounts)]));
      });
      if((j.fp_groups||[]).length){
        out.appendChild(el("h3",{style:"margin-top:12px"},["🖥 "+t("tw_byfp")]));
        (j.fp_groups||[]).forEach(function(gr){
          out.appendChild(el("div",{class:"card",style:"margin-bottom:8px"},[
            el("h3",{},["🖥 "+gr.gpu+" / "+gr.screen+" · "+gr.count]), acctable(gr.accounts)]));
        });
      }
      out.appendChild(el("h3",{style:"margin-top:12px"},["🌐 "+t("tw_byip")]));
      if(!(j.groups||[]).length) out.appendChild(el("p",{class:"muted small"},[t("tw_none")]));
      (j.groups||[]).forEach(function(gr){
        out.appendChild(el("div",{class:"card",style:"margin-bottom:8px"},[
          el("h3",{},["🌐 "+gr.ip+" · "+gr.count]),
          acctable(gr.accounts,[t("tw_connects"),t("pl_col_enter"),t("pl_col_exit"),t("tw_other_ips")])]));
      });
    }, function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  }
  v.appendChild(el("div",{},[
    el("p",{class:"muted small"},[t("tw_intro")]),
    el("div",{class:"row"},[
      el("label",{class:"small"},[t("tw_min")+" ", mn]),
      el("button",{class:"small pri",onclick:run},[t("tw_show")])
    ]),
    out
  ]));
}

// ---- roles ----
var SET_FIELDS={};   // path -> {type, getval()}
var SET_ACCS=[];
function tabSettings(v){
  var out=el("div",{id:"setout"},[el("p",{class:"muted"},["…"])]);
  v.appendChild(out);
  api("/api/settings").then(function(j){ drawSettings(out,j); })
    .catch(function(e){ out.innerHTML=""; out.appendChild(el("div",{class:"msg err"},[errText(e)])); });
}
function setFieldInput(fd){
  var lab=(S.lang==="ru"? fd.label_ru : fd.label_en)||fd.path;
  var inp;
  if(fd.type==="bool"){
    inp=el("input",{type:"checkbox"}); if(fd.value) inp.checked=true;
    SET_FIELDS[fd.path]={type:fd.type, get:function(){ return inp.checked; }};
    return el("label",{class:"row",style:"gap:8px"},[inp, el("span",{},[lab]),
      fd.hint? el("span",{class:"muted small"},["— "+fd.hint]):null].filter(Boolean));
  }
  if(fd.type==="strlist"||fd.type==="intlist"){
    inp=el("textarea",{rows:"2",style:"width:100%;font-family:inherit"},[String(fd.value||"")]);
  } else if(fd.type==="secret"){
    inp=el("input",{type:"password",placeholder:t("set_secret_ph"),style:"width:100%"});
  } else {
    inp=el("input",{value:fd.value==null?"":String(fd.value),style:"width:100%"});
  }
  SET_FIELDS[fd.path]={type:fd.type, get:function(){ return inp.value; }};
  return el("label",{class:"fld"},[
    el("span",{},[lab + (fd.type==="secret" && fd.has_secret? "  ("+t("set_secret_set")+")":"")]),
    inp,
    fd.hint? el("div",{class:"muted small"},[fd.hint]):null ].filter(Boolean));
}
function accRow(a){
  var lbl=el("input",{value:a.label||"",placeholder:t("set_acc_label"),style:"width:120px"});
  var usr=el("input",{value:a.user||"",placeholder:t("set_acc_user"),style:"width:150px"});
  var pw=el("input",{type:"password",placeholder:a.has_password? "••••••":t("set_acc_pw"),style:"width:170px"});
  var rec={label:lbl,user:usr,pw:pw};
  SET_ACCS.push(rec);
  var rm=el("button",{class:"small danger",onclick:function(){ row.remove(); SET_ACCS=SET_ACCS.filter(function(x){return x!==rec;}); }},["×"]);
  var row=el("div",{class:"row",style:"gap:6px;margin-bottom:5px;align-items:center"},[
    el("input",{type:"radio",name:"setacc",value:a.label||"",checked:a.active?"checked":null,title:t("set_acc_active")}),
    lbl, usr, pw, rm ]);
  return row;
}
// ---- пользователи панели (только админ; изменения — под паролем, в аудит) ----
function usersCard(){
  var body=el("div",{},[el("p",{class:"muted small"},["…"])]), msg=el("div",{});
  function roleSel(v){ var s=el("select",{},(isGM()||v==="gm"?["gm"]:[]).concat(["admin","moderator","viewer"]).map(function(r){ var o=el("option",{value:r},[t("role_"+r)]); if(r===v) o.selected=true; return o; })); return s; }
  function call(extra){ msg.innerHTML="";
    gatedApi("/api/users",extra,function(d){ draw(d); },function(e){ msg.appendChild(el("div",{class:"msg err"},[e.detail||errText(e)])); }); }
  function draw(d){
    body.innerHTML="";
    var tb=el("table",{},[el("tr",{},[t("us_name"),t("us_role"),"",""].map(function(x){ return el("th",{},[x]); }))]);
    d.users.forEach(function(u){
      var me=u.name===S.user, rs=roleSel(u.role);
      rs.disabled=me; rs.addEventListener("change",function(){ call({op:"role",name:u.name,role:rs.value}); });
      tb.appendChild(el("tr",{},[
        el("td",{},[el("b",{},[u.name]), me? el("span",{class:"muted small"},["  ("+t("us_me")+")"]) : null,
          u.must_change? el("span",{class:"pill warn",style:"margin-left:6px"},[t("us_mc")]) : null]),
        el("td",{},[rs]),
        el("td",{},[el("button",{class:"small",onclick:function(){ var pw=window.prompt(t("us_new_pw")+u.name); if(pw) call({op:"reset",name:u.name,new_password:pw}); }},[t("us_reset")])]),
        el("td",{},[me? null : el("button",{class:"small danger",onclick:function(){ if(window.confirm(t("us_confirm_del")+u.name+"?")) call({op:"delete",name:u.name}); }},[t("us_del")])])]));
    });
    var nm=el("input",{placeholder:t("us_name"),style:"width:140px"}), pw=el("input",{type:"password",placeholder:t("us_pw"),autocomplete:"new-password",style:"width:220px"}), rl=roleSel("moderator");
    body.appendChild(tb);
    body.appendChild(el("div",{class:"row",style:"gap:8px;flex-wrap:wrap;margin-top:10px"},[nm,rl,pw,
      el("button",{class:"pri small",onclick:function(){ call({op:"add",name:nm.value.trim(),role:rl.value,new_password:pw.value}); }},[t("us_add")])]));
  }
  api("/api/users").then(draw).catch(function(e){ body.innerHTML=""; body.appendChild(el("div",{class:"msg err"},[errText(e)])); });
  return el("div",{class:"card",style:"margin-bottom:12px"},[el("h3",{},[t("us_title")]), el("p",{class:"muted small"},[t("us_intro")]), body, msg]);
}
function drawSettings(out,j){
  out.innerHTML=""; SET_FIELDS={}; SET_ACCS=[];
  if(!j.ok){ out.appendChild(el("div",{class:"msg err"},[j.error||"error"])); return; }
  var grid=el("div",{class:"grid",style:"grid-template-columns:repeat(auto-fit,minmax(320px,1fr))"},[]);
  (j.sections||[]).forEach(function(sec){
    var card=el("div",{class:"card"},[el("h3",{},[S.lang==="ru"? sec.title_ru : sec.title_en])]);
    sec.fields.forEach(function(fd){ card.appendChild(setFieldInput(fd)); });
    grid.appendChild(card);
  });
  out.appendChild(usersCard());
  out.appendChild(grid);
  out.appendChild(faviconCard());

  // игровые аккаунты
  var accBox=el("div",{},[]);
  (j.game_accounts||[]).forEach(function(a){
    a.active=(a.label===j.active_account);
    accBox.appendChild(accRow(a));
  });
  var accCard=el("div",{class:"card",style:"margin-top:12px"},[
    el("h3",{},["🎮 "+t("set_accounts")]),
    accBox,
    el("button",{class:"small",onclick:function(){ accBox.appendChild(accRow({})); }},[t("set_acc_add")]),
    el("p",{class:"muted small"},[t("set_acc_pw")])
  ]);
  out.appendChild(accCard);

  // login_flow advanced
  var lfTa=el("textarea",{rows:"10",style:"width:100%;font-family:ui-monospace,Consolas,monospace;font-size:12px"},[j.login_flow_json||"{}"]);
  out.appendChild(el("details",{style:"margin-top:12px"},[
    el("summary",{class:"small"},[t("set_lf")]),
    el("div",{class:"card"},[lfTa])
  ]));

  var msg=el("div",{id:"setmsg",style:"margin-top:10px"},[]);
  out.appendChild(el("div",{class:"row",style:"margin-top:12px;gap:10px"},[
    el("button",{class:"pri",onclick:function(){ saveSettings(lfTa,msg); }},[t("set_save")]),
    el("span",{class:"muted small"},[j.restart_hint_ru||""])
  ]));
  out.appendChild(msg);
}
function saveSettings(lfTa,msg){
  msg.innerHTML="";
  var values={};
  Object.keys(SET_FIELDS).forEach(function(p){ values[p]=SET_FIELDS[p].get(); });
  var radios=Array.prototype.slice.call(document.querySelectorAll('input[name="setacc"]'));
  var ci=radios.findIndex(function(r){ return r.checked; });
  var active=(ci>=0 && SET_ACCS[ci])? SET_ACCS[ci].label.value.trim() : "";
  var accs=SET_ACCS.map(function(r){ return {label:r.label.value.trim(), user:r.user.value.trim(), password:r.pw.value}; })
                   .filter(function(a){ return a.label||a.user; });
  var body={values:values, game_accounts:accs, active_account:active};
  var lf=lfTa.value.trim();
  if(lf && lf!=="{}"){ try{ JSON.parse(lf); body.login_flow=lf; }catch(e){ msg.appendChild(el("div",{class:"msg err"},["login_flow: невалидный JSON — "+e.message])); return; } }
  api("/api/settings",{body:body}).then(function(r){
    msg.appendChild(el("div",{class:"msg ok"},[t("set_saved")+(r.restart_recommended? " — "+t("set_restart"):"")]));
    if(r.restart_recommended) msg.appendChild(el("button",{class:"small danger",style:"margin-left:8px",
      onclick:function(){ if(window.confirm(t("a_restarttask")+"?")){ S.tab="act"; localStorage.setItem("sw_tab","act"); render(); setTimeout(function(){ runAction("restarttask",{},1,t("a_restarttask")); },300); } }},[t("a_restarttask")]));
  }).catch(function(e){ msg.appendChild(el("div",{class:"msg err"},[errText(e)])); });
}

// ---- logs ----
var logTimer=null;
function tabLogs(v){
  var sub=localStorage.getItem("sw_logsub")||"sup";
  var bar=el("nav",{style:"padding:0;border:0;background:transparent;margin-bottom:10px"},
    (isGM()? [["act","log_act"],["blocks","log_blocks"],["sup","log_sup"],["audit","log_audit"],["nav","log_nav"]]
           : [["sup","log_sup"],["nav","log_nav"]]).map(function(x){
      return el("button",{class:sub===x[0]?"active":"",onclick:function(){ localStorage.setItem("sw_logsub",x[0]); render(); }},[t(x[1])]);
    }));
  var body=el("div",{id:"logbody"},[]);
  v.appendChild(el("div",{},[bar,body]));
  clearInterval(logTimer);
  if(!isGM() && (sub==="act"||sub==="blocks"||sub==="audit")) sub="sup";
  if(sub==="sup") logSup(body);
  else if(sub==="audit") logAudit(body);
  else if(sub==="act") logAct(body);
  else if(sub==="blocks") logBlocks(body);
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
// ---- журнал активности (только GM) ----
var AC_ROWS=[];
function acFmtT(sec){ sec=Math.round(sec); return sec>=86400? Math.round(sec/3600)+" ч" : sec>=3600? (sec%3600? (sec/3600).toFixed(1) : sec/3600)+" ч" : sec>=60? Math.round(sec/60)+" мин" : sec+" с"; }
function acDetail(r){
  var q=r.q? Object.keys(r.q).map(function(k){ return k+"="+(typeof r.q[k]==="string"? r.q[k] : JSON.stringify(r.q[k])); }).join("&") : "";
  if(r.ev==="req"||r.ev==="page"||r.ev==="denied"||r.ev==="probe"){
    return (r.m||"")+" "+(r.via||"")+(r.path||"")+(q? "?"+q : "")+"  → "+(r.st||"?")+" · "+(r.ms||0)+" мс"
      +(r.rep? "  ×"+(r.rep+1) : "")+(r.body&&Object.keys(r.body).length? "  "+JSON.stringify(r.body).slice(0,400) : "")
      +(r.ua? "  ["+r.ua+"]" : ""); }
  if(r.ev==="ui") return "["+(r.tab||"")+"] "+(r.a||"")+": "+(r.d||"");
  if(r.ev==="block") return (r.key||"")+" — "+acFmtT(r.dur||0)+" (№"+(r.lvl||1)+(r.panel? ", "+r.panel : "")+")";
  if(r.ev==="flood") return (r.n||"")+" / 10 мин"+(r.panel? ", "+r.panel : "");
  return [r.d, r.role? "роль "+r.role : "", r.via? "через "+r.via : "", r.st? "код "+r.st : "", r.ua? "["+r.ua+"]" : ""].filter(Boolean).join(" · ");
}
function logAct(body){
  body.innerHTML="";
  function sel(id, opts){ return el("select",{id:id}, opts.map(function(o){ return el("option",{value:o[0]},[t(o[1])]); })); }
  var src=sel("acsrc",[["","ac_src_all"],["admin","ac_src_admin"],["player","ac_src_player"],["guard","ac_src_guard"]]);
  var ev=sel("acev",[["","ac_ev_all"],["logins","ac_ev_logins"],["bad","ac_ev_bad"],["req","ac_ev_req"],["ui","ac_ev_ui"],["audit","ac_ev_audit"],["guard","ac_ev_guard"]]);
  var hrs=sel("achrs",[["24","ac_h24"],["1","ac_h1"],["168","ac_h168"],["0","ac_hall"]]);
  var us=el("input",{id:"acuser",placeholder:t("ac_user"),style:"width:150px"}), ip=el("input",{id:"acip",placeholder:t("ac_ip"),style:"width:120px"}),
      tx=el("input",{id:"actext",placeholder:t("ac_text"),style:"width:200px"});
  [us,ip,tx].forEach(function(x){ x.addEventListener("keydown",function(e){ if(e.key==="Enter") pullAct(false); }); });
  [src,ev,hrs].forEach(function(x){ x.onchange=function(){ pullAct(false); }; });
  body.appendChild(el("p",{class:"muted small"},[t("ac_intro")]));
  body.appendChild(el("div",{class:"row",style:"margin-bottom:8px;flex-wrap:wrap;gap:6px"},[src,ev,us,ip,tx,
    el("label",{class:"small"},[t("ac_hours")+" ",hrs]),
    el("button",{class:"small pri",onclick:function(){ pullAct(false); }},[t("ac_find")]),
    el("span",{id:"acinfo",class:"muted small"},[])]));
  body.appendChild(el("div",{style:"overflow-x:auto"},[el("table",{id:"actb"},[])]));
  body.appendChild(el("div",{style:"margin-top:8px"},[el("button",{id:"acmore",class:"small",style:"display:none",onclick:function(){ pullAct(true); }},[t("ac_more")])]));
  pullAct(false);
}
function pullAct(more){
  var g=function(id){ return (($("#"+id)||{}).value||"").trim(); };
  var qs="src="+encodeURIComponent(g("acsrc"))+"&ev="+encodeURIComponent(g("acev"))+"&user="+encodeURIComponent(g("acuser"))
    +"&ip="+encodeURIComponent(g("acip"))+"&text="+encodeURIComponent(g("actext"))+"&hours="+encodeURIComponent(g("achrs"))+"&limit=300";
  if(more && AC_ROWS.length) qs+="&before="+AC_ROWS[AC_ROWS.length-1].t;
  api("/api/activity-log?"+qs).then(function(j){
    AC_ROWS = more? AC_ROWS.concat(j.rows) : j.rows;
    var tb=$("#actb"); if(!tb) return; tb.innerHTML="";
    tb.appendChild(el("tr",{},[t("ac_col_ts"),t("ac_col_src"),t("ac_col_who"),"IP",t("ac_col_ev"),t("ac_col_d")].map(function(x){ return el("th",{},[x]); })));
    if(!AC_ROWS.length) tb.appendChild(el("tr",{},[el("td",{colspan:"6",class:"muted"},[t("ac_none")])]));
    var BAD={login_fail:1,login_blocked:1,denied:1,probe:1,block:1,flood:1};
    AC_ROWS.forEach(function(r){
      var who=r.user? r.user+(r.uid!=null? " #"+r.uid : "") : "—";
      var wEl=el("a",{href:"#",onclick:function(e){ e.preventDefault(); $("#acuser").value=r.user||""; pullAct(false); }},[who]);
      var iEl=el("a",{href:"#",class:"mono",onclick:function(e){ e.preventDefault(); $("#acip").value=r.ip||""; pullAct(false); }},[r.ip||""]);
      tb.appendChild(el("tr",{style:BAD[r.ev]? "background:rgba(220,60,60,.10)" : (r.ev==="login_ok"? "background:rgba(60,180,90,.10)" : null)},[
        el("td",{class:"mono small",style:"white-space:nowrap"},[r.ts||""]),
        el("td",{class:"small"},[t("ac_src_"+(r.src||"admin"))]),
        el("td",{class:"small"},[wEl, r.role? el("span",{class:"muted"},[" · "+r.role]) : null]),
        el("td",{class:"small"},[iEl]),
        el("td",{class:"small",style:"white-space:nowrap"},[T[S.lang]["ev_"+r.ev]||r.ev]),
        el("td",{class:"small",style:"word-break:break-word"},[acDetail(r)])]));
    });
    var mb=$("#acmore"); if(mb) mb.style.display=j.more? "" : "none";
    var inf=$("#acinfo"); if(inf) inf.textContent=AC_ROWS.length+" · "+t("ac_size")+" "+(j.size/1048576).toFixed(1)+" МБ / "+j.files;
  }).catch(function(e){ var tb=$("#actb"); if(tb){ tb.innerHTML=""; tb.appendChild(el("tr",{},[el("td",{class:"msg err"},[errText(e)])])); } });
}
function logBlocks(body){
  body.innerHTML="";
  api("/api/guard").then(function(j){
    var R=j.rules||{};
    body.appendChild(el("p",{class:"muted small"},[t("gb_intro").replace("{ip}",R.ip[0]).replace("{ipw}",R.ip[1]/60).replace("{ac}",R.account[0])
      .replace("{acw}",R.account[1]/60).replace("{steps}",(R.steps||[]).map(acFmtT).join(" → "))]));
    body.appendChild(el("p",{},[t("gb_fails10")+": "+j.fails_10m, j.flood? el("b",{style:"color:#d33"},["  "+t("gb_flood")]) : null,
      " ", el("button",{class:"small",onclick:function(){ logBlocks(body); }},[t("refresh")])]));
    if(!j.keys.length){ body.appendChild(el("p",{class:"muted"},[t("gb_none")])); return; }
    body.appendChild(ltable([t("gb_key"),t("gb_left"),t("gb_lvl"),t("gb_fails"),t("gb_who"),""], j.keys, function(k){
      return [el("span",{class:"mono"},[k.key]), k.left? acFmtT(k.left) : "—", String(k.level), String(k.fails), k.who||"",
        el("button",{class:"small",onclick:function(){ api("/api/guard",{body:{op:"unblock",key:k.key}}).then(function(){ logBlocks(body); }).catch(function(e){ alert(errText(e)); }); }},[t("gb_unblock")])]; }));
  }).catch(function(e){ body.appendChild(el("div",{class:"msg err"},[errText(e)])); });
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

// ---- название и иконка панели (config: webui.title + favicon.img в base_dir) ----
function applyBrand(title, favV){
  if(title){ S.title=title; document.title=title; }
  if(favV!=null){ S.favV=favV; var hl=document.getElementById("hlogo"); if(hl) hl.src="/favicon.ico?v="+favV; }
  var lk=document.getElementById("favicon");
  if(lk) lk.href="/favicon.ico?v="+(favV||0);
}
function faviconCard(){
  var prev=el("img",{src:"/favicon.ico?v="+Date.now(),style:"width:48px;height:48px;border-radius:8px;border:1px solid var(--line);object-fit:contain;background:var(--panel2)"});
  var msg=el("span",{class:"muted small"},[]);
  var inp=el("input",{type:"file",accept:"image/png,image/x-icon,image/vnd.microsoft.icon,image/jpeg,image/gif,image/webp"});
  function done(r){ msg.textContent="✅"; prev.src="/favicon.ico?v="+Date.now(); applyBrand(null, r.v||Date.now()); }
  inp.addEventListener("change",function(){
    var f=inp.files[0]; if(!f) return;
    if(f.size>512*1024){ msg.textContent=t("fav_big"); return; }
    var rd=new FileReader();
    rd.onload=function(){ api("/api/favicon",{body:{data:rd.result}}).then(done).catch(function(e){ msg.textContent=errText(e); }); inp.value=""; };
    rd.readAsDataURL(f);
  });
  return el("div",{class:"card",style:"margin-top:12px"},[el("h3",{},[t("fav_title")]),
    el("div",{class:"row",style:"gap:12px;align-items:center;flex-wrap:wrap"},[prev, inp,
      el("button",{class:"small",onclick:function(){ api("/api/favicon",{body:{data:null}}).then(done).catch(function(e){ msg.textContent=errText(e); }); }},[t("fav_remove")]), msg]),
    el("p",{class:"muted small"},[t("fav_hint")])]);
}

// ---- boot ----
(function(){
  var th=localStorage.getItem("sw_theme"); if(th) document.documentElement.setAttribute("data-theme",th);
  document.addEventListener("visibilitychange",function(){ if(!document.hidden && S.authed && !S.must_change){
    if(S.tab==="dash") loadState(false); } });
  api("/api/session").then(function(j){
    S.authed=!!j.authed; S.user=j.username||""; S.csrf=j.csrf||""; S.must_change=!!j.must_change; S.role=j.role||"admin";
    applyBrand(j.title, j.favicon_v); render();
  }).catch(function(){ S.authed=false; render(); });
})();
</script>
</body>
</html>
"""
