# -*- coding: utf-8 -*-
"""Панель ИГРОКА (отдельно от админской webui.py, свой порт — по умолчанию 80).

Вход — ником и игровым паролем (``code`` из user<N>.json, те же данные, что
в игре). Пароль сравнивается на сервере и наружу не отдаётся. Игрок видит
только своё (профиль, инвентарь, исследования, свой клан) и общие агрегаты
(статус сервера, рейтинги). Всё только на чтение — ничего в файлы мира не пишет.

Запускается потоком внутри supervisor.py, если ``playerweb.enabled``.
``playerweb.allowed_nets`` пусто = пускать всех (панель для игроков); для
выхода в интернет — только через обратный прокси с HTTPS.
"""
import hashlib
import html
import http.cookies
import re
import collections
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import ThreadingHTTPServer

import game_i18n
import players
from webui import Throttle, _Handler, _ip_allowed, _parse_nets

SESSION_TTL = 12 * 3600
REMEMBER_TTL = 30 * 86400    # «Запомнить меня»: токен на 30 дней, на диске — только его хэш
REMEMBER_RECHECK = 60        # как часто сверять, не сменил ли игрок пароль в игре
LEADERS_CACHE_SEC = 300
MAP_IMG_CACHE_SEC = 600    # сырые пиксели карты (без клаймов) — одни на всех, туман накладывается на каждый запрос
FOG_RADIUS = 20            # клеток: видно вокруг персонажа и вокруг своих участков
FOG_RGB = (22, 25, 31)
SAMPLE_SEC = 120           # фоновый цикл: где бывали онлайн-игроки (открытые места карты), прогрев карт
HISTORY_SEC = 3600         # почасовой снимок уровня/рейтинга/техов всех игроков для «Моей истории»
MARKET_CACHE_SEC = 300      # полный проход по картам — десятки секунд, считаем в фоне


def _epoch(ts):
    """Время из журналов: игра пишет «24.09.2026 12:00:00», панель — «2026-09-24 12:00:00»."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(ts), fmt).timestamp()
        except ValueError:
            continue
    return 0.0


# ---------------------------------------------------------------- английский
# Серверные ответы собираются на русском (players.* отдаёт подписи из русской
# локализации). Для lang=en ответ проходит через _tr: точные подписи (предметы,
# способности, роли, специализации, ветки техов, слова панели) и шаблоны
# (названия техов «A / B +1», «ветка · тир N», карты, магазины). Ники, кланы и
# тексты чата не переводятся — такие ключи пропускаются.
_TECH_FAMILY_EN = {
    "Строительство": "Building", "Мосты": "Bridges", "Лодки/амфибии": "Boats/amphibians", "Автомобили": "Cars",
    "Двигатели авто": "Car engines", "Танки": "Tanks", "Хранение": "Storage", "Верстаки/энергия": "Workbenches/energy",
    "Ракеты": "Rockets", "Ракетные двигатели": "Rocket engines", "Радары": "Radars", "Сборщики": "Collectors",
    "Кислород/скафандры": "Oxygen/space suits", "Броня": "Armor", "Оружие ближнее/резаки": "Melee weapons/cutters",
    "Стрелковое": "Firearms", "Тяжёлое оружие": "Heavy weapons", "Корабельные пушки": "Ship guns",
    "Топоры/пилы": "Axes/saws", "Кирки/буры": "Pickaxes/drills", "Лопаты": "Shovels", "Мотыги": "Hoes",
    "Медицина/добыча": "Medicine/mining", "Молоты/ремонт": "Hammers/repair", "Ускорители": "Boosters",
    "Клан": "Clan", "Клан·роботы": "Clan·robots", "Клан·процессоры": "Clan·processors",
    "Клан·модули PMR": "Clan·PMR modules", "Клан·пульт роботов": "Clan·robot console", "Эндгейм": "Endgame",
}
_WORDS_EN = {
    "Лидер": "Leader", "Офицер": "Officer", "Участник": "Member", "Капрал": "Corporal",
    "Бой": "Combat", "Производство": "Production", "Наука": "Science", "Фермерство": "Farming", "Пилот": "Pilot",
    "Космос": "Space", "терминал": "terminal", "магазин": "shop",
    "В стопке": "Stack", "Прочность": "Durability", "Урон": "Damage", "Скорость атаки": "Attack speed",
    "Дальность": "Range", "Атак": "Attacks", "Щит": "Shield", "Питательность": "Nutrition", "Сытость": "Satiety",
    "Энергия": "Energy", "Ускорение добычи": "Mining boost", "Добыча за удар": "Mining per hit",
    "Топливо двигателя": "Engine fuel", "Топливо": "Fuel", "Слотов транспорта": "Vehicle slots", "Одежда": "Clothes",
    "еда": "food", "ингредиент микстур": "mixture ingredient", "растение": "plant", "одежда": "clothes",
    "инструмент": "tool", "строится": "buildable", "оружие техники": "vehicle weapon",
    "на земле": "on the ground", "хранилище": "storage",
    "каталог мира не найден": "world folder not found", "нет такого предмета": "no such item",
    "нет доступа к этой карте": "no access to this map", "нет такого канала": "no such channel",
    "карта не нарисовалась": "map failed to render", "нет атласа иконок": "no icon atlas",
}
_KIND_EN = {"планета": "planet", "спутник": "satellite", "астероид": "asteroid"}
_TR_SKIP = {"text", "nick", "to", "who", "owner", "clan", "clan_name", "ts", "first_seen", "where_raw"}
_TR_EXACT = None


def _tr_exact():
    global _TR_EXACT
    if _TR_EXACT is None:
        d = dict(_WORDS_EN)
        d.update(_TECH_FAMILY_EN)
        for slug, ru in players._ITEM_NAMES_RU.items():
            if game_i18n.ITEM_EN.get(slug):
                d.setdefault(ru, game_i18n.ITEM_EN[slug])
        for slug, ru in players._ABILITY_NAMES_RU.items():
            if game_i18n.ABILITY_EN.get(slug):
                d.setdefault(ru, game_i18n.ABILITY_EN[slug])
        for slug, ru in game_i18n.ITEM_INFO_RU.items():
            if game_i18n.ITEM_INFO_EN.get(slug):
                d.setdefault(ru, game_i18n.ITEM_INFO_EN[slug])
        _TR_EXACT = d
    return _TR_EXACT


_RX_KIND = re.compile(r"^(.*) \((планета|спутник|астероид)\)$")
_RX_MAP = re.compile(r"^карта (\d+)$")
_RX_SHOP = re.compile(r"^магазин · (.+) · (-?\d+), (-?\d+)$")
_RX_TECH = re.compile(r"^([A-Za-z]+\d*) — (.+)$")
_RX_TIER = re.compile(r"^(.+) · тир (\d+)$")
_RX_PLUS = re.compile(r"^(.*?)( \+\d+)$")


def _tr_str(s):
    ex = _tr_exact()
    if s in ex:
        return ex[s]
    m = _RX_KIND.match(s)
    if m:
        return "%s (%s)" % (m.group(1), _KIND_EN[m.group(2)])
    m = _RX_MAP.match(s)
    if m:
        return "map " + m.group(1)
    m = _RX_SHOP.match(s)
    if m:
        return "shop · %s · %s, %s" % (_tr_str(m.group(1)), m.group(2), m.group(3))
    m = _RX_TECH.match(s)
    if m:
        return "%s — %s" % (m.group(1), _tr_str(m.group(2)))
    m = _RX_TIER.match(s)
    if m:
        fam = m.group(1)
        fam = _TECH_FAMILY_EN.get(fam) or (("branch " + fam[6:]) if fam.startswith("ветка ") else fam)
        return "%s · tier %s" % (fam, m.group(2))
    if " / " in s or _RX_PLUS.match(s):   # название теха: «Деревянная стена / Деревянная дверь +1»
        m = _RX_PLUS.match(s)
        body, tail = (m.group(1), m.group(2)) if m else (s, "")
        parts = [ex.get(p) for p in body.split(" / ")]
        if all(parts):
            return " / ".join(parts) + tail
    if s.startswith("ветка "):
        return "branch " + s[6:]
    return s


def _tr(obj, key=None):
    if isinstance(obj, str):
        return obj if key in _TR_SKIP else _tr_str(obj)
    if isinstance(obj, list):
        return [_tr(x, key) for x in obj]
    if isinstance(obj, dict):
        return {k: (v if k in _TR_SKIP else _tr(v, k)) for k, v in obj.items()}
    return obj


class PlayerWeb:
    def __init__(self, cfg, state, ct_events=None, ct_points=None, tt_log=None, web=None):
        self.cfg = cfg
        self.state = state
        self.web = web               # админ-панель (WebUI) — для входа стаффа и /admin/ на этом порту
        base = cfg.get("base_dir") or os.path.dirname(os.path.abspath(__file__))
        self._base = base
        self._ct_events = ct_events or os.path.join(base, "logs", "clan_events.jsonl")
        self._ct_points = ct_points or os.path.join(base, "logs", "clan_points.jsonl")
        self._tt_log = tt_log or os.path.join(base, "logs", "tech_track.jsonl")
        self._sessions = {}          # tok -> {uid, nick, exp, fp?, chk?}
        self._slock = threading.Lock()
        self._remember_path = os.path.join(base, "playerweb_remember.json")
        self.throttle = Throttle()                                   # по IP
        self.nick_throttle = Throttle(max_fail=8, window=900, block=900)  # по нику (перебор с разных IP)
        self._cache = {}
        self._market = {"data": None, "ts": 0, "running": False}
        self._map_img = {}           # map -> (ts, {w, h, pixels})
        self._map_lock = threading.Lock()
        self._map_busy = set()       # карты, которые сейчас перерисовываются в фоне
        self._map_seen = {}          # map -> когда её последний раз смотрели (что держать тёплым)
        self._trade_own = {"terminals": [], "shops": []}   # с id владельцев — наружу только своё
        self._explored_path = os.path.join(base, "playerweb_explored.json")
        self._explored = players._read_json(self._explored_path) or {}   # "uid" -> {"map": [[bx, by], ...]}
        self._elock = threading.Lock()
        self._hist_path = os.path.join(base, "logs", "player_points.jsonl")
        self._stop = threading.Event()
        self._srv = None

    # ---------------------------------------------------------------- lifecycle
    def _pcfg(self):
        return self.cfg.get("playerweb") or {}

    def start(self):
        host = self._pcfg().get("host", "0.0.0.0")
        port = int(self._pcfg().get("port", 80))
        self._srv = ThreadingHTTPServer((host, port), _Handler)
        self._srv.daemon_threads = True
        self._srv.webui = self      # _Handler зовёт server.webui.dispatch
        threading.Thread(target=self._srv.serve_forever, name="playerweb", daemon=True).start()
        # рынок собирается минуты на больших мирах — прогреть заранее, чтобы первый игрок не ждал
        self._market["running"] = True
        t = threading.Timer(60, self._market_build)
        t.daemon = True
        t.start()
        threading.Thread(target=self._bg_loop, name="pw-bg", daemon=True).start()
        logging.info("playerweb: панель игроков на http://%s:%d/", host, port)

    def stop(self):
        self._stop.set()
        try:
            if self._srv:
                self._srv.shutdown()
                self._srv.server_close()
        except Exception:  # noqa: BLE001
            logging.exception("playerweb: ошибка остановки")

    # ------------------------------------------------------------ фоновый цикл
    def _bg_loop(self):
        """Раз в SAMPLE_SEC: блоки 8×8, где стоят онлайн-игроки (туман войны
        запоминает исследованное), прогрев карт онлайн-игроков; раз в час —
        снимок уровня/рейтинга/техов для «Моей истории»."""
        if self._stop.wait(30):
            return
        last_hist = 0
        for ln in players._read_text(self._hist_path, tail_bytes=200_000).splitlines()[-1:]:
            try:
                last_hist = json.loads(ln).get("t", 0)
            except ValueError:
                pass
        while not self._stop.is_set():
            warm = set()
            try:
                wd = players.find_world_dir(self.cfg)
                if wd:
                    warm = self._sample_explored(wd)
                    if time.time() - last_hist >= HISTORY_SEC:
                        self._history_snapshot(wd)
                        last_hist = time.time()
            except Exception:  # noqa: BLE001
                logging.exception("playerweb: фоновый цикл")
            now = time.time()
            warm |= {m for m, ts in self._map_seen.items() if now - ts < 3600}
            for mp in sorted(warm):
                if self._stop.is_set():
                    return
                hit = self._map_img.get(mp)
                if not hit or now - hit[0] >= MAP_IMG_CACHE_SEC - SAMPLE_SEC:
                    self._map_refresh(mp)
            if self._stop.wait(SAMPLE_SEC):
                return

    def _sample_explored(self, wd):
        """-> карты онлайн-игроков, у которых открыта панель (их стоит держать
        тёплыми: перерисовка карты — десятки секунд, всё подряд греть накладно)."""
        maps, changed = set(), False
        with self._slock:
            panel_uids = {v["uid"] for v in self._sessions.values()}
        for u, on in players._online_now(wd).items():
            if not on:
                continue
            raw = players._read_json(players._user_file(wd, u)) or {}
            pos = self._my_pos(wd, raw)
            if pos.get("map") in (None, 0) or pos.get("x") is None:
                continue
            if u in panel_uids:
                maps.add(pos["map"])
                maps.update(t.get("mapId") for t in raw.get("userTerritories") or [] if t.get("mapId"))
            b = [int(pos["x"]) // 8, int(pos["y"]) // 8]
            with self._elock:
                lst = self._explored.setdefault(str(u), {}).setdefault(str(pos["map"]), [])
                if b not in lst:
                    lst.append(b)
                    changed = True
        if changed:
            with self._elock:
                tmp = self._explored_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._explored, f, separators=(",", ":"))
                os.replace(tmp, self._explored_path)
        return maps

    def _history_snapshot(self, wd):
        tcost = players._load_ref(wd, "tech.json", "id", "cost")
        row = {}
        for u in players.load_user_list(wd):
            raw = players._read_json(players._user_file(wd, u)) or {}
            if not raw:
                continue
            techs = raw.get("techList") or []
            row[str(u)] = [raw.get("unitLevel"), raw.get("addRating"), len(techs),
                           round(float(raw.get("timeGame") or 0) / 3600.0, 1),
                           round(sum(tcost.get(t) or 0 for t in techs) / 60.0, 1)]
        os.makedirs(os.path.dirname(self._hist_path), exist_ok=True)
        with open(self._hist_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": int(time.time()), "u": row}, separators=(",", ":")) + "\n")
        logging.info("playerweb: снимок истории (%d игроков)", len(row))
        self._history_compact()

    def _history_compact(self, full_days=30):
        """Раз в сутки: старше ``full_days`` дней оставить один снимок в день
        (почасовые нужны только для свежего; иначе ~0,3 МБ/сутки на 370 игроков)."""
        if time.time() - getattr(self, "_hist_compacted", 0) < 86400:
            return
        self._hist_compacted = time.time()
        cut = time.time() - full_days * 86400
        keep, days, dropped = [], set(), 0
        for ln in players._read_text(self._hist_path).splitlines():
            try:
                t = json.loads(ln).get("t", 0)
            except ValueError:
                continue
            if t < cut:
                day = time.strftime("%Y-%m-%d", time.localtime(t))
                if day in days:
                    dropped += 1
                    continue
                days.add(day)
            keep.append(ln)
        if dropped:
            tmp = self._hist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(keep) + "\n")
            os.replace(tmp, self._hist_path)
            logging.info("playerweb: история прорежена — убрано %d старых почасовых снимков", dropped)

    # ------------------------------------------------------------------ helpers
    def _send(self, h, status, ctype, body, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            h.send_response(status)
            h.send_header("Content-Type", ctype)
            h.send_header("Content-Length", str(len(body)))
            h.send_header("X-Content-Type-Options", "nosniff")
            h.send_header("Referrer-Policy", "no-referrer")
            h.send_header("X-Frame-Options", "DENY")
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
        return self._send(h, status, "application/json; charset=utf-8",
                          json.dumps(obj, ensure_ascii=False, default=str), extra)

    def _body(self, h):
        try:
            n = min(int(h.headers.get("Content-Length") or 0), 4096)
        except ValueError:
            n = 0
        try:
            v = json.loads(h.rfile.read(n).decode("utf-8")) if n > 0 else {}
            return v if isinstance(v, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _thash(tok):
        return hashlib.sha256(tok.encode("utf-8")).hexdigest()

    def _code_fp(self, uid):
        """Отпечаток текущего игрового пароля: сменил пароль — запомненные входы гаснут."""
        code = players.player_code(self.cfg, uid) or ""
        return hashlib.sha256(("%s:%s" % (uid, code)).encode("utf-8")).hexdigest()[:24] if code else ""

    def _remember_load(self):
        d = players._read_json(self._remember_path) or {}
        return d if isinstance(d, dict) else {}

    def _remember_save(self, d):
        now = time.time()
        d = {k: v for k, v in d.items() if (v or {}).get("exp", 0) > now}
        tmp = self._remember_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, self._remember_path)

    def _session(self, h):
        c = http.cookies.SimpleCookie(h.headers.get("Cookie", ""))
        tok = c["psid"].value if "psid" in c else ""
        if not tok:
            return "", None
        now = time.time()
        with self._slock:
            s = self._sessions.get(tok)
            if not s:   # после перезапуска панели — поднять из «запомненных»
                r = self._remember_load().get(self._thash(tok))
                if r and r.get("exp", 0) > now:
                    s = {"uid": r["uid"], "nick": r["nick"], "exp": r["exp"], "fp": r.get("fp"), "chk": 0}
                    self._sessions[tok] = s
            if s and s["exp"] < now:
                self._sessions.pop(tok, None)
                s = None
            if s and s.get("fp") and now - s.get("chk", 0) > REMEMBER_RECHECK:
                if self._code_fp(s["uid"]) != s["fp"]:
                    self._sessions.pop(tok, None)
                    d = self._remember_load()
                    d.pop(self._thash(tok), None)
                    self._remember_save(d)
                    logging.info("playerweb: запомненный вход %s сброшен — пароль сменён", s["nick"])
                    return tok, None
                s["chk"] = now
        return tok, s

    def _title(self):
        return (self._pcfg().get("title") or "").strip() or \
            ((self.cfg.get("webui") or {}).get("title") or "").strip() or "Sigma World"

    def _cached(self, key, ttl, fn):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        d = fn()
        if d.get("ok"):
            self._cache[key] = (time.time(), d)
        return d

    # ---------------------------------------------------------------- dispatch
    def dispatch(self, h, method):
        ip = h.client_address[0]
        raw_nets = tuple(self._pcfg().get("allowed_nets") or [])
        if raw_nets:
            try:
                nets = _parse_nets(raw_nets)
            except ValueError:
                nets = []
            if not _ip_allowed(ip, nets):
                return self._send(h, 403, "text/plain; charset=utf-8", "403")
        try:
            path, _, qs = h.path.partition("?")
            q = urllib.parse.parse_qs(qs)
            if path in ("/", "/index.html") and method == "GET":
                return self._send(h, 200, "text/html; charset=utf-8",
                                  PAGE.replace("__TITLE__", html.escape(self._title())), {"Cache-Control": "no-store"})
            if path == "/admin" or path.startswith("/admin/"):
                if not (self.web and self._pcfg().get("admin_proxy")):
                    return self._send(h, 404, "text/plain; charset=utf-8", "not found")
                # только тем, кто уже вошёл в админку (кнопка «Админка» у стаффа) и сохранил роль;
                # остальным — 403 без формы входа: подбирать пароль здесь не через что
                if not self.web.session_ok(h):
                    return self._send(h, 403, "text/html; charset=utf-8",
                                      '<!doctype html><meta charset="utf-8"><title>403</title>'
                                      '<p style="font:16px system-ui;margin:40px">403 — админка открывается только '
                                      'через кнопку «Админка» в <a href="/">панели игрока</a> '
                                      '(для игроков с ролью в игре).</p>')
                if path == "/admin":
                    return self._send(h, 302, "text/plain", "", {"Location": "/admin/"})
                h.path = h.path[len("/admin"):]
                return self.web.dispatch(h, method, prefix="/admin")
            if path == "/favicon.ico":
                p = os.path.join(self._base, "favicon.img")
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        data = f.read()
                    return self._send(h, 200, "image/png" if data[:4] == b"\x89PNG" else "image/x-icon",
                                      data, {"Cache-Control": "max-age=3600"})
                return self._send(h, 404, "text/plain", b"")
            if not path.startswith("/api/"):
                return self._send(h, 404, "text/plain; charset=utf-8", "not found")
            route = path[5:].strip("/")
            # POST — только с нашим заголовком: кросс-сайтовая форма его не пошлёт
            if method == "POST" and h.headers.get("X-Requested-With") != "swp":
                return self._json(h, {"error": "csrf"}, 403)
            if route == "session" and method == "GET":
                tok, s = self._session(h)
                return self._json(h, {"authed": bool(s), "nick": s["nick"] if s else "", "title": self._title(),
                                      "staff": self._staff(s["uid"]) if s else None})
            if route == "login" and method == "POST":
                return self._api_login(h)
            if route == "admin-enter" and method == "POST":
                tok, s = self._session(h)
                if not s:
                    return self._json(h, {"error": "auth"}, 401)
                return self._api_admin_enter(h, s)
            tok, s = self._session(h)
            if not s:
                return self._json(h, {"error": "auth"}, 401)
            if route == "logout" and method == "POST":
                with self._slock:
                    if (self._sessions.pop(tok, None) or {}).get("fp"):
                        d = self._remember_load()
                        d.pop(self._thash(tok), None)
                        self._remember_save(d)
                return self._json(h, {"ok": True}, set_cookie="psid=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
            if method != "GET":
                return self._json(h, {"error": "unknown"}, 404)
            fn = getattr(self, "_api_" + route.replace("-", "_"), None)
            if not fn:
                return self._json(h, {"error": "unknown"}, 404)
            d = fn(s["uid"], q)
            if (q.get("lang") or [""])[0] == "en" and isinstance(d, dict) and route not in ("chat", "events"):
                d = _tr(d)
            if isinstance(d, (bytes, bytearray)):
                ttl = 86400 if route == "item-icons-png" else 60
                return self._send(h, 200, "image/png", bytes(d), {"Cache-Control": "private, max-age=%d" % ttl})
            return self._json(h, d, 200 if d.get("ok") else 404)
        except Exception:  # noqa: BLE001
            logging.exception("playerweb: %s %s", method, getattr(h, "path", "?"))
            try:
                self._json(h, {"error": "internal"}, 500)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------- login
    def _api_login(self, h):
        ip = h.client_address[0]
        b = self._body(h)
        nick = str(b.get("nick") or "").strip()
        code = str(b.get("code") or "")
        key = "n:" + nick.lower()
        for thr, k in ((self.throttle, ip), (self.nick_throttle, key)):
            ok, wait = thr.check(k)
            if not ok:
                return self._json(h, {"error": "throttled", "retry": wait}, 429)
        if not nick or not code or len(nick) > 64 or len(code) > 128:
            return self._json(h, {"error": "bad_login"}, 403)
        world_dir = players.find_world_dir(self.cfg)
        uid = None
        if world_dir:
            for i, n in players.load_user_list(world_dir).items():
                if n.strip().lower() == nick.lower():
                    real = players.player_code(self.cfg, i) or ""
                    if real and secrets.compare_digest(real.encode("utf-8"), code.encode("utf-8")):
                        uid, nick = i, n
                        break
        if uid is None:
            self.throttle.fail(ip)
            self.nick_throttle.fail(key)
            logging.info("playerweb: неудачный вход «%s» с %s", nick[:32], ip)
            return self._json(h, {"error": "bad_login"}, 403)
        self.throttle.ok(ip)
        self.nick_throttle.ok(key)
        tok = secrets.token_urlsafe(32)
        remember = bool(b.get("remember"))
        with self._slock:
            now = time.time()
            for t in [t for t, s in self._sessions.items() if s["exp"] < now]:
                self._sessions.pop(t, None)
            s = {"uid": uid, "nick": nick, "exp": now + (REMEMBER_TTL if remember else SESSION_TTL)}
            if remember:
                s.update(fp=self._code_fp(uid), chk=now)
                d = self._remember_load()
                d[self._thash(tok)] = {"uid": uid, "nick": nick, "exp": s["exp"], "fp": s["fp"]}
                self._remember_save(d)
            self._sessions[tok] = s
        logging.info("playerweb: вход %s (#%s) с %s%s", nick, uid, ip, " (запомнить)" if remember else "")
        # без «запомнить» — кука живёт до закрытия браузера
        cookie = "psid=%s; Path=/; HttpOnly; SameSite=Strict" % tok
        if remember:
            cookie += "; Max-Age=%d" % REMEMBER_TTL
        return self._json(h, {"ok": True, "nick": nick}, set_cookie=cookie)

    # ------------------------------------------------------------ вход в админку
    def _staff(self, uid):
        """Игрок с ролью выше обычной -> кнопка «Админка» (если включён /admin/ на этом порту)."""
        if not (self.web and self._pcfg().get("admin_proxy")):
            return None
        role = self.web.game_panel_role(uid)
        if not role:
            return None
        return {"role": role, "linked": self.web.auth.user_for_game(uid) is not None}

    def _api_admin_enter(self, h, s):
        """Вход в админку по ОТДЕЛЬНОМУ админскому паролю (первый раз — задать его)."""
        if not (self.web and self._pcfg().get("admin_proxy")):
            return self._json(h, {"error": "unknown"}, 404)
        ip = h.client_address[0]
        key = "a:%s" % s["uid"]
        for thr, k in ((self.throttle, ip), (self.nick_throttle, key)):
            ok, wait = thr.check(k)
            if not ok:
                return self._json(h, {"error": "throttled", "retry": wait}, 429)
        b = self._body(h)
        st, d, cookie = self.web.enter_as_game_user(h, s["uid"], s["nick"], str(b.get("password") or ""),
                                                   str(b.get("new_password") or ""), ip)
        if d.get("error") == "bad_password":
            self.throttle.fail(ip)
            self.nick_throttle.fail(key)
        elif d.get("ok"):
            self.throttle.ok(ip)
            self.nick_throttle.ok(key)
        return self._json(h, d, st, set_cookie=cookie)

    # --------------------------------------------------------------------- api
    def _api_me(self, uid, q):
        """Своя карточка — белым списком из player_detail (без железа/страны/IP)."""
        d = players.player_detail(self.cfg, uid)
        if not d.get("ok"):
            return d
        p, a = d.get("profile") or {}, d.get("avatar") or {}

        def inv(rows):
            return [{"name": players.item_label(r.get("name")), "count": r.get("count"),
                     "durability": r.get("durability")} for r in rows or []]
        return {
            "ok": True, "id": d["id"], "name": d["name"], "online": d.get("online"),
            "profile": {k: p.get(k) for k in ("level", "clan_id", "clan_name", "clan_role", "clan_point",
                                              "rating", "playtime_h", "first_seen", "banned", "ban_expires_in_h")},
            "research": {k: (d.get("research") or {}).get(k)
                         for k in ("current", "current_name", "remaining_min", "done_count", "tech_list",
                                   "invested_h", "booster")},
            "position": self._with_map_names(d.get("position") or {}),
            "avatar": {"params": a.get("params"), "long_params": a.get("long_params"), "skills": a.get("skills"),
                       "abilities": a.get("abilities"),
                       "stash": inv(a.get("stash")), "carry": inv(a.get("carry")),
                       "stash_size": a.get("stash_size"), "carry_size": a.get("carry_size")},
            "sessions": {k: (d.get("sessions") or {}).get(k) for k in ("total", "total_h", "avg_min", "max_min")},
            "friends": self._friends(d.get("friends") or []),
            "deaths": ((d.get("activity") or {}).get("deaths") or [])[:20],
            "rewards": ((d.get("activity") or {}).get("rewards") or [])[:50],
        }

    def _friends(self, rows):
        """Друзья — как «Сейчас на сервере»: ник, уровень, клан, в игре ли."""
        wd = players.find_world_dir(self.cfg)
        if not wd or not rows:
            return []
        clans, online = players.load_clans(wd), players._online_now(wd)
        out = []
        for f in rows:
            raw = players._read_json(players._user_file(wd, f["id"])) or {} if f.get("id") is not None else {}
            c = clans.get(raw.get("clanId") or 0)
            out.append({"name": f.get("name"), "level": raw.get("unitLevel"),
                        "clan": c["name"] if c else "", "online": bool(online.get(f["id"]))})
        out.sort(key=lambda x: (not x["online"], -(x["level"] or 0), (x["name"] or "").lower()))
        return out

    _KIND_RU = {"planet": "планета", "satellite": "спутник", "asteroid": "астероид"}

    def _map_names(self):
        """{id карты: название} — id карты мира == id объекта звёздной системы
        (Data\\world\\star1.json), 0 — космос."""
        def build():
            so = players.space_objects(self.cfg, 1)
            names = {o["id"]: "%s (%s)" % (o["name"], self._KIND_RU[o["kind"]]) if o.get("kind") in self._KIND_RU
                     else o["name"] for o in so.get("objects") or [] if o.get("name")}
            return {"ok": bool(so.get("ok")), "names": names}
        return self._cached("map_names", 600, build).get("names") or {}

    def _with_map_names(self, pos):
        names = self._map_names()

        def nm(mp):
            return "Космос" if mp == 0 else names.get(mp) or ("карта %s" % mp if mp is not None else "")
        pos = dict(pos)
        pos["map_name"] = nm(pos.get("map"))
        if pos.get("respawn"):
            pos["respawn"] = dict(pos["respawn"], map_name=nm(pos["respawn"].get("map")))
        pos["territories"] = [dict(t, map_name=nm(t.get("map"))) for t in pos.get("territories") or []]
        return pos

    def _api_tech_tree(self, uid, q):
        return self._cached("tech_tree", 600, lambda: players.tech_tree(self.cfg))

    def _api_craft_catalog(self, uid, q):
        return self._cached("craft_catalog", 600, lambda: players.craft_catalog(self.cfg))

    def _api_craft_plan(self, uid, q):
        g = lambda k: (q.get(k) or [""])[0]
        return players.craft_plan(self.cfg, g("item")[:80], g("qty") or 1, uid, None)

    # ------------------------------------------------------------------ рынок
    def _market_build(self):
        """Из trade_report — только то, что и так видно в игре у терминала/магазина:
        что продают, за что, продавец и его клан, где стоит магазин. Без выручки,
        склада терминала, простоя и онлайна продавцов."""
        try:
            d = players.trade_report(self.cfg)
            if d.get("ok"):
                names = self._map_names()
                offers = []
                for o in d.get("offers") or []:
                    where = "терминал"
                    if o.get("src") == "shop":
                        parts = (o.get("where") or "").replace("map ", "").replace(" @ ", ",").split(",")
                        try:
                            mp, x, y = (int(v) for v in parts[:3])
                            where = "магазин · %s · %d, %d" % ("Космос" if mp == 0 else names.get(mp) or "карта %d" % mp, x, y)
                        except ValueError:
                            where = "магазин"
                    offers.append({"src": o.get("src"), "owner_id": (o.get("owner") or {}).get("id"),
                                   "owner": (o.get("owner") or {}).get("name"), "clan": (o.get("owner") or {}).get("clan"),
                                   "where": where, "unit": o.get("unit"),
                                   "give": [{"name": x["name"], "count": x["count"]} for x in o.get("give") or []],
                                   "want": [{"name": x["name"], "count": x["count"]} for x in o.get("want") or []]})
                it = lambda xs: [{"name": x["name"], "count": x["count"]} for x in xs or []]
                names = self._map_names()
                self._trade_own = {
                    "terminals": [{"owner_id": t["owner"]["id"], "sales": t["sales"], "lots": t["lots"],
                                   "idle_h": t.get("idle_h"), "storage": it(t.get("storage"))}
                                  for t in d.get("terminals") or []],
                    "shops": [{"owner_id": sh["owner"]["id"], "sales": sh["sales"], "slots": sh["slots"],
                               "where": "%s · %d, %d" % ("Космос" if sh["map"] == 0 else names.get(sh["map"]) or "карта %s" % sh["map"], sh["x"], sh["y"]),
                               "map": sh["map"], "x": sh["x"], "y": sh["y"], "storage": it(sh.get("storage"))}
                              for sh in d.get("shops") or []]}
                self._market.update(data={"ok": True, "offers": offers}, ts=time.time())
            else:
                logging.warning("playerweb: рынок — %s", d.get("error"))
        except Exception:  # noqa: BLE001
            logging.exception("playerweb: рынок")
        finally:
            self._market["running"] = False

    def _api_market(self, uid, q):
        m = self._market
        now = time.time()
        if now - m["ts"] > MARKET_CACHE_SEC and not m["running"]:
            m["running"] = True
            threading.Thread(target=self._market_build, name="pw-market", daemon=True).start()
        if not m["data"]:
            return {"ok": True, "pending": True}
        out = []
        for o in m["data"]["offers"]:
            o = dict(o)
            o["mine"] = o.pop("owner_id") == uid
            out.append(o)
        return {"ok": True, "offers": out, "age_s": int(now - m["ts"]), "refreshing": m["running"]}

    def _api_my_trade(self, uid, q):
        """Своя торговля: терминал, магазины, свои лоты (из того же фонового прохода)."""
        m = self._market
        if not m["data"]:
            return {"ok": True, "pending": True}
        strip = lambda r: {k: v for k, v in r.items() if k != "owner_id"}
        mine = [dict(o, mine=True) for o in m["data"]["offers"] if o.get("owner_id") == uid]
        return {"ok": True, "age_s": int(time.time() - m["ts"]),
                "terminals": [strip(t) for t in self._trade_own["terminals"] if t["owner_id"] == uid],
                "shops": [strip(sh) for sh in self._trade_own["shops"] if sh["owner_id"] == uid],
                "offers": [{k: v for k, v in o.items() if k != "owner_id"} for o in mine]}

    # ------------------------------------------------------------ иконки предметов
    def _icons(self):
        """Атлас иконок (``build_item_icons.py`` → item_icons.png/.json в base_dir).
        Графика игры в репозиторий не кладётся — нет файлов, нет и иконок."""
        pj = os.path.join(self._base, "item_icons.json")
        try:
            mt = os.path.getmtime(pj)
        except OSError:
            return None
        hit = self._cache.get("icons")
        if hit and hit[0] == mt:
            return hit[1]
        d = players._read_json(pj) or {}
        # подписи на русском -> slug: многие ответы API отдают предметы по имени
        d["label"] = {players.item_label(k): k for k in d.get("idx") or {}}
        d["label"].update({game_i18n.ITEM_EN[k]: k for k in d.get("idx") or {} if game_i18n.ITEM_EN.get(k)})
        d["ok"] = True
        d["v"] = int(mt)      # версия в URL картинки: пересобрали атлас — браузер не возьмёт старый из кэша
        self._cache["icons"] = (mt, d)
        return d

    def _api_item_icons(self, uid, q):
        return self._icons() or {"ok": True, "none": True}

    def _api_item_icons_png(self, uid, q):
        try:
            with open(os.path.join(self._base, "item_icons.png"), "rb") as f:
                return f.read()
        except OSError:
            return {"ok": False, "error": "нет атласа иконок"}

    # ------------------------------------------------------------------ история
    def _api_history(self, uid, q):
        """Мои графики по часовым снимкам + техи задним числом из журнала трекинга."""
        pts = []
        for ln in players._read_text(self._hist_path, tail_bytes=30_000_000).splitlines():
            try:
                p = json.loads(ln)
            except ValueError:
                continue
            v = (p.get("u") or {}).get(str(uid))
            if v:
                pts.append({"t": p["t"], "level": v[0], "rating": v[1], "techs": v[2], "play_h": v[3], "research_h": v[4]})
        # техи до начала снимков — восстановить назад от первого снимка по tech_gained
        wd = players.find_world_dir(self.cfg)
        raw = players._read_json(players._user_file(wd, uid)) if wd else {}
        n = len(raw.get("techList") or [])
        gains = []
        for ln in players._read_text(self._tt_log, tail_bytes=20_000_000).splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("uid") == uid and e.get("kind") == "tech_gained":
                try:
                    gains.append((_epoch(e["ts"]), int(e.get("count") or 0)))
                except (KeyError, TypeError, ValueError):
                    continue
        tser, cur = [{"t": int(time.time()), "v": n}], n
        for ts, c in sorted(gains, reverse=True):
            tser.append({"t": int(ts), "v": cur})
            cur -= c
            tser.append({"t": int(ts) - 1, "v": cur})
        tser.reverse()
        return {"ok": True, "points": pts, "techs": tser,
                "since": pts[0]["t"] if pts else None}

    # ---------------------------------------------------------------- справочник
    _STAT_RU = [("stack", "В стопке"), ("durability", "Прочность"), ("damage", "Урон"), ("speedAttack", "Скорость атаки"),
                ("rangeAttack", "Дальность"), ("countAttack", "Атак"), ("shield", "Щит"), ("nutrition", "Питательность"),
                ("eat", "Сытость"), ("energy", "Энергия"), ("accelerationMining", "Ускорение добычи"),
                ("countMining", "Добыча за удар"), ("engineFuel", "Топливо двигателя"), ("itemFuel", "Топливо"),
                ("transportSlot", "Слотов транспорта"), ("clothes", "Одежда")]

    def _api_handbook(self, uid, q):
        """Без ?item — список предметов; с ?item=<slug> — карточка: свойства, как
        получить (рецепт/станок, техи), куда идёт, сколько лотов на рынке."""
        wd = players.find_world_dir(self.cfg)
        if not wd:
            return {"ok": False, "error": "каталог мира не найден"}
        by_id, by_name = players._items_full(wd)
        slug = (q.get("item") or [""])[0][:80]
        if not slug:
            return self._cached("handbook", 600, lambda: {"ok": True, "items": sorted(
                ({"id": d["name"], "name": players.item_label(d["name"])} for d in by_id.values() if d.get("name")),
                key=lambda x: x["name"].lower())})
        d = by_name.get(slug)
        if not d:
            return {"ok": False, "error": "нет такого предмета"}
        cd = players._craft_data(wd) or {"recipes": {}, "machine": {}, "uses": {}}
        known = set((players._read_json(players._user_file(wd, uid)) or {}).get("techList") or [])
        r = cd["recipes"].get(slug)
        recipe = None
        if r:
            recipe = {"out": r["count"], "time": r["time"], "workbench": players.item_label(r["workbench"]) if r["workbench"] else "",
                      "tech": players.tech_label(wd, r["tech"]) if r["tech"] else "", "tech_known": (r["tech"] in known) if r["tech"] else None,
                      "res": [{"id": rid, "name": players.item_label(rid), "n": n} for rid, n in r["res"]]}
        mach = [{"machine": players.item_label(mid), "machine_id": mid, "from": players.item_label(mat), "from_id": mat, "energy": en}
                for mid, mat, en in cd["machine"].get(slug) or []]
        uses = sorted({u for u in cd["uses"].get(slug) or []}, key=lambda u: players.item_label(u).lower())
        stats = [[lbl, d[k]] for k, lbl in self._STAT_RU if d.get(k) not in (None, 0, False, "")]
        label = players.item_label(slug)
        mk = self._market["data"]
        on_market = None
        if mk:
            on_market = {"sell": sum(1 for o in mk["offers"] if any(x["name"] == label for x in o["give"])),
                         "buy": sum(1 for o in mk["offers"] if any(x["name"] == label for x in o["want"]))}
        en = (q.get("lang") or [""])[0] == "en"
        info = (game_i18n.ITEM_INFO_EN if en else game_i18n.ITEM_INFO_RU).get(slug) or ""
        return {"ok": True, "id": slug, "name": label, "info": info, "stats": stats, "recipe": recipe, "machine": mach,
                "used_in": [{"id": u, "name": players.item_label(u)} for u in uses],
                "flags": [f for k, f in (("isProduct", "еда"), ("isBuff", "ингредиент микстур"), ("isPlant", "растение"),
                                         ("isClothes", "одежда"), ("tool", "инструмент"), ("build", "строится"),
                                         ("isMachineWeapon", "оружие техники")) if d.get(k)],
                "on_market": on_market}

    # ---------------------------------------------------------------------- чат
    def _api_chat(self, uid, q):
        """Общие каналы — целиком; клановый — только сообщения нынешних участников
        моего клана (в логе игры клановые чаты всех кланов в одном файле, клан у
        строки не записан); личные — только мои (от меня и мне)."""
        wd = players.find_world_dir(self.cfg)
        if not wd:
            return {"ok": False, "error": "каталог мира не найден"}
        ch = (q.get("ch") or ["global"])[0]
        qq = ((q.get("q") or [""])[0] or "").strip().lower()[:80]
        names = players.load_user_list(wd)
        me = names.get(uid)
        if ch == "private":
            rows = []
            for ln in players._read_text(os.path.join(wd, "Logs", "chat_privat.txt")).splitlines():
                m = players._PRIV_RX.match(ln)
                if m and me and (m.group(2) == me or m.group(3) == me):
                    rows.append({"ts": m.group(1), "nick": m.group(2), "to": m.group(3), "text": m.group(4),
                                 "out": m.group(2) == me})
        else:
            if ch not in ("global", "global2", "ru", "clan"):
                return {"ok": False, "error": "нет такого канала"}
            rows = [r for r in players._all_chat(wd) if r["channel"] == ch]
            if ch == "clan":
                cid = self._my_clan_id(uid)
                c = next((x for x in players._clans_raw(wd) if x.get("id") == cid), None) if cid else None
                mates = {names.get(u.get("userId")) for u in (c or {}).get("users") or []}
                rows = [r for r in rows if r["nick"] in mates] if c else []
            rows = [{"ts": r["ts"], "nick": r["nick"], "text": r["text"]} for r in rows]
        if qq:
            rows = [r for r in rows if qq in r["text"].lower() or qq in r["nick"].lower()]
        return {"ok": True, "ch": ch, "total": len(rows), "messages": rows[-300:][::-1]}

    def _api_events(self, uid, q):
        """Лента сервера: новые игроки, смерти, события кланов (создан/распущен/переименован)."""
        d = self._cached("events", 60, lambda: players.server_events(self.cfg, 400, ["register", "death"]))
        ev = [{"ts": e["ts"], "epoch": e["epoch"], "kind": e["kind"], "who": e["actor"], "detail": e.get("detail") or ""}
              for e in d.get("events") or []]
        for ln in players._read_text(self._ct_events, tail_bytes=1_000_000).splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("kind") in ("created", "disbanded", "renamed"):
                ev.append({"ts": e.get("ts"), "epoch": _epoch(e.get("ts")), "kind": "clan_" + e["kind"],
                           "who": e.get("clan_name") or "", "detail": e.get("was") or ""})
        ev.sort(key=lambda e: -(e["epoch"] or 0))
        return {"ok": True, "events": ev[:300]}

    # ------------------------------------------------------------------ сундуки
    def _map_containers(self, wd, mp):
        """Все непустые хранилища карты (разбор map<N>.dt — секунды), кэш 5 мин на всех."""
        key = ("containers", mp)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        path = os.path.join(wd, "Data", "maps", "map%d.dt" % mp)
        d = players.mapdt.list_containers(path, world_dir=wd, item_names=players.load_items(wd), cap=10 ** 6, min_items=1)
        if d.get("ok"):
            self._cache[key] = (time.time(), d)
        return d

    def _api_my_chests(self, uid, q):
        """Что лежит в хранилищах на МОИХ участках: сундуки и прочие ёмкости + брошенное
        на землю. Природные клады (slot underground) не показываем — это находка
        для лопаты, а не «моё»."""
        wd = players.find_world_dir(self.cfg)
        if not wd or players.mapdt is None:
            return {"ok": False, "error": "каталог мира не найден"}
        raw = players._read_json(players._user_file(wd, uid)) or {}
        mine = {}
        for t in raw.get("userTerritories") or []:
            p = t.get("pos") or {}
            if t.get("mapId") is not None and p.get("x") is not None:
                mine.setdefault(t["mapId"], set()).add((int(p["x"]), int(p["y"])))
        blocks, items = players._load_blocks(wd), players.load_items(wd)
        names = self._map_names()
        out = []
        for mp, blk in sorted(mine.items()):
            d = self._map_containers(wd, mp)
            if not d.get("ok"):
                continue
            rows = []
            for c in d.get("containers") or []:
                if c["slot"] == "underground" or (c["x"] // 8, c["y"] // 8) not in blk:
                    continue
                bslug = blocks.get(c.get("block")) if c.get("block") is not None else None
                what = players.item_label(bslug) if bslug and bslug in players._ITEM_NAMES_RU else \
                    (players._block_label(bslug) if bslug else "")
                ground = c["slot"] == "ground_inv"
                merged = collections.OrderedDict()      # одинаковые стопки в одном хранилище — одной строкой
                for it in c["items"]:
                    merged[it["type"]] = merged.get(it["type"], 0) + it["count"]
                rows.append({"x": c["x"], "y": c["y"], "ground": ground, "block": "" if ground else (bslug or ""),
                             "what": "на земле" if ground else (what or "хранилище"),
                             "items": [{"id": items.get(t) or "", "name": players.item_label(items.get(t)) or "#%s" % t, "count": n}
                                       for t, n in merged.items()],
                             "total": c["total"]})
            rows.sort(key=lambda r: (r["ground"], -r["total"]))
            out.append({"map": mp, "name": names.get(mp) or "карта %s" % mp, "territories": len(blk), "containers": rows})
        return {"ok": True, "maps": out}

    # ------------------------------------------------------------ карта своих участков
    def _my_map_ids(self, uid):
        wd = players.find_world_dir(self.cfg)
        raw = players._read_json(players._user_file(wd, uid)) if wd else {}
        ids = [t.get("mapId") for t in raw.get("userTerritories") or []]
        if raw.get("mapId") is not None:
            ids.append(raw.get("mapId"))
        with self._elock:   # и карты, где бывал раньше
            ids += [int(k) for k, v in (self._explored.get(str(uid)) or {}).items() if v]
        return wd, raw, [m for m in dict.fromkeys(ids) if m not in (None, 0)]

    def _api_my_maps(self, uid, q):
        wd, raw, ids = self._my_map_ids(uid)
        names = self._map_names()
        out = []
        for mp in ids:
            dim = players.map_dim(wd, mp)
            if not dim:
                continue
            out.append({"map": mp, "name": names.get(mp) or "карта %s" % mp, "w": dim["w"], "h": dim["h"],
                        "here": mp == raw.get("mapId"),
                        "territories": [{"x": (t.get("pos") or {}).get("x"), "y": (t.get("pos") or {}).get("y")}
                                        for t in raw.get("userTerritories") or [] if t.get("mapId") == mp]})
        out.sort(key=lambda x: -len(x["territories"]))
        return {"ok": True, "maps": out, "me": self._my_pos(wd, raw), "fog_radius": FOG_RADIUS}

    @staticmethod
    def _fog(w, h, pixels, rects, r):
        """Туман войны: всё закрашено FOG_RGB, кроме «скруглённых» прямоугольников
        ``rects`` (x0, y0, x1, y1 — игровые клетки), расширенных на ``r`` клеток.
        Строки картинки идут сверху, игровой y = h-1-строка."""
        out = bytearray(bytes(FOG_RGB) * (w * h))
        for x0, y0, x1, y1 in rects:
            for gy in range(max(0, int(y0 - r)), min(h - 1, int(y1 + r)) + 1):
                dy = 0 if y0 <= gy <= y1 else min(abs(gy - y0), abs(gy - y1))
                if dy > r:
                    continue
                half = int((r * r - dy * dy) ** 0.5)
                a, b = max(0, int(x0) - half), min(w - 1, int(x1) + half)
                if a > b:
                    continue
                row = (h - 1 - gy) * w * 3
                out[row + a * 3:row + (b + 1) * 3] = pixels[row + a * 3:row + (b + 1) * 3]
        return bytes(out)

    def _api_my_map_image(self, uid, q):
        """PNG карты с туманом войны: видно только FOG_RADIUS клеток вокруг
        персонажа и вокруг своих участков — туман накладывается ЗДЕСЬ, целая карта
        наружу не уходит. Сырые пиксели кэшируются на всех (отрисовка — десятки
        секунд). Отдаём только карты, где у игрока есть участки или где он сейчас."""
        try:
            mp = int((q.get("map") or [""])[0])
        except ValueError:
            return {"ok": False, "error": "bad map"}
        wd, raw, ids = self._my_map_ids(uid)
        if mp not in ids:
            return {"ok": False, "error": "нет доступа к этой карте"}
        self._map_seen[mp] = time.time()
        m = self._map_get(mp)
        if not m.get("ok"):
            return m
        rects = [(t["x"] * 8, t["y"] * 8, t["x"] * 8 + 7, t["y"] * 8 + 7)
                 for t in ((tt.get("pos") or {}) for tt in raw.get("userTerritories") or [] if tt.get("mapId") == mp)
                 if t.get("x") is not None and t.get("y") is not None]
        me = self._my_pos(wd, raw)
        if me.get("map") == mp and me.get("x") is not None:
            rects.append((me["x"], me["y"], me["x"], me["y"]))
        with self._elock:   # где бывал раньше — тоже открыто (центр блока 8×8)
            rects += [(bx * 8 + 4, by * 8 + 4, bx * 8 + 4, by * 8 + 4)
                      for bx, by in (self._explored.get(str(uid)) or {}).get(str(mp)) or []]
        return players.mapdt._png_bytes(m["w"], m["h"], self._fog(m["w"], m["h"], m["pixels"], rects, FOG_RADIUS), 1)

    def _map_refresh(self, mp):
        """Перерисовать карту (десятки секунд) — не больше одной перерисовки на карту."""
        with self._map_lock:
            if mp in self._map_busy:
                return None
            self._map_busy.add(mp)
        try:
            d = players.map_pixels(self.cfg, mp)
            if d.get("ok"):
                self._map_img[mp] = (time.time(), d)
            return d
        except Exception:  # noqa: BLE001
            logging.exception("playerweb: карта %s", mp)
            return None
        finally:
            with self._map_lock:
                self._map_busy.discard(mp)

    def _map_get(self, mp):
        """Кэш со «старым, пока обновляется»: устаревшая картинка отдаётся сразу,
        перерисовка идёт в фоне; ждать приходится только самый первый раз."""
        hit = self._map_img.get(mp)
        if hit:
            if time.time() - hit[0] >= MAP_IMG_CACHE_SEC and mp not in self._map_busy:
                threading.Thread(target=self._map_refresh, args=(mp,), daemon=True).start()
            return hit[1]
        for _ in range(600):         # кто-то уже рисует её — дождаться
            if mp not in self._map_busy:
                break
            time.sleep(0.2)
        hit = self._map_img.get(mp)
        if hit:
            return hit[1]
        return self._map_refresh(mp) or {"ok": False, "error": "карта не нарисовалась"}

    @staticmethod
    def _my_pos(wd, raw):
        pos = ((players._read_json(os.path.join(wd, "Data", "units", "unit%s.json" % raw["unitId"])) or {}).get("pos")
               if raw.get("unitId") is not None else None) or {}
        return {"map": raw.get("mapId"), "x": pos.get("x"), "y": pos.get("y")}

    def _my_clan_id(self, uid):
        wd = players.find_world_dir(self.cfg)
        raw = players._read_json(players._user_file(wd, uid)) if wd else {}
        return raw.get("clanId") or 0

    def _api_clan(self, uid, q):
        cid = self._my_clan_id(uid)
        if not cid:
            return {"ok": True, "none": True}
        d = players.clan_detail(self.cfg, cid)
        if d.get("ok"):
            # техи/исследования/часы участников — их личное, в панель игрока не отдаём
            d.pop("coverage", None)
            d["members"] = [{k: m.get(k) for k in ("id", "name", "role", "role_name", "rating", "clan_point",
                                                   "online", "level", "last_seen_h", "spec")}
                            for m in d.get("members") or []]
            d["history"] = players.clan_history(self.cfg, cid, self._ct_events, self._ct_points, 30)
        return d

    def _api_server(self, uid, q):
        snap = self.state.data.get("last_snapshot") or {}
        wd = players.find_world_dir(self.cfg)
        online = []
        if wd:
            names = players.load_user_list(wd)
            clans = players.load_clans(wd)
            for u, on in players._online_now(wd).items():
                if not on:
                    continue
                raw = players._read_json(players._user_file(wd, u)) or {}
                c = clans.get(raw.get("clanId") or 0)
                online.append({"name": names.get(u) or raw.get("name") or ("id %s" % u),
                               "level": raw.get("unitLevel"), "clan": c["name"] if c else ""})
            online.sort(key=lambda x: (-(x["level"] or 0), x["name"].lower()))
        lb = self._cached("leaders", LEADERS_CACHE_SEC,
                          lambda: players.leaderboards(self.cfg, self._tt_log, self._ct_points))
        ser = self._cached("online24", 120, lambda: players.online_series_recent(self.cfg, 24))
        rt = self._cached("rating", LEADERS_CACHE_SEC, lambda: players.rating_top(self.cfg, 10))
        # «id N» — аккаунта уже нет в user_list (удалён) — игрокам такие строки ни к чему
        strip = lambda rows: [{"name": r.get("name"), "v": r.get("v")} for r in rows or []
                              if not str(r.get("name") or "").startswith("id ")]
        return {
            "ok": True,
            "game_up": bool((snap.get("game") or {}).get("running")),
            "snapshot_age": int(time.time() - snap["ts"]) if snap.get("ts") else None,
            "online": online,
            "online_series": [{"t": p["t"], "v": p["n"]} for p in ser.get("series") or []],
            "online_peak": ser.get("peak"),
            "rating": [{"name": r["name"], "reward": r["reward"]} for r in rt.get("top") or []],
            "traders": strip(lb.get("traders")),
            "clans": [{"name": c.get("name"), "rating": c.get("rating"), "growth": c.get("growth")}
                      for c in lb.get("clans") or []],
        }


PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="icon" href="/favicon.ico">
<style>
:root{--bg:#0f1216;--panel:#171c22;--panel2:#1e252d;--line:#2b333d;--fg:#e7ecf1;--mut:#93a1b0;
  --acc:#4c8dff;--ok:#3fb950;--warn:#d29922;--err:#f85149}
@media (prefers-color-scheme: light){:root{--bg:#f4f6f8;--panel:#fff;--panel2:#eef1f4;--line:#d7dde3;
  --fg:#1b2229;--mut:#5b6670;--acc:#1f6feb;--ok:#1a7f37;--warn:#9a6700;--err:#cf222e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5;flex-wrap:wrap}
header h1{font-size:16px;margin:0}
header .sp{flex:1}
nav{display:flex;gap:4px;flex-wrap:wrap;padding:8px 16px;border-bottom:1px solid var(--line)}
nav button.on{background:var(--acc);border-color:var(--acc);color:#fff}
main{padding:16px;max-width:1200px;margin:0 auto}
button,input,select{font:inherit;color:var(--fg);background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:7px 12px}
button{cursor:pointer} button:hover{border-color:var(--acc)}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px}
.card h3{margin:0 0 10px;font-size:15px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px}
.kv div:nth-child(odd){color:var(--mut)}
.muted{color:var(--mut)} .small{font-size:12px}
.msg{padding:8px 12px;border-radius:8px;border:1px solid var(--line)} .msg.err{border-color:var(--err);color:var(--err)}
table{border-collapse:collapse;width:100%} th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:500;font-size:12px}
.chips{display:flex;flex-wrap:wrap;gap:5px} .chip{border:1px solid var(--line);border-radius:12px;padding:1px 9px;font-size:12px}
.pill{border-radius:10px;padding:1px 8px;font-size:12px;border:1px solid var(--line)}
.pill.ok{color:var(--ok);border-color:var(--ok)} .pill.warn{color:var(--warn);border-color:var(--warn)} .pill.err{color:var(--err);border-color:var(--err)}
.bar{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden} .bar>i{display:block;height:100%;background:var(--acc)}
.login{max-width:340px;margin:60px auto} .login input{width:100%;margin-bottom:10px}
.scroll{max-height:420px;overflow:auto}
/* карточка-колонка: .grow забирает всю оставшуюся высоту (ряд грида тянет карточки до самой высокой) */
.card.fill{display:flex;flex-direction:column}
.card.fill>.grow{flex:1 1 0;min-height:80px;max-height:none;overflow:auto;align-content:flex-start}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ico{display:inline-block;flex:0 0 auto;background-repeat:no-repeat;vertical-align:middle}
.iname{display:inline-flex;align-items:center;gap:6px}
.ibtn{display:inline-flex;align-items:center;gap:6px;padding:3px 10px 3px 5px;border:1px solid var(--line);border-radius:14px;background:var(--panel2);color:var(--fg);cursor:pointer;font:inherit;line-height:1.3}
.ibtn:hover{border-color:var(--acc)} .ibtn .n{color:var(--mut);font-size:12px}
.ibtns{display:flex;flex-wrap:wrap;gap:6px}
@media (max-width:600px){main{padding:10px}
  nav{flex-wrap:nowrap;overflow-x:auto;padding:6px 10px;scrollbar-width:none} nav::-webkit-scrollbar{display:none}
  nav button{flex:0 0 auto} header{padding:8px 10px}}
</style>
</head>
<body>
<header><h1 id="ttl">__TITLE__</h1><span class="sp"></span><span id="who" class="muted"></span><button id="admbtn" type="button" style="display:none"></button><button id="lang" type="button" title="Русский / English"></button></header>
<nav id="nav" style="display:none"></nav>
<main id="main"></main>
<script>
// ---- язык: RU по умолчанию для ru/uk/be браузеров, иначе EN; строки интерфейса
// обёрнуты в L("русский текст") -> английский из словаря ниже
var LANG=(function(){ try{ var v=localStorage.getItem("swp_lang"); if(v==="ru"||v==="en") return v; }catch(e){}
  return /^(ru|uk|be)/i.test(navigator.language||"")? "ru" : "en"; })();
var LOC=LANG==="en"? "en" : "ru";
var EN_DICT={
  "Админка":"Admin",
  "Новый админский пароль (от 8 символов)":"New admin password (8+ chars)",
  "Админский пароль":"Admin password",
  "Повторите пароль":"Repeat password",
  "Пароли не совпадают":"Passwords do not match",
  "Неверный админский пароль":"Wrong admin password",
  "Пароль: не короче 8 символов, не игровой и не ник":"Password: 8+ chars, not your game password and not your nickname",
  "Админ-панель":"Admin panel",
  "Задайте отдельный админский пароль — не такой, как в игре. Он понадобится при каждом входе в админку.":"Set a separate admin password — different from your game one. You will need it every time you enter the admin panel.",
  "Введите ваш админский пароль (не игровой).":"Enter your admin password (not the game one).",
  "Отмена":"Cancel",
  "Сундуки":"Chests",
  "Мои сундуки":"My chests",
  "Найти предмет в сундуках":"Find an item in chests",
  "Где (X, Y)":"Where (X, Y)",
  "Лежит":"Contents",
  "участков: ":"territories: ",
  "хранилищ: ":"storages: ",
  "Всего на моих участках":"Total on my territories",
  "Сундуки и прочие хранилища на ваших участках, а также брошенное на землю. Данные обновляются раз в 5 минут.":"Chests and other storages on your territories, plus items dropped on the ground. Data refreshes every 5 minutes.",
  "Неверный ник или пароль":"Wrong nickname or password",
  "Слишком много попыток, подождите":"Too many attempts, please wait",
  "Сессия истекла":"Session expired",
  "Ошибка сервера":"Server error",
  "Нет связи с сервером":"No connection to the server",
  "Загрузка…":"Loading…",
  " ч ":" h ",
  " мин":" min",
  "Ник в игре":"In-game nickname",
  "Пароль из игры":"In-game password",
  " с)":" s)",
  "Вход для игроков":"Player login",
  "Ник и пароль — те же, что при входе на сервер.":"Same nickname and password you use to join the server.",
  "Запомнить меня на 30 дней":"Remember me for 30 days",
  "Войти":"Log in",
  "Профиль":"Profile",
  "История":"History",
  "Изучение":"Research",
  "Крафт":"Craft",
  "Справочник":"Handbook",
  "Рынок":"Market",
  "Карта":"Map",
  "Клан":"Clan",
  "Чат":"Chat",
  "Сервер":"Server",
  "Выйти":"Log out",
  "Энергия":"Energy",
  "Сытость":"Satiety",
  "Здоровье":"Health",
  "Меткость":"Accuracy",
  "Скорость движения":"Movement speed",
  "Скорость действия":"Action speed",
  "Скорость атаки":"Attack speed",
  "Генетика A":"Genetics A",
  "Генетика B":"Genetics B",
  "Генетика C":"Genetics C",
  "Генетика D":"Genetics D",
  "Кислород":"Oxygen",
  "Очки генетики":"Genetic points",
  "Опыт":"Experience",
  "Уровень":"Level",
  "Очки распределения":"Distribution points",
  "пусто":"empty",
  "Предмет":"Item",
  "Кол-во":"Qty",
  "Прочность":"Durability",
  "Статус":"Status",
  "в игре":"in game",
  "не в игре":"offline",
  "Лидер":"Leader",
  "Офицер":"Officer",
  "Участник":"Member",
  "Капрал":"Corporal",
  "Очки клана":"Clan points",
  "Рейтинг":"Rating",
  "Наиграно":"Played",
  " ч":" h",
  "Сессий":"Sessions",
  " · в среднем ":" · avg ",
  "Первый вход":"First seen",
  "Бан":"Ban",
  "ещё ":"",
  "да":"yes",
  "Исследования":"Research",
  "Сейчас изучается":"Researching now",
  " · осталось ":" · left ",
  "ничего":"nothing",
  "Изучено технологий":"Technologies researched",
  "Вложено времени":"Time invested",
  "Ускорители":"Boosters",
  "Аватар":"Avatar",
  "Способности":"Abilities",
  "нет":"none",
  "нет территорий":"no territories",
  "Где я":"Where I am",
  "Координаты":"Coordinates",
  "Точка возрождения":"Respawn point",
  "Мои территории":"My territories",
  "Склад":"Storage",
  "С собой":"Carrying",
  "Друзья (":"Friends (",
  "Игрок":"Player",
  "Последние смерти":"Recent deaths",
  "Когда":"When",
  "Что":"What",
  "Награды за рейтинг":"Rating rewards",
  "Награда":"Reward",
  "изучено":"researched",
  "изучается":"researching",
  "доступно":"available",
  "закрыто":"locked",
  "\nВремя: ":"\nTime: ",
  "\nОткрывает: ":"\nUnlocks: ",
  "Нажмите на технологию — покажу путь до неё и сколько осталось.":"Click a technology to see the path to it and how much is left.",
  "Изучено ":"Researched ",
  " · доступно сейчас ":" · available now ",
  " · закрыто ":" · locked ",
  "Осталось: ":"Left: ",
  " шаг(ов) · ~":" step(s) · ~",
  "Открывает: ":"Unlocks: ",
  "Сейчас":"Now",
  "Изучается":"Researching",
  "Осталось":"Left",
  "Схема изучения":"Research tree",
  "Что скрафтить?":"What to craft?",
  "Время крафта: ":"Craft time: ",
  " с":" s",
  " · нужно: ":" · needs: ",
  "Сырьё":"Raw materials",
  "Ресурс":"Resource",
  "Нужно":"Needed",
  "Промежуточное":"Intermediate",
  "Крафтов":"Crafts",
  "Технологии":"Technologies",
  "Технология":"Technology",
  "У меня":"Mine",
  "нет · ещё ":"no · ",
  " шаг(ов), ~":" more step(s), ~",
  "Раскладка до сырья":"Raw material breakdown",
  "Посчитать":"Calculate",
  " за 1":" per 1",
  " шт":" pcs",
  "Предмет, например: Железный слиток":"Item, e.g. Iron ingot",
  "продают":"selling",
  "просят взамен":"asked in return",
  "везде":"anywhere",
  "покупают":"buying",
  "Цена за 1 шт":"Price per 1 pc",
  "Товар":"Goods",
  "Сделка":"Deal",
  "Платят":"Paid in",
  "Мин":"Min",
  "Медиана":"Median",
  "Макс":"Max",
  "Лотов":"Lots",
  "Предложения (":"Offers (",
  ", показаны первые ":", showing first ",
  "Отдаёт":"Gives",
  "Просит":"Asks",
  "Курс":"Rate",
  "Продавец":"Seller",
  "Где":"Where",
  "моё":"mine",
  "ничего не нашлось":"nothing found",
  "предложений нет":"no offers",
  "Собираю предложения со всех карт — это до пары минут, страница обновится сама…":"Collecting offers from all maps — this can take a couple of minutes, the page will update by itself…",
  "Обновлено ":"Updated ",
  "только что":"just now",
  " мин назад":" min ago",
  " · всего предложений: ":" · total offers: ",
  " · обновляю в фоне…":" · refreshing in background…",
  "Терминалы игроков и магазины на картах. Курс и цены — только для простых лотов «один товар за одну валюту»; сводка цен появляется при поиске.":"Player terminals and shops on maps. Rates and prices only for simple lots \"one item for one currency\"; the price summary appears when you search.",
  "Терминал":"Terminal",
  "  · лотов ":"  · lots ",
  " · продаж ":" · sales ",
  " · не заходил ":" · idle ",
  "На складе терминала: ":"In terminal storage: ",
  "Магазин · ":"Shop · ",
  "  · слотов ":"  · slots ",
  "Выручка: ":"Revenue: ",
  "Отдаю":"I give",
  "Прошу":"I ask",
  "Моя торговля":"My trade",
  "У вас пока нет участков.":"You have no territories yet.",
  " · участков: ":" · territories: ",
  " · вы здесь":" · you are here",
  "мой участок ":"my territory ",
  "вы здесь":"you are here",
  "загрузка карты…":"loading map…",
  "карта не загрузилась":"map failed to load",
  "Участки на этой карте (":"Territories on this map (",
  "Участок (X, Y)":"Territory (X, Y)",
  "Клетки":"Cells",
  "показать":"show",
  "Мои участки на карте":"My territories on the map",
  "повернуть против часовой на 45°":"rotate 45° counter-clockwise",
  "повернуть по часовой на 45°":"rotate 45° clockwise",
  "ко мне":"to me",
  "Видно ":"You see ",
  " клеток вокруг вас и вокруг ваших участков — остальное скрыто туманом. Зелёные квадраты — ваши участки, красная точка — вы. Карту можно тащить мышью, колесо — масштаб.":" cells around you and your territories — the rest is hidden by fog. Green squares are your territories, the red dot is you. Drag the map with the mouse, wheel to zoom.",
  "Моя история":"My history",
  "Панель записывает снимок раз в час с ":"The panel records a snapshot every hour since ",
  " — графики будут расти со временем.":" — the charts will grow over time.",
  "Снимки ещё не делались — первый появится в течение часа.":"No snapshots yet — the first one will appear within an hour.",
  "техов":"techs",
  "дата":"date",
  "уровень":"level",
  "рейтинг":"rating",
  "часов":"hours",
  "Вложено в исследования":"Invested in research",
  " (выходит ":" (yields ",
  " шт)":" pcs)",
  "в руках":"by hand",
  "Время":"Time",
  "изучена":"researched",
  "не изучена":"not researched",
  "не нужна":"not needed",
  "Станок":"Machine",
  "Из чего":"From",
  "Не крафтится — добывается или находится в мире.":"Not craftable — mined or found in the world.",
  "Используется в (":"Used in (",
  "На рынке: продают — ":"On the market: selling — ",
  " лот(ов), просят взамен — ":" lot(s), asked in return — ",
  "Искать на рынке":"Find on market",
  "Открыть в справочнике":"Open in handbook",
  "Справочник предметов":"Item handbook",
  "Свойства, как получить, какая технология нужна и куда предмет идёт дальше.":"Properties, how to get it, which technology is needed and what it is used for.",
  "Общий":"General",
  "Русский":"Russian",
  "Личные":"Private",
  "События":"Events",
  "убит":"killed",
  "сброс позиции":"position reset",
  "голод":"starvation",
  "задохнулся":"suffocated",
  "газ":"gas",
  "мох":"moss",
  "новый игрок":"new player",
  "смерть":"death",
  "создан клан":"clan created",
  "распущен клан":"clan disbanded",
  "клан переименован":"clan renamed",
  "Поиск по нику или тексту":"Search by nickname or text",
  "События сервера":"Server events",
  "Кто":"Who",
  "было: ":"was: ",
  "пока пусто":"nothing yet",
  " · сообщений: ":" · messages: ",
  " (последние 300)":" (last 300)",
  "Сообщения нынешних участников вашего клана.":"Messages from current members of your clan.",
  "Только ваши личные сообщения — от вас и вам.":"Only your private messages — from you and to you.",
  "сообщений нет":"no messages",
  "Только чтение, обновляется раз в 20 секунд.":"Read-only, refreshes every 20 seconds.",
  "вступил":"joined",
  "ушёл":"left",
  "роль":"role",
  "клан-технология":"clan technology",
  "переименован":"renamed",
  "слоты":"slots",
  "создан":"created",
  "распущен":"disbanded",
  "сейчас: ":"now: ",
  " · график появится, когда накопятся данные":" · the chart will appear once data accumulates",
  "мало данных":"not enough data",
  "время":"time",
  "Вы не состоите в клане.":"You are not in a clan.",
  "Участников":"Members",
  "Сейчас в игре":"In game now",
  "Клан-технологий":"Clan technologies",
  "Состав":"Members",
  "Роль":"Role",
  "Специализация":"Specialization",
  "Был":"Last seen",
  "сейчас":"now",
  " ч назад":" h ago",
  " дн назад":" d ago",
  "Рост клана (30 дней)":"Clan growth (30 days)",
  "очки":"points",
  "участников":"members",
  "работает":"running",
  "не запущен":"not running",
  "Сейчас онлайн":"Online now",
  "Пик за сутки":"Peak in 24 h",
  "Онлайн за 24 часа":"Online over 24 hours",
  "игроков":"players",
  "пока нет данных":"no data yet",
  "Сезонный рейтинг":"Season rating",
  " ускор.":" boosters",
  "Торговцы":"Traders",
  "Продаж":"Sales",
  "Кланы":"Clans",
  "За 7 дней":"Last 7 days",
  "Сейчас на сервере (":"On the server now (",
  "никого":"nobody"
};
function L(s){ return LANG==="en"? (EN_DICT[s]!=null? EN_DICT[s] : s) : s; }
document.documentElement.lang=LANG;

var S={nick:"",tab:"me"};
try{ S.tab=localStorage.getItem("swp_tab")||"me"; }catch(e){}
var $=function(s){ return document.querySelector(s); };
function el(tag,a,kids){ var e=document.createElement(tag); a=a||{};
  for(var k in a){ if(k==="class") e.className=a[k]; else if(k.slice(0,2)==="on") e.addEventListener(k.slice(2),a[k]); else if(a[k]!=null) e.setAttribute(k,a[k]); }
  (kids||[]).forEach(function(c){ if(c==null) return; e.appendChild(typeof c==="object"? c : document.createTextNode(String(c))); });
  return e; }
function svgEl(tag,a,kids){ var e=document.createElementNS("http://www.w3.org/2000/svg",tag);
  for(var k in (a||{})) if(a[k]!=null) e.setAttribute(k,a[k]);
  (kids||[]).forEach(function(c){ if(c!=null) e.appendChild(typeof c==="string"? document.createTextNode(c):c); }); return e; }
function api(p,body){
  var o={headers:{}}; if(body!==undefined){ o.method="POST"; o.headers["Content-Type"]="application/json"; o.headers["X-Requested-With"]="swp"; o.body=JSON.stringify(body); }
  if(LANG==="en" && p.indexOf("/api/")===0 && p.indexOf("item-icons-png")<0) p+=(p.indexOf("?")<0?"?":"&")+"lang=en";
  return fetch(p,o).then(function(r){
    if(r.status===401){ S.nick=""; render(); throw {error:"auth"}; }
    return r.json().then(function(j){ if(!r.ok) throw j; return j; }); });
}
var ERR={bad_login:L("Неверный ник или пароль"), throttled:L("Слишком много попыток, подождите"), auth:L("Сессия истекла"), internal:L("Ошибка сервера")};
function errText(e){ return (e&&(ERR[e.error]||e.error))||L("Нет связи с сервером"); }
function errBox(e){ return el("div",{class:"msg err"},[errText(e)]); }
function card(title,kids){ return el("div",{class:"card"},[title? el("h3",{},[title]):null].concat(kids)); }
function table(head,rows,mk){ var t=el("table",{},[el("tr",{},head.map(function(h){ return el("th",{},[h]); }))]);
  rows.forEach(function(r){ t.appendChild(el("tr",{},mk(r).map(function(c){ return el("td",{},[c]); }))); }); return t; }
function kv(pairs){ var d=el("div",{class:"kv"}); pairs.forEach(function(p){ if(p[1]==null||p[1]==="") return; d.appendChild(el("div",{},[p[0]])); d.appendChild(el("div",{},[p[1]])); }); return d; }
function load(box,path,draw){ box.innerHTML=""; box.appendChild(el("p",{class:"muted"},[L("Загрузка…")]));
  api(path).then(function(d){ box.innerHTML=""; draw(d); }).catch(function(e){ box.innerHTML=""; box.appendChild(errBox(e)); }); }
function fmtMin(m){ if(m==null) return ""; var h=Math.floor(m/60), mm=Math.round(m%60); return h? h+L(" ч ")+mm+L(" мин") : mm+L(" мин"); }

// ---------------------------------------------------------------- вход
function renderLogin(){
  // настоящая <form> с name/autocomplete — браузер сам предложит сохранить ник и пароль
  var nick=el("input",{name:"username",placeholder:L("Ник в игре"),autocomplete:"username"});
  var code=el("input",{name:"password",type:"password",placeholder:L("Пароль из игры"),autocomplete:"current-password"});
  var rem=el("input",{type:"checkbox",id:"rem",style:"width:auto;margin:0"});
  try{ rem.checked=localStorage.getItem("swp_rem")!=="0"; }catch(e){ rem.checked=true; }
  var msg=el("div",{class:"msg err",style:"display:none"});
  function go(ev){ ev.preventDefault(); msg.style.display="none";
    try{ localStorage.setItem("swp_rem",rem.checked?"1":"0"); }catch(e){}
    api("/api/login",{nick:nick.value,code:code.value,remember:rem.checked}).then(function(d){ S.nick=d.nick; render(); })
      .catch(function(e){ msg.textContent=errText(e)+(e.retry? " ("+e.retry+L(" с)"):""); msg.style.display=""; }); }
  $("#main").appendChild(el("form",{class:"login card",method:"post",action:"#",onsubmit:go},[el("h3",{},[L("Вход для игроков")]),
    el("p",{class:"muted small"},[L("Ник и пароль — те же, что при входе на сервер.")]), nick, code,
    el("label",{for:"rem",class:"row small",style:"margin:0 0 12px;cursor:pointer"},[rem,L("Запомнить меня на 30 дней")]),
    el("button",{class:"pri",type:"submit",style:"width:100%"},[L("Войти")]), el("div",{style:"margin-top:10px"},[msg])]));
}

// ---------------------------------------------------------------- каркас
var TABS=[["me",L("Профиль")],["hist",L("История")],["tech",L("Изучение")],["craft",L("Крафт")],["book",L("Справочник")],["market",L("Рынок")],["map",L("Карта")],["chests",L("Сундуки")],["clan",L("Клан")],["chat",L("Чат")],["server",L("Сервер")]];
// иконки предметов: атлас item_icons.png, клетка по индексу; ключ — slug или русское имя
var ICONS=null;
function ico(key,size){
  if(!ICONS||ICONS.none||!key) return null;
  var slug=(ICONS.idx[key]!=null)? key : ICONS.label[key], i=ICONS.idx[slug];
  if(i==null) return null;
  size=size||20;
  return el("span",{class:"ico",style:"background-image:url(/api/item-icons-png?v="+ICONS.v+");width:"+size+"px;height:"+size+"px;background-position:-"+((i%ICONS.cols)*size)+"px -"+(Math.floor(i/ICONS.cols)*size)+"px;"
    +"background-size:"+(ICONS.cols*size)+"px auto"});
}
// кнопка-плашка предмета: иконка + название (+ количество); по клику — действие
function itemBtn(id,name,n,onclick,title){
  return el("button",{class:"ibtn",type:"button",title:title||null,onclick:function(e){ e.preventDefault(); onclick(id); }},
    [ico(id,20), name, n!=null? el("span",{class:"n"},["×"+n]) : null]);
}
function goBook(id){ try{ localStorage.setItem("swp_book",id); }catch(e){} S.tab="book"; render(); window.scrollTo(0,0); }
function goCraft(id){ try{ localStorage.setItem("swp_craft_go",id); }catch(e){} S.tab="craft"; render(); window.scrollTo(0,0); }
function withIco(key,label,size){ var i=ico(key,size); return i? el("span",{class:"iname"},[i,label]) : label; }
// ---- вход в админку для стаффа: отдельный админский пароль (первый раз — задать)
function openAdminEnter(){
  var first=!(S.staff&&S.staff.linked);
  var p1=el("input",{type:"password",autocomplete:first?"new-password":"current-password",placeholder:first?L("Новый админский пароль (от 8 символов)"):L("Админский пароль"),style:"width:100%;margin-bottom:8px"});
  var p2=first? el("input",{type:"password",autocomplete:"new-password",placeholder:L("Повторите пароль"),style:"width:100%;margin-bottom:8px"}) : null;
  var msg=el("div",{class:"msg err",style:"display:none;margin-top:8px"});
  var bg=el("div",{style:"position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:50;display:flex;align-items:center;justify-content:center;padding:16px"});
  function close(){ bg.remove(); }
  function go(ev){ ev.preventDefault(); msg.style.display="none";
    if(first&&p1.value!==p2.value){ msg.textContent=L("Пароли не совпадают"); msg.style.display=""; return; }
    api("/api/admin-enter", first? {new_password:p1.value} : {password:p1.value}).then(function(d){
      if(d.ok){ location.href=d.url||"/admin/"; return; }
      if(d.need_setup){ S.staff.linked=false; close(); openAdminEnter(); } })
      .catch(function(e){ msg.textContent= e.error==="bad_password"? L("Неверный админский пароль") : e.error==="weak"? L("Пароль: не короче 8 символов, не игровой и не ник") : errText(e)+(e.retry? " ("+e.retry+L(" с)"):""); msg.style.display=""; }); }
  var form=el("form",{class:"card",style:"max-width:380px;width:100%",onsubmit:go},[
    el("h3",{},[L("Админ-панель")]),
    el("p",{class:"muted small"},[first? L("Задайте отдельный админский пароль — не такой, как в игре. Он понадобится при каждом входе в админку.") : L("Введите ваш админский пароль (не игровой).")]),
    p1, p2, el("div",{class:"row"},[el("button",{class:"pri",type:"submit"},[L("Войти")]), el("button",{type:"button",onclick:close},[L("Отмена")])]), msg]);
  bg.addEventListener("click",function(e){ if(e.target===bg) close(); });
  bg.appendChild(form); document.body.appendChild(bg); p1.focus();
}
function setLang(v){ try{ localStorage.setItem("swp_lang",v); }catch(e){} location.reload(); }
function render(){
  var ab=$("#admbtn"); if(ab){ ab.style.display=(S.nick&&S.staff)?"":"none"; ab.textContent=L("Админка"); ab.onclick=openAdminEnter; }
  var lb=$("#lang"); if(lb){ lb.textContent=LANG==="en"? "RU" : "EN"; lb.onclick=function(){ setLang(LANG==="en"? "ru" : "en"); }; }
  if(S.nick && ICONS===null){ ICONS={none:true};
    api("/api/item-icons").then(function(d){ ICONS=d; if(!d.none) render(); }).catch(function(){}); }
  var m=$("#main"), nav=$("#nav"), who=$("#who"); m.innerHTML=""; nav.innerHTML=""; who.innerHTML="";
  if(!S.nick){ nav.style.display="none"; return renderLogin(); }
  nav.style.display="";
  who.appendChild(document.createTextNode(S.nick+"  "));
  who.appendChild(el("button",{onclick:function(){ api("/api/logout",{}).finally(function(){ S.nick=""; render(); }); }},[L("Выйти")]));
  TABS.forEach(function(t){ nav.appendChild(el("button",{class:S.tab===t[0]?"on":"",onclick:function(){
    S.tab=t[0]; try{ localStorage.setItem("swp_tab",S.tab); }catch(e){} render(); }},[t[1]])); });
  ({me:tabMe,hist:tabHist,tech:tabTech,craft:tabCraft,book:tabBook,market:tabMarket,map:tabMap,chests:tabChests,clan:tabClan,chat:tabChat,server:tabServer}[S.tab]||tabMe)(m);
}

// ---------------------------------------------------------------- профиль
var PARAM={0:L("Энергия"),1:L("Сытость"),2:L("Здоровье"),3:L("Меткость"),4:L("Скорость движения"),5:L("Скорость действия"),6:L("Скорость атаки"),
  7:L("Генетика A"),8:L("Генетика B"),9:L("Генетика C"),10:L("Генетика D"),11:L("Кислород"),12:L("Очки генетики")};
var LPARAM={0:L("Опыт"),1:L("Уровень"),2:L("Очки распределения")};
function invTable(rows){ if(!rows||!rows.length) return el("div",{class:"muted"},[L("пусто")]);
  return el("div",{class:"scroll"},[table([L("Предмет"),L("Кол-во"),L("Прочность")],rows,function(r){ return [withIco(r.name,r.name,24), r.count, r.durability==null?"":r.durability]; })]); }
function tabMe(m){
  var box=el("div"); m.appendChild(box);
  load(box,"/api/me",function(d){
    var p=d.profile, r=d.research;
    var head=card(d.name,[kv([
      [L("Статус"), el("span",{class:"pill "+(d.online?"ok":"")},[d.online?L("в игре"):L("не в игре")])],
      [L("Уровень"),p.level],[L("Клан"),p.clan_name? p.clan_name+(p.clan_role!=null?" ("+[L("Лидер"),L("Офицер"),L("Участник"),L("Капрал")][p.clan_role]+")":""):"—"],
      [L("Очки клана"),p.clan_point],[L("Рейтинг"),p.rating],[L("Наиграно"),p.playtime_h+L(" ч")],
      [L("Сессий"),d.sessions.total? d.sessions.total+L(" · в среднем ")+d.sessions.avg_min+L(" мин"):""],
      [L("Первый вход"),p.first_seen],
      p.banned? [L("Бан"), el("span",{class:"pill err"},[p.ban_expires_in_h? L("ещё ")+p.ban_expires_in_h+L(" ч"):L("да")])] : ["",""] ]),
      el("h3",{style:"margin-top:14px"},[L("Исследования")]),
      kv([[L("Сейчас изучается"), r.current? r.current_name+(r.remaining_min!=null?L(" · осталось ")+fmtMin(r.remaining_min):"") : L("ничего")],
        [L("Изучено технологий"), r.done_count],[L("Вложено времени"), r.invested_h+L(" ч")],[L("Ускорители"), r.booster]])]);
    var params=(d.avatar.params||[]).map(function(x){
      var pct=x.max? Math.max(0,Math.min(100,100*x.val/x.max)):null;
      return el("div",{style:"margin-bottom:6px"},[el("div",{class:"row small",style:"justify-content:space-between"},[
        el("span",{},[PARAM[x.type]||("#"+x.type)]), el("span",{class:"muted"},[(Math.round(x.val*10)/10)+(x.max?" / "+Math.round(x.max*10)/10:"")])]),
        pct!=null? el("div",{class:"bar"},[el("i",{style:"width:"+pct+"%"})]) : null]); });
    var lp=(d.avatar.long_params||[]).map(function(x){ return [LPARAM[x.type]||("#"+x.type), x.val]; });
    var av=card(L("Аватар"),[kv(lp), el("div",{style:"margin-top:10px"},params)]);
    var ab=card(L("Способности")+((d.avatar.abilities||[]).length?" ("+d.avatar.abilities.length+")":""),[(d.avatar.abilities||[]).length?
      el("div",{class:"chips grow"},d.avatar.abilities.map(function(a){ return el("span",{class:"chip"},[a]); }))
      : el("div",{class:"muted"},[L("нет")])]);
    var pos=d.position||{};
    var terr=(pos.territories||[]).length? el("div",{class:"grow"},[table([L("Карта"),"X","Y"],pos.territories,function(t){ return [t.map_name,t.x,t.y]; })])
      : el("div",{class:"muted"},[L("нет территорий")]);
    var where=card(L("Где я"),[kv([[L("Карта"),pos.map_name],[L("Координаты"),pos.x!=null? Math.round(pos.x)+", "+Math.round(pos.y):""],
      [L("Точка возрождения"),pos.respawn? pos.respawn.map_name+" · "+Math.round(pos.respawn.x||0)+", "+Math.round(pos.respawn.y||0):""]]),
      el("div",{class:"muted small",style:"margin:10px 0 4px"},[L("Мои территории")]), terr]);
    ab.classList.add("fill"); where.classList.add("fill");
    box.appendChild(el("div",{class:"grid"},[head,av,ab,where]));
    box.appendChild(el("div",{class:"grid"},[
      card(L("Склад")+(d.avatar.stash_size?" ("+d.avatar.stash.length+" / "+d.avatar.stash_size+")":""),[invTable(d.avatar.stash)]),
      card(L("С собой")+(d.avatar.carry_size?" ("+d.avatar.carry.length+" / "+d.avatar.carry_size+")":""),[invTable(d.avatar.carry)])]));
    var extra=[];
    var sc=function(t){ return el("div",{class:"scroll",style:"max-height:320px"},[t]); };
    if(d.friends.length) extra.push(card(L("Друзья (")+d.friends.length+")",[sc(table([L("Игрок"),L("Уровень"),L("Клан")],d.friends,function(f){
      return [el("span",{},[f.online? el("span",{class:"pill ok",title:L("в игре")},["●"]):null," "+f.name]), f.level==null?"":f.level, f.clan||"—"]; }))]));
    if(d.deaths.length) extra.push(card(L("Последние смерти"),[sc(table([L("Когда"),L("Что")],d.deaths,function(x){ return [x.ts,x.event]; }))]));
    if(d.rewards.length) extra.push(card(L("Награды за рейтинг"),[sc(table([L("Когда"),L("Награда")],d.rewards,function(x){ return [x.ts,x.reward]; }))]));
    if(extra.length) box.appendChild(el("div",{class:"grid"},extra));
  });
}

// ---------------------------------------------------------------- изучение
// Схема «метро»: первая ветка продолжает строку, остальные дети — новые строки.
function techScheme(nodes,done,cur,hl,onPick){
  var byId={},kids={},roots=[]; nodes.forEach(function(n){ byId[n.id]=n; });
  nodes.forEach(function(n){ if(n.parent&&byId[n.parent]) (kids[n.parent]=kids[n.parent]||[]).push(n.id); else roots.push(n.id); });
  var pos={},rowStart=[],nRows=0,maxX=0;
  function place(id,x,row){ pos[id]={x:x,row:row}; if(x>maxX) maxX=x;
    (kids[id]||[]).forEach(function(c,i){ if(i===0) place(c,x+1,row); else { var r=nRows++; rowStart[r]=c; place(c,x+1,r); } }); }
  roots.forEach(function(id){ var r=nRows++; rowStart[r]=id; place(id,0,r); });
  var LBL=150,P=21,C=15,W=LBL+(maxX+1)*P+8,H=nRows*P+6;
  function cx(id){ return LBL+pos[id].x*P; } function cy(id){ return 3+pos[id].row*P; }
  var svg=svgEl("svg",{width:W,height:H,viewBox:"0 0 "+W+" "+H,style:"display:block;font-family:inherit"}), prev=null;
  rowStart.forEach(function(id,r){ var f=byId[id].family||"";
    svg.appendChild(svgEl("text",{x:LBL-8,y:3+r*P+C-3,"text-anchor":"end","font-size":"11",fill:f===prev?"var(--line)":"var(--mut)"},[f===prev?"↳":f])); prev=f; });
  nodes.forEach(function(n){ if(!n.parent||!pos[n.parent]) return;
    var px=cx(n.parent)+C/2,py=cy(n.parent)+C/2,x=cx(n.id),y=cy(n.id)+C/2;
    var d=pos[n.parent].row===pos[n.id].row? "M"+(px+C/2)+" "+py+" H"+x : "M"+px+" "+(py+C/2)+" V"+y+" H"+x;
    var h=hl&&hl[n.id];
    svg.appendChild(svgEl("path",{d:d,fill:"none",stroke:h?"var(--warn)":(done[n.parent]?"var(--mut)":"var(--line)"),"stroke-width":h?"2.2":"1.2"})); });
  var cnt={done:0,avail:0,lock:0};
  nodes.forEach(function(n){ var st,fill,stroke;
    if(done[n.id]){ st=L("изучено"); fill=stroke="var(--ok)"; cnt.done++; }
    else if(cur===n.id){ st=L("изучается"); fill=stroke="var(--warn)"; }
    else if(!n.parent||!byId[n.parent]||done[n.parent]){ st=L("доступно"); fill="transparent"; stroke="var(--acc)"; cnt.avail++; }
    else { st=L("закрыто"); fill="transparent"; stroke="var(--line)"; cnt.lock++; }
    var h=hl&&hl[n.id];
    var tip=n.label+(n.cost_h!=null?L("\nВремя: ")+n.cost_h+L(" ч"):"")+"\n"+st+((n.unlocks&&n.unlocks.length)?L("\nОткрывает: ")+n.unlocks.join(", "):"");
    var g=svgEl("g",{style:"cursor:pointer"},[svgEl("title",{},[tip]),svgEl("rect",{x:cx(n.id),y:cy(n.id),width:C,height:C,rx:3,fill:fill,
      stroke:h?"var(--warn)":stroke,"stroke-width":h?2.4:1.4})]);
    g.addEventListener("click",function(){ onPick(n); }); svg.appendChild(g); });
  return {svg:svg,cnt:cnt};
}
function tabTech(m){
  var box=el("div"); m.appendChild(box);
  Promise.all([api("/api/tech-tree"),api("/api/me")]).then(function(r){
    var tree=r[0], me=r[1], by={};
    var nodes=tree.nodes.filter(function(n){ return !n.clan; });
    nodes.forEach(function(n){ by[n.id]=n; });
    var done={}; (me.research.tech_list||[]).forEach(function(t){ done[t]=true; });
    var holder=el("div",{style:"overflow:auto;max-height:640px;border:1px solid var(--line);border-radius:8px;padding:6px"});
    var info=el("div",{style:"margin-top:10px"},[el("div",{class:"muted small"},[L("Нажмите на технологию — покажу путь до неё и сколько осталось.")])]);
    var legend=el("div",{class:"row small muted",style:"margin-bottom:6px"});
    function draw(hl){ holder.innerHTML=""; var s=techScheme(nodes,done,me.research.current,hl,pick); holder.appendChild(s.svg);
      legend.textContent=L("Изучено ")+s.cnt.done+" / "+nodes.length+L(" · доступно сейчас ")+s.cnt.avail+L(" · закрыто ")+s.cnt.lock; }
    function pick(n){
      var chain=[],c=n,g=0; while(c&&g++<200){ chain.unshift(c); c=c.parent? by[c.parent]:null; }
      var hl={}; chain.forEach(function(x){ hl[x.id]=true; }); draw(hl);
      var miss=chain.filter(function(x){ return !done[x.id]; }), h=0; miss.forEach(function(x){ h+=x.cost_h||0; });
      info.innerHTML="";
      info.appendChild(card(n.label,[
        el("div",{},[miss.length? L("Осталось: ")+miss.length+L(" шаг(ов) · ~")+(Math.round(h*10)/10)+L(" ч") : el("span",{class:"pill ok"},[L("изучено")])]),
        el("div",{class:"chips",style:"margin-top:6px"},chain.map(function(x){ return el("span",{class:"chip",style:done[x.id]?"color:var(--ok);border-color:var(--ok)":""},[(done[x.id]?"✓ ":"")+x.label]); })),
        (n.unlocks&&n.unlocks.length)? el("div",{class:"small",style:"margin-top:8px"},[L("Открывает: ")+n.unlocks.join(", ")]) : null]));
    }
    draw(null);
    var cur=me.research.current? card(L("Сейчас"),[kv([[L("Изучается"),me.research.current_name],[L("Осталось"),fmtMin(me.research.remaining_min)]])]) : null;
    box.appendChild(el("div",{},[cur, card(L("Схема изучения"),[legend,holder,info])]));
  }).catch(function(e){ box.appendChild(errBox(e)); });
}

// ---------------------------------------------------------------- крафт
function tabCraft(m){
  var plan=el("div");
  var inp=el("input",{list:"cr-dl",placeholder:L("Что скрафтить?"),style:"min-width:240px"}), dl=el("datalist",{id:"cr-dl"});
  var qty=el("input",{type:"number",min:"1",value:"1",style:"width:80px"}), byName={};
  api("/api/craft-catalog").then(function(d){ (d.items||[]).forEach(function(it){ byName[it.name.toLowerCase()]=it.id; dl.appendChild(el("option",{value:it.name})); }); }).catch(function(){});
  function doPlan(item){
    var id=byName[(item||"").toLowerCase()]||item; if(!id) return;
    load(plan,"/api/craft-plan?item="+encodeURIComponent(id)+"&qty="+(parseInt(qty.value)||1),function(d){
      if(!d.ok){ plan.appendChild(errBox(d)); return; }
      var out=[el("div",{class:"muted small"},[L("Время крафта: ")+Math.round(d.time_s)+L(" с")+(d.benches.length?L(" · нужно: ")+d.benches.join(", "):"")])];
      out.push(el("h3",{style:"margin-top:10px"},[L("Сырьё")]));
      out.push(table([L("Ресурс"),L("Нужно")],d.raw,function(r){ return [itemBtn(r.id,r.name,null,goBook,L("Открыть в справочнике")),r.count]; }));
      if(d.intermediate.length){ out.push(el("h3",{style:"margin-top:10px"},[L("Промежуточное")]));
        out.push(table([L("Предмет"),L("Нужно"),L("Крафтов")],d.intermediate,function(r){ return [itemBtn(r.id,r.name,null,goBook,L("Открыть в справочнике")),r.need,r.crafts||""]; })); }
      if(d.techs.length){ out.push(el("h3",{style:"margin-top:10px"},[L("Технологии")]));
        out.push(table([L("Технология"),L("У меня")],d.techs,function(x){ return [x.label, x.known? el("span",{class:"pill ok"},[L("изучено")])
          : el("span",{class:"pill warn"},[L("нет · ещё ")+x.missing_chain+L(" шаг(ов), ~")+x.missing_h+L(" ч")])]; })); }
      out.unshift(el("div",{style:"margin-bottom:8px"},[itemBtn(d.item,d.name,d.qty,goBook,L("Открыть в справочнике"))]));
      plan.appendChild(card("",out));
    });
  }
  m.appendChild(card(L("Раскладка до сырья"),[el("div",{class:"row"},[inp,dl,qty,el("button",{class:"pri",onclick:function(){ doPlan(inp.value); }},[L("Посчитать")])]),
    el("div",{style:"margin-top:10px"},[plan])]));
  inp.addEventListener("keydown",function(e){ if(e.key==="Enter") doPlan(inp.value); });
  var go=""; try{ go=localStorage.getItem("swp_craft_go")||""; localStorage.removeItem("swp_craft_go"); }catch(e){}
  if(go) doPlan(go);
}

// ---------------------------------------------------------------- рынок
function fmtItems(a){ return (a||[]).map(function(x){ return x.name+" ×"+x.count; }).join(", ")||"—"; }
function itemsEl(a){ if(!a||!a.length) return "—";
  return el("span",{style:"display:inline-flex;flex-wrap:wrap;gap:4px 10px"},a.map(function(x){ return withIco(x.name,x.name+" ×"+x.count,18); })); }
function median(a){ a=a.slice().sort(function(x,y){ return x-y; }); var n=a.length; return n? (n%2? a[(n-1)/2] : (a[n/2-1]+a[n/2])/2) : null; }
function num(v){ return v==null? "" : (v>=100? Math.round(v).toLocaleString(LOC) : String(Math.round(v*100)/100)); }
// курс простого лота: «N валюты за 1 шт», а если товар дешёвый — «1 валюты = N шт»
function rate(o){ if(o.unit==null||!o.unit||o.give.length!==1||o.want.length!==1) return "";
  return o.unit>=1? num(o.unit)+" "+o.want[0].name+L(" за 1") : "1 "+o.want[0].name+" = "+num(1/o.unit)+L(" шт"); }
function tabMarket(m){
  var q=el("input",{placeholder:L("Предмет, например: Железный слиток"),style:"min-width:260px;flex:1"});
  var mode=el("select",{},[el("option",{value:"give"},[L("продают")]),el("option",{value:"want"},[L("просят взамен")]),el("option",{value:"any"},[L("везде")])]);
  var info=el("div",{class:"muted small",style:"margin-top:6px"}), sum=el("div"), list=el("div"), data=null;
  try{ q.value=localStorage.getItem("swp_mq")||""; }catch(e){}
  function has(arr,t){ return (arr||[]).some(function(x){ return x.name.toLowerCase().indexOf(t)>=0; }); }
  function draw(){
    sum.innerHTML=""; list.innerHTML=""; if(!data) return;
    try{ localStorage.setItem("swp_mq",q.value); }catch(e){}
    var t=q.value.trim().toLowerCase(), md=mode.value;
    var rows=data.offers.filter(function(o){ if(!t) return true;
      return md==="give"? has(o.give,t) : md==="want"? has(o.want,t) : (has(o.give,t)||has(o.want,t)); });
    if(t){  // сводка цен по искомому предмету: простые лоты «1 товар за 1 вид оплаты»
      var g={}; rows.forEach(function(o){ if(o.unit==null||!o.unit||o.give.length!==1||o.want.length!==1) return;
        var gv=o.give[0], wn=o.want[0], r;
        if(gv.name.toLowerCase().indexOf(t)>=0) r={item:gv.name, cur:wn.name, p:wn.count/gv.count, side:L("продают")};
        else if(wn.name.toLowerCase().indexOf(t)>=0) r={item:wn.name, cur:gv.name, p:gv.count/wn.count, side:L("покупают")};
        else return;
        var k=r.side+"|"+r.item+"|"+r.cur; (g[k]=g[k]||{item:r.item,cur:r.cur,side:r.side,u:[]}).u.push(r.p); });
      var ps=Object.keys(g).map(function(k){ return g[k]; }).sort(function(a,b){ return b.u.length-a.u.length; });
      if(ps.length) sum.appendChild(card(L("Цена за 1 шт"),[table([L("Товар"),L("Сделка"),L("Платят"),L("Мин"),L("Медиана"),L("Макс"),L("Лотов")],ps,function(x){
        return [withIco(x.item,x.item,18),x.side,withIco(x.cur,x.cur,18),num(Math.min.apply(null,x.u)),num(median(x.u)),num(Math.max.apply(null,x.u)),x.u.length]; })]));
    }
    rows.sort(function(a,b){ return (a.unit==null)-(b.unit==null) || (a.unit||0)-(b.unit||0); });
    var shown=rows.slice(0,300);
    list.appendChild(card(L("Предложения (")+rows.length+(rows.length>shown.length?L(", показаны первые ")+shown.length:"")+")",[
      rows.length? el("div",{class:"scroll",style:"max-height:600px"},[table([L("Отдаёт"),L("Просит"),L("Курс"),L("Продавец"),L("Где")],shown,function(o){
        return [itemsEl(o.give),itemsEl(o.want),rate(o),
          el("span",{},[o.owner+(o.clan?" ["+o.clan+"]":""), o.mine? el("span",{class:"pill ok",style:"margin-left:6px"},[L("моё")]):null]), o.where]; })])
      : el("div",{class:"muted"},[t? L("ничего не нашлось") : L("предложений нет")])]));
  }
  function fetchM(){
    api("/api/market").then(function(d){
      if(S.tab!=="market") return;
      if(d.pending){ info.textContent=L("Собираю предложения со всех карт — это до пары минут, страница обновится сама…"); setTimeout(fetchM,4000); return; }
      data=d; info.textContent=L("Обновлено ")+(d.age_s<90? L("только что") : Math.round(d.age_s/60)+L(" мин назад"))+L(" · всего предложений: ")+d.offers.length
        +(d.refreshing? L(" · обновляю в фоне…") : ""); draw();
    }).catch(function(e){ info.textContent=""; list.innerHTML=""; list.appendChild(errBox(e)); });
  }
  var tmr; q.addEventListener("input",function(){ clearTimeout(tmr); tmr=setTimeout(draw,200); }); mode.addEventListener("change",draw);
  m.appendChild(card(L("Рынок"),[el("div",{class:"row"},[q,mode]),info,
    el("div",{class:"muted small"},[L("Терминалы игроков и магазины на картах. Курс и цены — только для простых лотов «один товар за одну валюту»; сводка цен появляется при поиске.")])]));
  var own=el("div"); m.insertBefore(own, m.firstChild);
  api("/api/my-trade").then(function(d){ if(d.pending||(!d.terminals.length&&!d.shops.length&&!d.offers.length)) return;
    var k=[];
    d.terminals.forEach(function(t){ k.push(el("div",{style:"margin-bottom:8px"},[el("b",{},[L("Терминал")]),
      el("span",{class:"muted"},[L("  · лотов ")+t.lots+L(" · продаж ")+t.sales+(t.idle_h!=null?L(" · не заходил ")+t.idle_h+L(" ч"):"")]),
      el("div",{class:"small"},[L("На складе терминала: "),itemsEl(t.storage)])])); });
    d.shops.forEach(function(sh){ k.push(el("div",{style:"margin-bottom:8px"},[el("b",{},[L("Магазин · ")+sh.where]),
      el("span",{class:"muted"},[L("  · слотов ")+sh.slots+L(" · продаж ")+sh.sales]),
      el("div",{class:"small"},[L("Выручка: "),itemsEl(sh.storage)])])); });
    if(d.offers.length) k.push(el("div",{class:"scroll",style:"max-height:260px"},[table([L("Отдаю"),L("Прошу"),L("Курс"),L("Где")],d.offers,function(o){ return [itemsEl(o.give),itemsEl(o.want),rate(o),o.where]; })]));
    own.appendChild(card(L("Моя торговля"),k));
  }).catch(function(){});
  m.appendChild(sum); m.appendChild(list); fetchM();
}

// ---------------------------------------------------------------- карта своих участков
function tabMap(m){
  var box=el("div"); m.appendChild(box);
  load(box,"/api/my-maps",function(d){
    if(!d.maps.length){ box.appendChild(card(L("Карта"),[el("div",{class:"muted"},[L("У вас пока нет участков.")])])); return; }
    var sel=el("select",{}), zoom=6, rot=315, cur=null, sc=1;
    try{ var r0=parseInt(localStorage.getItem("swp_rot")); if(!isNaN(r0)) rot=r0; }catch(e){}
    d.maps.forEach(function(x,i){ sel.appendChild(el("option",{value:i},[x.name+L(" · участков: ")+x.territories.length+(x.here?L(" · вы здесь"):"")])); });
    // как в админке: картинка поворачивается целиком (вместе с метками) внутри
    // «сцены» размером с диагональ, чтобы повёрнутые углы не обрезались
    var img=el("img",{alt:"",style:"display:block;image-rendering:pixelated;max-width:none;width:100%;height:100%"});
    var layer=el("div",{style:"position:absolute;left:50%;top:50%;transform-origin:50% 50%"},[img]);
    var stage=el("div",{style:"position:relative"},[layer]);
    var view=el("div",{style:"overflow:auto;height:70vh;border:1px solid var(--line);border-radius:8px;background:rgb(22,25,31)"},[stage]);
    var tlist=el("div"), zl=el("span",{class:"muted small"}), rl=el("span",{class:"muted small"}), st=el("span",{class:"muted small"});
    function dims(){ return {W:img.naturalWidth*zoom, H:img.naturalHeight*zoom}; }
    function place(){
      if(!img.naturalWidth) return;
      sc=img.naturalWidth/cur.w; var z=dims(), diag=Math.ceil(Math.sqrt(z.W*z.W+z.H*z.H));
      stage.style.width=diag+"px"; stage.style.height=diag+"px";
      layer.style.width=z.W+"px"; layer.style.height=z.H+"px";
      layer.style.transform="translate(-50%,-50%) rotate("+rot+"deg)";
      zl.textContent="×"+zoom; rl.textContent=(((rot%360)+360)%360)+"°";
      [].slice.call(layer.querySelectorAll(".mk")).forEach(function(e){ e.remove(); });
      var k=sc*zoom;
      cur.territories.forEach(function(t){ layer.appendChild(el("div",{class:"mk",title:L("мой участок ")+t.x+", "+t.y,
        style:"position:absolute;left:"+(t.x*8*k)+"px;top:"+((cur.h-t.y*8-8)*k)+"px;width:"+(8*k)+"px;height:"+(8*k)+"px;"
          +"background:rgba(80,255,120,.25);outline:2px solid #3fff7a;box-sizing:border-box"})); });
      if(d.me.map===cur.map && d.me.x!=null) layer.appendChild(el("div",{class:"mk",title:L("вы здесь"),
        style:"position:absolute;left:"+(d.me.x*k-6)+"px;top:"+((cur.h-d.me.y)*k-6)+"px;width:12px;height:12px;border-radius:50%;background:#ff3b30;border:2px solid #fff;box-shadow:0 0 4px #000"}));
    }
    // клетка игры -> точка в «сцене» с учётом поворота (ось Y картинки — вверх, как в игре)
    function toStage(x,y){ var k=sc*zoom, z=dims(), px=x*k-z.W/2, py=(cur.h-y)*k-z.H/2, a=rot*Math.PI/180,
        c=Math.cos(a), s=Math.sin(a), diag=stage.offsetWidth;
      return {x:diag/2+px*c-py*s, y:diag/2+px*s+py*c}; }
    function fromStage(sx,sy){ var k=sc*zoom, z=dims(), diag=stage.offsetWidth, a=-rot*Math.PI/180, c=Math.cos(a), s=Math.sin(a),
        dx=sx-diag/2, dy=sy-diag/2, px=dx*c-dy*s, py=dx*s+dy*c;
      return {x:(px+z.W/2)/k, y:cur.h-(py+z.H/2)/k}; }
    function focus(x,y){ var p=toStage(x,y); view.scrollLeft=p.x-view.clientWidth/2; view.scrollTop=p.y-view.clientHeight/2; }
    function center(){ return fromStage(view.scrollLeft+view.clientWidth/2, view.scrollTop+view.clientHeight/2); }
    function focusMine(){ if(d.me.map===cur.map && d.me.x!=null) return focus(d.me.x,d.me.y);
      var t=cur.territories; if(t.length){ var cx=0,cy=0; t.forEach(function(p){ cx+=p.x*8+4; cy+=p.y*8+4; }); focus(cx/t.length,cy/t.length); } }
    function keep(fn){ var c=center(); fn(); place(); focus(c.x,c.y); }
    function show(){
      cur=d.maps[+sel.value]; st.textContent=L("загрузка карты…"); img.removeAttribute("src");
      img.onload=function(){ st.textContent=""; place(); focusMine(); };
      img.onerror=function(){ st.textContent=L("карта не загрузилась"); };
      img.src="/api/my-map-image?map="+cur.map;
      tlist.innerHTML="";
      if(cur.territories.length) tlist.appendChild(card(L("Участки на этой карте (")+cur.territories.length+")",[el("div",{class:"scroll",style:"max-height:260px"},[
        table(["#",L("Участок (X, Y)"),L("Клетки"),""],cur.territories,function(t){ return [cur.territories.indexOf(t)+1, t.x+", "+t.y,
          (t.x*8)+"–"+(t.x*8+7)+", "+(t.y*8)+"–"+(t.y*8+7), el("button",{onclick:function(){ focus(t.x*8+4,t.y*8+4); view.scrollIntoView({block:"nearest"}); }},[L("показать")])]; })])]));
    }
    function setRot(v){ keep(function(){ rot=v; try{ localStorage.setItem("swp_rot",String(rot)); }catch(e){} }); }
    // протяжка как в админке: тащим зажатой ЛКМ (или пальцем), колесо — масштаб под курсором
    img.draggable=false; view.style.cursor="grab"; view.style.touchAction="none"; view.style.userSelect="none";
    var drag=null;
    view.addEventListener("pointerdown",function(e){ if(e.button!==0) return;
      drag={x:e.clientX,y:e.clientY,l:view.scrollLeft,t:view.scrollTop}; view.setPointerCapture(e.pointerId);
      view.style.cursor="grabbing"; e.preventDefault(); });
    view.addEventListener("pointermove",function(e){ if(!drag) return;
      view.scrollLeft=drag.l-(e.clientX-drag.x); view.scrollTop=drag.t-(e.clientY-drag.y); });
    function endDrag(){ drag=null; view.style.cursor="grab"; }
    view.addEventListener("pointerup",endDrag); view.addEventListener("pointercancel",endDrag);
    view.addEventListener("wheel",function(e){
      if(!img.naturalWidth) return; e.preventDefault();
      var nz=Math.max(1,Math.min(16,zoom+(e.deltaY<0?1:-1))); if(nz===zoom) return;
      var r=view.getBoundingClientRect(), ox=e.clientX-r.left, oy=e.clientY-r.top;
      var c=fromStage(view.scrollLeft+ox, view.scrollTop+oy);   // клетка под курсором остаётся под курсором
      zoom=nz; place(); var p=toStage(c.x,c.y); view.scrollLeft=p.x-ox; view.scrollTop=p.y-oy;
    },{passive:false});
    sel.addEventListener("change",show);
    box.appendChild(card(L("Мои участки на карте"),[el("div",{class:"row",style:"margin-bottom:8px"},[sel,
      el("button",{title:L("повернуть против часовой на 45°"),onclick:function(){ setRot(rot-45); }},["↺"]), rl,
      el("button",{title:L("повернуть по часовой на 45°"),onclick:function(){ setRot(rot+45); }},["↻"]),
      el("button",{onclick:function(){ keep(function(){ zoom=Math.max(1,zoom-1); }); }},["−"]), zl,
      el("button",{onclick:function(){ keep(function(){ zoom=Math.min(16,zoom+1); }); }},["+"]),
      el("button",{onclick:focusMine},[L("ко мне")]), st]),
      el("div",{class:"muted small",style:"margin-bottom:6px"},[L("Видно ")+d.fog_radius+L(" клеток вокруг вас и вокруг ваших участков — остальное скрыто туманом. Зелёные квадраты — ваши участки, красная точка — вы. Карту можно тащить мышью, колесо — масштаб.")]), view]));
    box.appendChild(tlist); show();
  });
}

// ---------------------------------------------------------------- история
function tabHist(m){
  var box=el("div"); m.appendChild(box);
  load(box,"/api/history",function(d){
    var s=function(k){ return d.points.map(function(p){ return {t:p.t,v:p[k]}; }).filter(function(p){ return p.v!=null; }); };
    box.appendChild(card(L("Моя история"),[el("div",{class:"muted small"},[d.since? L("Панель записывает снимок раз в час с ")+new Date(d.since*1000).toLocaleDateString(LOC)+L(" — графики будут расти со временем.")
      : L("Снимки ещё не делались — первый появится в течение часа.")])]));
    box.appendChild(el("div",{class:"grid"},[
      card("",[chart(d.techs,{title:L("Изучено технологий"),y:L("техов"),x:L("дата")})]),
      card("",[chart(s("level"),{title:L("Уровень"),y:L("уровень"),x:L("дата")})]),
      card("",[chart(s("rating"),{title:L("Рейтинг"),y:L("рейтинг"),x:L("дата")})]),
      card("",[chart(s("play_h"),{title:L("Наиграно"),y:L("часов"),x:L("дата")})]),
      card("",[chart(s("research_h"),{title:L("Вложено в исследования"),y:L("часов"),x:L("дата")})])]));
  });
}

// ---------------------------------------------------------------- справочник
function tabBook(m){
  var inp=el("input",{list:"bk-dl",placeholder:L("Предмет"),style:"min-width:260px;flex:1"}), dl=el("datalist",{id:"bk-dl"}), out=el("div"), byName={};
  function open(id){ try{ localStorage.setItem("swp_book",id); }catch(e){}
    load(out,"/api/handbook?item="+encodeURIComponent(id),function(d){
      if(!d.ok){ out.appendChild(errBox(d)); return; }
      inp.value=d.name;
      function lnk(x,n){ return itemBtn(x.id,x.name,n,open); }
      var k=[];
      if(d.flags.length) k.push(el("div",{class:"chips",style:"margin-bottom:8px"},d.flags.map(function(f){ return el("span",{class:"chip"},[f]); })));
      if(d.stats.length) k.push(kv(d.stats));
      if(d.recipe){ var r=d.recipe;
        k.push(el("h3",{style:"margin-top:12px"},[L("Крафт")+(r.out>1?L(" (выходит ")+r.out+L(" шт)"):"")]));
        k.push(kv([[L("Нужно"),el("div",{class:"ibtns"},r.res.map(function(x){ return lnk(x,x.n); }))],[L("Где"),r.workbench||L("в руках")],[L("Время"),r.time+L(" с")],
          [L("Технология"),r.tech? el("span",{},[r.tech+" ", el("span",{class:"pill "+(r.tech_known?"ok":"warn")},[r.tech_known?L("изучена"):L("не изучена")])]) : L("не нужна")]])); }
      if(d.machine.length){ k.push(el("h3",{style:"margin-top:12px"},[L("Станок")]));
        k.push(table([L("Станок"),L("Из чего"),L("Энергия")],d.machine,function(x){ return [lnk({id:x.machine_id,name:x.machine}),lnk({id:x.from_id,name:x.from}),x.energy==null?"":x.energy]; })); }
      if(!d.recipe&&!d.machine.length) k.push(el("div",{class:"muted",style:"margin-top:10px"},[L("Не крафтится — добывается или находится в мире.")]));
      if(d.used_in.length){ k.push(el("h3",{style:"margin-top:12px"},[L("Используется в (")+d.used_in.length+")"]));
        k.push(el("div",{class:"ibtns scroll",style:"max-height:220px"},d.used_in.map(function(x){ return lnk(x); }))); }
      if(d.on_market) k.push(el("div",{class:"small",style:"margin-top:12px"},[L("На рынке: продают — ")+d.on_market.sell+L(" лот(ов), просят взамен — ")+d.on_market.buy]));
      // шапка карточки: крупная иконка, описание из игры и кнопки переходов
      var acts=el("div",{class:"row"},[
        (d.recipe||d.machine.length)? el("button",{onclick:function(){ goCraft(d.id); }},[L("Раскладка до сырья")]) : null,
        el("button",{onclick:function(){ try{ localStorage.setItem("swp_mq",d.name); }catch(_){} S.tab="market"; render(); window.scrollTo(0,0); }},[L("Искать на рынке")])]);
      k.unshift(el("div",{class:"row",style:"margin-bottom:10px;align-items:flex-start;gap:14px"},[ico(d.id,64),
        el("div",{style:"flex:1;min-width:200px"},[d.info? el("div",{style:"margin-bottom:8px"},[d.info]) : null, acts])]));
      out.appendChild(card(d.name,k));
    }); }
  api("/api/handbook").then(function(d){ (d.items||[]).forEach(function(it){ byName[it.name.toLowerCase()]=it.id; dl.appendChild(el("option",{value:it.name})); }); }).catch(function(){});
  inp.addEventListener("change",function(){ var id=byName[inp.value.trim().toLowerCase()]; if(id) open(id); });
  m.appendChild(card(L("Справочник предметов"),[el("div",{class:"row"},[inp,dl]),el("div",{class:"muted small",style:"margin-top:6px"},[L("Свойства, как получить, какая технология нужна и куда предмет идёт дальше.")])]));
  m.appendChild(out);
  var last=""; try{ last=localStorage.getItem("swp_book")||""; }catch(e){}
  if(last) open(last);
}

// ---------------------------------------------------------------- чат и события
var CH=[["global",L("Общий")],["global2","Global (EN)"],["ru",L("Русский")],["clan",L("Клан")],["private",L("Личные")],["events",L("События")]];
var EVD={kill:L("убит"),reset_position:L("сброс позиции"),satiety:L("голод"),oxygen:L("задохнулся"),gas:L("газ"),moss:L("мох")};
var EVK={register:L("новый игрок"),death:L("смерть"),clan_created:L("создан клан"),clan_disbanded:L("распущен клан"),clan_renamed:L("клан переименован")};
function tabChat(m){
  var ch="global"; try{ ch=localStorage.getItem("swp_ch")||"global"; }catch(e){}
  var bar=el("div",{class:"row"}), q=el("input",{placeholder:L("Поиск по нику или тексту"),style:"width:100%"}), out=el("div"), tmr=null;
  function draw(){
    bar.innerHTML="";
    CH.forEach(function(c){ bar.appendChild(el("button",{class:ch===c[0]?"pri":"",onclick:function(){ ch=c[0]; try{ localStorage.setItem("swp_ch",ch); }catch(e){} draw(); }},[c[1]])); });
    q.style.display=ch==="events"?"none":"";
    fetchC();
  }
  function fetchC(){
    clearTimeout(tmr);
    var p= ch==="events"? "/api/events" : "/api/chat?ch="+ch+"&q="+encodeURIComponent(q.value.trim());
    api(p).then(function(d){
      if(S.tab!=="chat") return;
      out.innerHTML="";
      if(ch==="events"){
        out.appendChild(card(L("События сервера"),[d.events.length? el("div",{class:"scroll",style:"max-height:65vh"},[table([L("Когда"),L("Что"),L("Кто"),""],d.events,function(e){
          return [e.ts, EVK[e.kind]||e.kind, e.who, e.kind==="death"? (EVD[e.detail]||e.detail) : e.kind==="clan_renamed"&&e.detail? L("было: ")+e.detail : e.detail]; })]) : el("div",{class:"muted"},[L("пока пусто")])]));
      } else {
        var rows=d.messages.map(function(r){ return el("div",{style:"padding:3px 0;border-bottom:1px solid var(--line)"},[
          el("span",{class:"muted small"},[r.ts+"  "]),
          el("b",{style:r.out?"color:var(--acc)":""},[r.nick]), r.to? el("span",{class:"muted"},[" → "+r.to]) : null, ": "+r.text]); });
        out.appendChild(card((CH.filter(function(c){ return c[0]===ch; })[0]||[,""])[1]+L(" · сообщений: ")+d.total+(d.total>300?L(" (последние 300)"):""),[
          ch==="clan"? el("div",{class:"muted small",style:"margin-bottom:6px"},[L("Сообщения нынешних участников вашего клана.")]) : null,
          ch==="private"? el("div",{class:"muted small",style:"margin-bottom:6px"},[L("Только ваши личные сообщения — от вас и вам.")]) : null,
          rows.length? el("div",{class:"scroll",style:"max-height:65vh"},rows) : el("div",{class:"muted"},[L("сообщений нет")])]));
      }
      tmr=setTimeout(fetchC,20000);
    }).catch(function(e){ out.innerHTML=""; out.appendChild(errBox(e)); });
  }
  var qt; q.addEventListener("input",function(){ clearTimeout(qt); qt=setTimeout(fetchC,300); });
  m.appendChild(card(L("Чат"),[bar,el("div",{style:"margin-top:8px"},[q]),el("div",{class:"muted small",style:"margin-top:6px"},[L("Только чтение, обновляется раз в 20 секунд.")])]));
  m.appendChild(out); draw();
}

// ---------------------------------------------------------------- сундуки
function tabChests(m){
  var box=el("div"); m.appendChild(box);
  load(box,"/api/my-chests",function(d){
    if(!d.maps.length){ box.appendChild(card(L("Мои сундуки"),[el("div",{class:"muted"},[L("У вас пока нет участков.")])])); return; }
    var q=el("input",{placeholder:L("Найти предмет в сундуках"),style:"width:100%"}), list=el("div");
    function draw(){
      list.innerHTML=""; var t=q.value.trim().toLowerCase(), tot={};
      d.maps.forEach(function(mp){
        var rows=mp.containers.filter(function(c){ return !t || c.items.some(function(x){ return x.name.toLowerCase().indexOf(t)>=0; }); });
        rows.forEach(function(c){ c.items.forEach(function(x){ var k=x.name; tot[k]=tot[k]||{id:x.id,name:x.name,n:0}; tot[k].n+=x.count; }); });
        var body= rows.length? el("div",{class:"scroll",style:"max-height:520px"},[table([L("Что"),L("Где (X, Y)"),L("Лежит")],rows,function(c){
            return [withIco(c.block,c.what,24), c.x+", "+c.y, el("div",{class:"ibtns"},c.items.map(function(x){ return itemBtn(x.id,x.name,x.count,goBook,L("Открыть в справочнике")); }))]; })])
          : el("div",{class:"muted"},[t? L("ничего не нашлось") : L("пусто")]);
        list.appendChild(card(mp.name+" · "+L("участков: ")+mp.territories+" · "+L("хранилищ: ")+mp.containers.length,[body]));
      });
      var all=Object.keys(tot).map(function(k){ return tot[k]; }).sort(function(a,b){ return b.n-a.n; });
      if(all.length) list.insertBefore(card(L("Всего на моих участках"),[el("div",{class:"ibtns"},all.map(function(x){ return itemBtn(x.id,x.name,x.n,goBook,L("Открыть в справочнике")); }))]), list.firstChild);
    }
    var tm; q.addEventListener("input",function(){ clearTimeout(tm); tm=setTimeout(draw,200); });
    box.appendChild(card(L("Мои сундуки"),[q, el("div",{class:"muted small",style:"margin-top:6px"},[L("Сундуки и прочие хранилища на ваших участках, а также брошенное на землю. Данные обновляются раз в 5 минут.")])]));
    box.appendChild(list); draw();
  });
}

// ---------------------------------------------------------------- клан
var CE={joined:L("вступил"),left:L("ушёл"),role:L("роль"),tech:L("клан-технология"),renamed:L("переименован"),slots:L("слоты"),created:L("создан"),disbanded:L("распущен")};
// Линейный график с осями: Y — 3 деления (мин/середина/макс), X — время.
function chart(pts,o){
  o=o||{}; var wide=o.wide && window.innerWidth>700;   // на телефоне широкий график стал бы мелким
  var W=wide?1100:520,H=wide?230:220,L=44,R=10,T=10,B=34;
  var box=el("div",{style:"flex:1;min-width:260px"},[el("div",{class:"small muted"},[o.title||""])]);
  if(pts.length<2){ box.appendChild(el("div",{class:"muted small"},[pts.length? L("сейчас: ")+pts[0].v+L(" · график появится, когда накопятся данные") : L("мало данных")])); return box; }
  var t0=pts[0].t,t1=pts[pts.length-1].t,lo=Infinity,hi=-Infinity; pts.forEach(function(p){ lo=Math.min(lo,p.v); hi=Math.max(hi,p.v); });
  if(o.zero) lo=Math.min(0,lo);
  if(hi===lo){ hi+=1; if(!o.zero) lo-=1; }
  function X(t){ return L+(W-L-R)*(t-t0)/((t1-t0)||1); } function Y(v){ return T+(H-T-B)*(1-(v-lo)/(hi-lo)); }
  var fmt=function(v){ return Math.abs(v)>=1000? Math.round(v).toLocaleString(LOC) : String(Math.round(v*10)/10); };
  var kids=[];
  [lo,(lo+hi)/2,hi].forEach(function(v){ kids.push(svgEl("line",{x1:L,x2:W-R,y1:Y(v),y2:Y(v),stroke:"var(--line)","stroke-dasharray":"3 3"}));
    kids.push(svgEl("text",{x:L-6,y:Y(v)+4,"text-anchor":"end","font-size":"11",fill:"var(--mut)"},[fmt(v)])); });
  var span=t1-t0, n=4, prevLab=null;
  for(var i=0;i<=n;i++){ var t=t0+span*i/n, d=new Date(t*1000);
    var lab= span>2*86400? (d.getDate()+"."+String(d.getMonth()+1).padStart(2,"0")) : (String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0"));
    if(lab===prevLab) continue; prevLab=lab;
    kids.push(svgEl("line",{x1:X(t),x2:X(t),y1:H-B,y2:H-B+4,stroke:"var(--mut)"}));
    kids.push(svgEl("text",{x:X(t),y:H-B+16,"text-anchor":i===0?"start":i===n?"end":"middle","font-size":"11",fill:"var(--mut)"},[lab])); }
  kids.push(svgEl("line",{x1:L,x2:W-R,y1:H-B,y2:H-B,stroke:"var(--mut)"}));
  kids.push(svgEl("line",{x1:L,x2:L,y1:T,y2:H-B,stroke:"var(--mut)"}));
  var d=pts.map(function(p,i){ return (i?"L":"M")+X(p.t).toFixed(1)+" "+Y(p.v).toFixed(1); }).join(" ");
  kids.push(svgEl("path",{d:d+" L"+X(t1)+" "+(H-B)+" L"+X(t0)+" "+(H-B)+" Z",fill:"var(--acc)","fill-opacity":"0.12",stroke:"none"}));
  kids.push(svgEl("path",{d:d,fill:"none",stroke:"var(--acc)","stroke-width":"2"}));
  kids.push(svgEl("text",{x:(L+W-R)/2,y:H-2,"text-anchor":"middle","font-size":"11",fill:"var(--mut)"},[o.x||L("время")]));
  kids.push(svgEl("text",{x:12,y:(T+H-B)/2,"text-anchor":"middle","font-size":"11",fill:"var(--mut)",transform:"rotate(-90 12 "+((T+H-B)/2)+")"},[o.y||""]));
  box.appendChild(svgEl("svg",{viewBox:"0 0 "+W+" "+H,style:"width:100%;height:auto;display:block"},kids));
  return box;
}
function tabClan(m){
  var box=el("div"); m.appendChild(box);
  load(box,"/api/clan",function(d){
    if(d.none){ box.appendChild(card(L("Клан"),[el("div",{class:"muted"},[L("Вы не состоите в клане.")])])); return; }
    box.appendChild(card(d.name,[kv([[L("Участников"),d.size+(d.max?" / "+d.max:"")],[L("Сейчас в игре"),d.online],[L("Рейтинг"),d.rating],
      [L("Очки клана"),d.clan_point],[L("Клан-технологий"),d.tech.length]]),
      d.tech_named.length? el("div",{class:"chips",style:"margin-top:8px"},d.tech_named.map(function(x){ return el("span",{class:"chip"},[x.label]); })) : null]));
    box.appendChild(card(L("Состав"),[el("div",{class:"scroll"},[table([L("Игрок"),L("Роль"),L("Уровень"),L("Специализация"),L("Был")],d.members,function(x){
      return [el("span",{},[x.online? el("span",{class:"pill ok"},["●"]):null," "+x.name]), x.role_name, x.level==null?"":x.level,
        x.spec? "+"+x.spec.positive+" / −"+x.spec.negative : "",
        x.online? L("сейчас") : (x.last_seen_h==null? "" : x.last_seen_h<48? x.last_seen_h+L(" ч назад") : Math.round(x.last_seen_h/24)+L(" дн назад"))]; })])]));
    var h=d.history||{};
    if(h.series&&h.series.length>1){ var s=function(k){ return h.series.map(function(p){ return {t:p.t,v:p[k]}; }); };
      box.appendChild(card(L("Рост клана (30 дней)"),[el("div",{class:"row",style:"align-items:flex-start;gap:16px"},[chart(s("rating"),{title:L("Рейтинг"),y:L("рейтинг"),x:L("дата")}),chart(s("cp"),{title:L("Очки клана"),y:L("очки"),x:L("дата")}),chart(s("size"),{title:L("Состав"),y:L("участников"),x:L("дата"),zero:true})])])); }
    if(h.events&&h.events.length) box.appendChild(card(L("События"),[el("div",{class:"scroll small"},h.events.slice(0,100).map(function(e){
      return el("div",{},[el("span",{class:"muted"},[e.ts+"  "]), (CE[e.kind]||e.kind)+" ", e.name||"",
        e.kind==="role"? " "+e.was+" → "+e.role : e.kind==="tech"? " "+(e.label||e.tech) : ""]); }))]));
  });
}

// ---------------------------------------------------------------- сервер
function tabServer(m){
  var box=el("div"); m.appendChild(box);
  load(box,"/api/server",function(d){
    box.appendChild(card(L("Сервер"),[kv([[L("Статус"),el("span",{class:"pill "+(d.game_up?"ok":"err")},[d.game_up?L("работает"):L("не запущен")])],
      [L("Сейчас онлайн"),d.online.length],[L("Пик за сутки"),d.online_peak]]),
      el("div",{style:"margin-top:10px"},[chart(d.online_series,{title:L("Онлайн за 24 часа"),y:L("игроков"),x:L("время"),zero:true,wide:true})])]));
    function top(title,rows,col){ return card(title,[rows.length? table(["#",L("Игрок"),col],rows,function(r){ return [rows.indexOf(r)+1, r.name, r.v]; })
      : el("div",{class:"muted"},[L("пока нет данных")])]); }
    box.appendChild(el("div",{class:"grid"},[
      card(L("Сезонный рейтинг"),[d.rating.length? table(["#",L("Игрок"),L("Награда")],d.rating,function(r){ return [d.rating.indexOf(r)+1,r.name,r.reward? r.reward+L(" ускор."):""]; }) : el("div",{class:"muted"},[L("пока нет данных")])]),
      top(L("Торговцы"),d.traders,L("Продаж")),
      card(L("Кланы"),[d.clans.length? table([L("Клан"),L("Рейтинг"),L("За 7 дней")],d.clans,function(c){ return [c.name,c.rating,c.growth==null?"":(c.growth>0?"+":"")+c.growth]; })
        : el("div",{class:"muted"},[L("пока нет данных")])])]));
    box.appendChild(card(L("Сейчас на сервере (")+d.online.length+")",[d.online.length?
      el("div",{class:"scroll"},[table(["#",L("Игрок"),L("Уровень"),L("Клан")],d.online,function(p){ return [d.online.indexOf(p)+1,p.name,p.level==null?"":p.level,p.clan||"—"]; })])
      : el("div",{class:"muted"},[L("никого")])]));
  });
}

api("/api/session").then(function(d){ S.nick=d.authed? d.nick:""; S.staff=d.staff||null; if(d.title) document.title=d.title; render(); })
  .catch(function(){ render(); });
</script>
</body>
</html>
"""
