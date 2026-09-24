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
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer

import players
from webui import Throttle, _Handler, _ip_allowed, _parse_nets

SESSION_TTL = 12 * 3600
REMEMBER_TTL = 30 * 86400    # «Запомнить меня»: токен на 30 дней, на диске — только его хэш
REMEMBER_RECHECK = 60        # как часто сверять, не сменил ли игрок пароль в игре
LEADERS_CACHE_SEC = 300
MAP_IMG_CACHE_SEC = 600    # сырые пиксели карты (без клаймов) — одни на всех, туман накладывается на каждый запрос
FOG_RADIUS = 20            # клеток: видно вокруг персонажа и вокруг своих участков
FOG_RGB = (22, 25, 31)
MARKET_CACHE_SEC = 300      # полный проход по картам — десятки секунд, считаем в фоне


class PlayerWeb:
    def __init__(self, cfg, state, ct_events=None, ct_points=None, tt_log=None):
        self.cfg = cfg
        self.state = state
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
        logging.info("playerweb: панель игроков на http://%s:%d/", host, port)

    def stop(self):
        try:
            if self._srv:
                self._srv.shutdown()
                self._srv.server_close()
        except Exception:  # noqa: BLE001
            logging.exception("playerweb: ошибка остановки")

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
                return self._json(h, {"authed": bool(s), "nick": s["nick"] if s else "", "title": self._title()})
            if route == "login" and method == "POST":
                return self._api_login(h)
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
            if isinstance(d, (bytes, bytearray)):
                return self._send(h, 200, "image/png", bytes(d), {"Cache-Control": "private, max-age=60"})
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

    # ------------------------------------------------------------ карта своих участков
    def _my_map_ids(self, uid):
        wd = players.find_world_dir(self.cfg)
        raw = players._read_json(players._user_file(wd, uid)) if wd else {}
        ids = [t.get("mapId") for t in raw.get("userTerritories") or []]
        if raw.get("mapId") is not None:
            ids.append(raw.get("mapId"))
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
        with self._map_lock:     # не рисовать одну карту параллельно
            hit = self._map_img.get(mp)
            if not hit or time.time() - hit[0] >= MAP_IMG_CACHE_SEC:
                d = players.map_pixels(self.cfg, mp)
                if not d.get("ok"):
                    return d
                hit = self._map_img[mp] = (time.time(), d)
        m = hit[1]
        rects = [(t["x"] * 8, t["y"] * 8, t["x"] * 8 + 7, t["y"] * 8 + 7)
                 for t in ((tt.get("pos") or {}) for tt in raw.get("userTerritories") or [] if tt.get("mapId") == mp)
                 if t.get("x") is not None and t.get("y") is not None]
        me = self._my_pos(wd, raw)
        if me.get("map") == mp and me.get("x") is not None:
            rects.append((me["x"], me["y"], me["x"], me["y"]))
        return players.mapdt._png_bytes(m["w"], m["h"], self._fog(m["w"], m["h"], m["pixels"], rects, FOG_RADIUS), 1)

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
@media (max-width:600px){main{padding:10px}}
</style>
</head>
<body>
<header><h1 id="ttl">__TITLE__</h1><span class="sp"></span><span id="who" class="muted"></span></header>
<nav id="nav" style="display:none"></nav>
<main id="main"></main>
<script>
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
  return fetch(p,o).then(function(r){
    if(r.status===401){ S.nick=""; render(); throw {error:"auth"}; }
    return r.json().then(function(j){ if(!r.ok) throw j; return j; }); });
}
var ERR={bad_login:"Неверный ник или пароль", throttled:"Слишком много попыток, подождите", auth:"Сессия истекла", internal:"Ошибка сервера"};
function errText(e){ return (e&&(ERR[e.error]||e.error))||"Нет связи с сервером"; }
function errBox(e){ return el("div",{class:"msg err"},[errText(e)]); }
function card(title,kids){ return el("div",{class:"card"},[title? el("h3",{},[title]):null].concat(kids)); }
function table(head,rows,mk){ var t=el("table",{},[el("tr",{},head.map(function(h){ return el("th",{},[h]); }))]);
  rows.forEach(function(r){ t.appendChild(el("tr",{},mk(r).map(function(c){ return el("td",{},[c]); }))); }); return t; }
function kv(pairs){ var d=el("div",{class:"kv"}); pairs.forEach(function(p){ if(p[1]==null||p[1]==="") return; d.appendChild(el("div",{},[p[0]])); d.appendChild(el("div",{},[p[1]])); }); return d; }
function load(box,path,draw){ box.innerHTML=""; box.appendChild(el("p",{class:"muted"},["Загрузка…"]));
  api(path).then(function(d){ box.innerHTML=""; draw(d); }).catch(function(e){ box.innerHTML=""; box.appendChild(errBox(e)); }); }
function fmtMin(m){ if(m==null) return ""; var h=Math.floor(m/60), mm=Math.round(m%60); return h? h+" ч "+mm+" мин" : mm+" мин"; }

// ---------------------------------------------------------------- вход
function renderLogin(){
  // настоящая <form> с name/autocomplete — браузер сам предложит сохранить ник и пароль
  var nick=el("input",{name:"username",placeholder:"Ник в игре",autocomplete:"username"});
  var code=el("input",{name:"password",type:"password",placeholder:"Пароль из игры",autocomplete:"current-password"});
  var rem=el("input",{type:"checkbox",id:"rem",style:"width:auto;margin:0"});
  try{ rem.checked=localStorage.getItem("swp_rem")!=="0"; }catch(e){ rem.checked=true; }
  var msg=el("div",{class:"msg err",style:"display:none"});
  function go(ev){ ev.preventDefault(); msg.style.display="none";
    try{ localStorage.setItem("swp_rem",rem.checked?"1":"0"); }catch(e){}
    api("/api/login",{nick:nick.value,code:code.value,remember:rem.checked}).then(function(d){ S.nick=d.nick; render(); })
      .catch(function(e){ msg.textContent=errText(e)+(e.retry? " ("+e.retry+" с)":""); msg.style.display=""; }); }
  $("#main").appendChild(el("form",{class:"login card",method:"post",action:"#",onsubmit:go},[el("h3",{},["Вход для игроков"]),
    el("p",{class:"muted small"},["Ник и пароль — те же, что при входе на сервер."]), nick, code,
    el("label",{for:"rem",class:"row small",style:"margin:0 0 12px;cursor:pointer"},[rem,"Запомнить меня на 30 дней"]),
    el("button",{class:"pri",type:"submit",style:"width:100%"},["Войти"]), el("div",{style:"margin-top:10px"},[msg])]));
}

// ---------------------------------------------------------------- каркас
var TABS=[["me","Профиль"],["tech","Изучение"],["craft","Крафт"],["market","Рынок"],["map","Карта"],["clan","Клан"],["server","Сервер"]];
function render(){
  var m=$("#main"), nav=$("#nav"), who=$("#who"); m.innerHTML=""; nav.innerHTML=""; who.innerHTML="";
  if(!S.nick){ nav.style.display="none"; return renderLogin(); }
  nav.style.display="";
  who.appendChild(document.createTextNode(S.nick+"  "));
  who.appendChild(el("button",{onclick:function(){ api("/api/logout",{}).finally(function(){ S.nick=""; render(); }); }},["Выйти"]));
  TABS.forEach(function(t){ nav.appendChild(el("button",{class:S.tab===t[0]?"on":"",onclick:function(){
    S.tab=t[0]; try{ localStorage.setItem("swp_tab",S.tab); }catch(e){} render(); }},[t[1]])); });
  ({me:tabMe,tech:tabTech,craft:tabCraft,market:tabMarket,map:tabMap,clan:tabClan,server:tabServer}[S.tab]||tabMe)(m);
}

// ---------------------------------------------------------------- профиль
var PARAM={0:"Энергия",1:"Сытость",2:"Здоровье",3:"Меткость",4:"Скорость движения",5:"Скорость действия",6:"Скорость атаки",
  7:"Генетика A",8:"Генетика B",9:"Генетика C",10:"Генетика D",11:"Кислород",12:"Очки генетики"};
var LPARAM={0:"Опыт",1:"Уровень",2:"Очки распределения"};
function invTable(rows){ if(!rows||!rows.length) return el("div",{class:"muted"},["пусто"]);
  return el("div",{class:"scroll"},[table(["Предмет","Кол-во","Прочность"],rows,function(r){ return [r.name, r.count, r.durability==null?"":r.durability]; })]); }
function tabMe(m){
  var box=el("div"); m.appendChild(box);
  load(box,"/api/me",function(d){
    var p=d.profile, r=d.research;
    var head=card(d.name,[kv([
      ["Статус", el("span",{class:"pill "+(d.online?"ok":"")},[d.online?"в игре":"не в игре"])],
      ["Уровень",p.level],["Клан",p.clan_name? p.clan_name+(p.clan_role!=null?" ("+["Лидер","Офицер","Участник","Капрал"][p.clan_role]+")":""):"—"],
      ["Очки клана",p.clan_point],["Рейтинг",p.rating],["Наиграно",p.playtime_h+" ч"],
      ["Сессий",d.sessions.total? d.sessions.total+" · в среднем "+d.sessions.avg_min+" мин":""],
      ["Первый вход",p.first_seen],
      p.banned? ["Бан", el("span",{class:"pill err"},[p.ban_expires_in_h? "ещё "+p.ban_expires_in_h+" ч":"да"])] : ["",""] ]),
      el("h3",{style:"margin-top:14px"},["Исследования"]),
      kv([["Сейчас изучается", r.current? r.current_name+(r.remaining_min!=null?" · осталось "+fmtMin(r.remaining_min):"") : "ничего"],
        ["Изучено технологий", r.done_count],["Вложено времени", r.invested_h+" ч"],["Ускорители", r.booster]])]);
    var params=(d.avatar.params||[]).map(function(x){
      var pct=x.max? Math.max(0,Math.min(100,100*x.val/x.max)):null;
      return el("div",{style:"margin-bottom:6px"},[el("div",{class:"row small",style:"justify-content:space-between"},[
        el("span",{},[PARAM[x.type]||("#"+x.type)]), el("span",{class:"muted"},[(Math.round(x.val*10)/10)+(x.max?" / "+Math.round(x.max*10)/10:"")])]),
        pct!=null? el("div",{class:"bar"},[el("i",{style:"width:"+pct+"%"})]) : null]); });
    var lp=(d.avatar.long_params||[]).map(function(x){ return [LPARAM[x.type]||("#"+x.type), x.val]; });
    var av=card("Аватар",[kv(lp), el("div",{style:"margin-top:10px"},params)]);
    var ab=card("Способности"+((d.avatar.abilities||[]).length?" ("+d.avatar.abilities.length+")":""),[(d.avatar.abilities||[]).length?
      el("div",{class:"chips grow"},d.avatar.abilities.map(function(a){ return el("span",{class:"chip"},[a]); }))
      : el("div",{class:"muted"},["нет"])]);
    var pos=d.position||{};
    var terr=(pos.territories||[]).length? el("div",{class:"grow"},[table(["Карта","X","Y"],pos.territories,function(t){ return [t.map_name,t.x,t.y]; })])
      : el("div",{class:"muted"},["нет территорий"]);
    var where=card("Где я",[kv([["Карта",pos.map_name],["Координаты",pos.x!=null? Math.round(pos.x)+", "+Math.round(pos.y):""],
      ["Точка возрождения",pos.respawn? pos.respawn.map_name+" · "+Math.round(pos.respawn.x||0)+", "+Math.round(pos.respawn.y||0):""]]),
      el("div",{class:"muted small",style:"margin:10px 0 4px"},["Мои территории"]), terr]);
    ab.classList.add("fill"); where.classList.add("fill");
    box.appendChild(el("div",{class:"grid"},[head,av,ab,where]));
    box.appendChild(el("div",{class:"grid"},[
      card("Склад"+(d.avatar.stash_size?" ("+d.avatar.stash.length+" / "+d.avatar.stash_size+")":""),[invTable(d.avatar.stash)]),
      card("С собой"+(d.avatar.carry_size?" ("+d.avatar.carry.length+" / "+d.avatar.carry_size+")":""),[invTable(d.avatar.carry)])]));
    var extra=[];
    var sc=function(t){ return el("div",{class:"scroll",style:"max-height:320px"},[t]); };
    if(d.friends.length) extra.push(card("Друзья ("+d.friends.length+")",[sc(table(["Игрок","Уровень","Клан"],d.friends,function(f){
      return [el("span",{},[f.online? el("span",{class:"pill ok",title:"в игре"},["●"]):null," "+f.name]), f.level==null?"":f.level, f.clan||"—"]; }))]));
    if(d.deaths.length) extra.push(card("Последние смерти",[sc(table(["Когда","Что"],d.deaths,function(x){ return [x.ts,x.event]; }))]));
    if(d.rewards.length) extra.push(card("Награды за рейтинг",[sc(table(["Когда","Награда"],d.rewards,function(x){ return [x.ts,x.reward]; }))]));
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
    if(done[n.id]){ st="изучено"; fill=stroke="var(--ok)"; cnt.done++; }
    else if(cur===n.id){ st="изучается"; fill=stroke="var(--warn)"; }
    else if(!n.parent||!byId[n.parent]||done[n.parent]){ st="доступно"; fill="transparent"; stroke="var(--acc)"; cnt.avail++; }
    else { st="закрыто"; fill="transparent"; stroke="var(--line)"; cnt.lock++; }
    var h=hl&&hl[n.id];
    var tip=n.label+(n.cost_h!=null?"\nВремя: "+n.cost_h+" ч":"")+"\n"+st+((n.unlocks&&n.unlocks.length)?"\nОткрывает: "+n.unlocks.join(", "):"");
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
    var info=el("div",{style:"margin-top:10px"},[el("div",{class:"muted small"},["Нажмите на технологию — покажу путь до неё и сколько осталось."])]);
    var legend=el("div",{class:"row small muted",style:"margin-bottom:6px"});
    function draw(hl){ holder.innerHTML=""; var s=techScheme(nodes,done,me.research.current,hl,pick); holder.appendChild(s.svg);
      legend.textContent="Изучено "+s.cnt.done+" / "+nodes.length+" · доступно сейчас "+s.cnt.avail+" · закрыто "+s.cnt.lock; }
    function pick(n){
      var chain=[],c=n,g=0; while(c&&g++<200){ chain.unshift(c); c=c.parent? by[c.parent]:null; }
      var hl={}; chain.forEach(function(x){ hl[x.id]=true; }); draw(hl);
      var miss=chain.filter(function(x){ return !done[x.id]; }), h=0; miss.forEach(function(x){ h+=x.cost_h||0; });
      info.innerHTML="";
      info.appendChild(card(n.label,[
        el("div",{},[miss.length? "Осталось: "+miss.length+" шаг(ов) · ~"+(Math.round(h*10)/10)+" ч" : el("span",{class:"pill ok"},["изучено"])]),
        el("div",{class:"chips",style:"margin-top:6px"},chain.map(function(x){ return el("span",{class:"chip",style:done[x.id]?"color:var(--ok);border-color:var(--ok)":""},[(done[x.id]?"✓ ":"")+x.label]); })),
        (n.unlocks&&n.unlocks.length)? el("div",{class:"small",style:"margin-top:8px"},["Открывает: "+n.unlocks.join(", ")]) : null]));
    }
    draw(null);
    var cur=me.research.current? card("Сейчас",[kv([["Изучается",me.research.current_name],["Осталось",fmtMin(me.research.remaining_min)]])]) : null;
    box.appendChild(el("div",{},[cur, card("Схема изучения",[legend,holder,info])]));
  }).catch(function(e){ box.appendChild(errBox(e)); });
}

// ---------------------------------------------------------------- крафт
function tabCraft(m){
  var plan=el("div");
  var inp=el("input",{list:"cr-dl",placeholder:"Что скрафтить?",style:"min-width:240px"}), dl=el("datalist",{id:"cr-dl"});
  var qty=el("input",{type:"number",min:"1",value:"1",style:"width:80px"}), byName={};
  api("/api/craft-catalog").then(function(d){ (d.items||[]).forEach(function(it){ byName[it.name.toLowerCase()]=it.id; dl.appendChild(el("option",{value:it.name})); }); }).catch(function(){});
  function doPlan(item){
    var id=byName[(item||"").toLowerCase()]||item; if(!id) return;
    load(plan,"/api/craft-plan?item="+encodeURIComponent(id)+"&qty="+(parseInt(qty.value)||1),function(d){
      if(!d.ok){ plan.appendChild(errBox(d)); return; }
      var out=[el("div",{class:"muted small"},["Время крафта: "+Math.round(d.time_s)+" с"+(d.benches.length?" · нужно: "+d.benches.join(", "):"")])];
      out.push(el("h3",{style:"margin-top:10px"},["Сырьё"]));
      out.push(table(["Ресурс","Нужно"],d.raw,function(r){ return [r.name,r.count]; }));
      if(d.intermediate.length){ out.push(el("h3",{style:"margin-top:10px"},["Промежуточное"]));
        out.push(table(["Предмет","Нужно","Крафтов"],d.intermediate,function(r){ return [r.name,r.need,r.crafts||""]; })); }
      if(d.techs.length){ out.push(el("h3",{style:"margin-top:10px"},["Технологии"]));
        out.push(table(["Технология","У меня"],d.techs,function(x){ return [x.label, x.known? el("span",{class:"pill ok"},["изучено"])
          : el("span",{class:"pill warn"},["нет · ещё "+x.missing_chain+" шаг(ов), ~"+x.missing_h+" ч"])]; })); }
      plan.appendChild(card(d.name+" × "+d.qty,out));
    });
  }
  m.appendChild(card("Раскладка до сырья",[el("div",{class:"row"},[inp,dl,qty,el("button",{class:"pri",onclick:function(){ doPlan(inp.value); }},["Посчитать"])]),
    el("div",{style:"margin-top:10px"},[plan])]));
}

// ---------------------------------------------------------------- рынок
function fmtItems(a){ return (a||[]).map(function(x){ return x.name+" ×"+x.count; }).join(", ")||"—"; }
function median(a){ a=a.slice().sort(function(x,y){ return x-y; }); var n=a.length; return n? (n%2? a[(n-1)/2] : (a[n/2-1]+a[n/2])/2) : null; }
function num(v){ return v==null? "" : (v>=100? Math.round(v).toLocaleString("ru") : String(Math.round(v*100)/100)); }
// курс простого лота: «N валюты за 1 шт», а если товар дешёвый — «1 валюты = N шт»
function rate(o){ if(o.unit==null||!o.unit||o.give.length!==1||o.want.length!==1) return "";
  return o.unit>=1? num(o.unit)+" "+o.want[0].name+" за 1" : "1 "+o.want[0].name+" = "+num(1/o.unit)+" шт"; }
function tabMarket(m){
  var q=el("input",{placeholder:"Предмет, например: Железный слиток",style:"min-width:260px;flex:1"});
  var mode=el("select",{},[el("option",{value:"give"},["продают"]),el("option",{value:"want"},["просят взамен"]),el("option",{value:"any"},["везде"])]);
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
        if(gv.name.toLowerCase().indexOf(t)>=0) r={item:gv.name, cur:wn.name, p:wn.count/gv.count, side:"продают"};
        else if(wn.name.toLowerCase().indexOf(t)>=0) r={item:wn.name, cur:gv.name, p:gv.count/wn.count, side:"покупают"};
        else return;
        var k=r.side+"|"+r.item+"|"+r.cur; (g[k]=g[k]||{item:r.item,cur:r.cur,side:r.side,u:[]}).u.push(r.p); });
      var ps=Object.keys(g).map(function(k){ return g[k]; }).sort(function(a,b){ return b.u.length-a.u.length; });
      if(ps.length) sum.appendChild(card("Цена за 1 шт",[table(["Товар","Сделка","Платят","Мин","Медиана","Макс","Лотов"],ps,function(x){
        return [x.item,x.side,x.cur,num(Math.min.apply(null,x.u)),num(median(x.u)),num(Math.max.apply(null,x.u)),x.u.length]; })]));
    }
    rows.sort(function(a,b){ return (a.unit==null)-(b.unit==null) || (a.unit||0)-(b.unit||0); });
    var shown=rows.slice(0,300);
    list.appendChild(card("Предложения ("+rows.length+(rows.length>shown.length?", показаны первые "+shown.length:"")+")",[
      rows.length? el("div",{class:"scroll",style:"max-height:600px"},[table(["Отдаёт","Просит","Курс","Продавец","Где"],shown,function(o){
        return [fmtItems(o.give),fmtItems(o.want),rate(o),
          el("span",{},[o.owner+(o.clan?" ["+o.clan+"]":""), o.mine? el("span",{class:"pill ok",style:"margin-left:6px"},["моё"]):null]), o.where]; })])
      : el("div",{class:"muted"},[t? "ничего не нашлось" : "предложений нет"])]));
  }
  function fetchM(){
    api("/api/market").then(function(d){
      if(S.tab!=="market") return;
      if(d.pending){ info.textContent="Собираю предложения со всех карт — это до пары минут, страница обновится сама…"; setTimeout(fetchM,4000); return; }
      data=d; info.textContent="Обновлено "+(d.age_s<90? "только что" : Math.round(d.age_s/60)+" мин назад")+" · всего предложений: "+d.offers.length
        +(d.refreshing? " · обновляю в фоне…" : ""); draw();
    }).catch(function(e){ info.textContent=""; list.innerHTML=""; list.appendChild(errBox(e)); });
  }
  var tmr; q.addEventListener("input",function(){ clearTimeout(tmr); tmr=setTimeout(draw,200); }); mode.addEventListener("change",draw);
  m.appendChild(card("Рынок",[el("div",{class:"row"},[q,mode]),info,
    el("div",{class:"muted small"},["Терминалы игроков и магазины на картах. Курс и цены — только для простых лотов «один товар за одну валюту»; сводка цен появляется при поиске."])]));
  m.appendChild(sum); m.appendChild(list); fetchM();
}

// ---------------------------------------------------------------- карта своих участков
function tabMap(m){
  var box=el("div"); m.appendChild(box);
  load(box,"/api/my-maps",function(d){
    if(!d.maps.length){ box.appendChild(card("Карта",[el("div",{class:"muted"},["У вас пока нет участков."])])); return; }
    var sel=el("select",{}), zoom=6, rot=315, cur=null, sc=1;
    try{ var r0=parseInt(localStorage.getItem("swp_rot")); if(!isNaN(r0)) rot=r0; }catch(e){}
    d.maps.forEach(function(x,i){ sel.appendChild(el("option",{value:i},[x.name+" · участков: "+x.territories.length+(x.here?" · вы здесь":"")])); });
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
      cur.territories.forEach(function(t){ layer.appendChild(el("div",{class:"mk",title:"мой участок "+t.x+", "+t.y,
        style:"position:absolute;left:"+(t.x*8*k)+"px;top:"+((cur.h-t.y*8-8)*k)+"px;width:"+(8*k)+"px;height:"+(8*k)+"px;"
          +"background:rgba(80,255,120,.25);outline:2px solid #3fff7a;box-sizing:border-box"})); });
      if(d.me.map===cur.map && d.me.x!=null) layer.appendChild(el("div",{class:"mk",title:"вы здесь",
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
      cur=d.maps[+sel.value]; st.textContent="загрузка карты…"; img.removeAttribute("src");
      img.onload=function(){ st.textContent=""; place(); focusMine(); };
      img.onerror=function(){ st.textContent="карта не загрузилась"; };
      img.src="/api/my-map-image?map="+cur.map;
      tlist.innerHTML="";
      if(cur.territories.length) tlist.appendChild(card("Участки на этой карте ("+cur.territories.length+")",[el("div",{class:"scroll",style:"max-height:260px"},[
        table(["#","Участок (X, Y)","Клетки",""],cur.territories,function(t){ return [cur.territories.indexOf(t)+1, t.x+", "+t.y,
          (t.x*8)+"–"+(t.x*8+7)+", "+(t.y*8)+"–"+(t.y*8+7), el("button",{onclick:function(){ focus(t.x*8+4,t.y*8+4); view.scrollIntoView({block:"nearest"}); }},["показать"])]; })])]));
    }
    function setRot(v){ keep(function(){ rot=v; try{ localStorage.setItem("swp_rot",String(rot)); }catch(e){} }); }
    sel.addEventListener("change",show);
    box.appendChild(card("Мои участки на карте",[el("div",{class:"row",style:"margin-bottom:8px"},[sel,
      el("button",{title:"повернуть против часовой на 45°",onclick:function(){ setRot(rot-45); }},["↺"]), rl,
      el("button",{title:"повернуть по часовой на 45°",onclick:function(){ setRot(rot+45); }},["↻"]),
      el("button",{onclick:function(){ keep(function(){ zoom=Math.max(1,zoom-1); }); }},["−"]), zl,
      el("button",{onclick:function(){ keep(function(){ zoom=Math.min(16,zoom+1); }); }},["+"]),
      el("button",{onclick:focusMine},["ко мне"]), st]),
      el("div",{class:"muted small",style:"margin-bottom:6px"},["Видно "+d.fog_radius+" клеток вокруг вас и вокруг ваших участков — остальное скрыто туманом. Зелёные квадраты — ваши участки, красная точка — вы."]), view]));
    box.appendChild(tlist); show();
  });
}

// ---------------------------------------------------------------- клан
var CE={joined:"вступил",left:"ушёл",role:"роль",tech:"клан-технология",renamed:"переименован",slots:"слоты",created:"создан",disbanded:"распущен"};
// Линейный график с осями: Y — 3 деления (мин/середина/макс), X — время.
function chart(pts,o){
  o=o||{}; var W=o.wide?1100:520,H=o.wide?230:200,L=44,R=10,T=10,B=34;
  var box=el("div",{style:"flex:1;min-width:260px"},[el("div",{class:"small muted"},[o.title||""])]);
  if(pts.length<2){ box.appendChild(el("div",{class:"muted small"},["мало данных"])); return box; }
  var t0=pts[0].t,t1=pts[pts.length-1].t,lo=Infinity,hi=-Infinity; pts.forEach(function(p){ lo=Math.min(lo,p.v); hi=Math.max(hi,p.v); });
  if(o.zero) lo=Math.min(0,lo);
  if(hi===lo){ hi+=1; if(!o.zero) lo-=1; }
  function X(t){ return L+(W-L-R)*(t-t0)/((t1-t0)||1); } function Y(v){ return T+(H-T-B)*(1-(v-lo)/(hi-lo)); }
  var fmt=function(v){ return Math.abs(v)>=1000? Math.round(v).toLocaleString("ru") : String(Math.round(v*10)/10); };
  var kids=[];
  [lo,(lo+hi)/2,hi].forEach(function(v){ kids.push(svgEl("line",{x1:L,x2:W-R,y1:Y(v),y2:Y(v),stroke:"var(--line)","stroke-dasharray":"3 3"}));
    kids.push(svgEl("text",{x:L-6,y:Y(v)+4,"text-anchor":"end","font-size":"11",fill:"var(--mut)"},[fmt(v)])); });
  var span=t1-t0, n=4;
  for(var i=0;i<=n;i++){ var t=t0+span*i/n, d=new Date(t*1000);
    var lab= span>2*86400? (d.getDate()+"."+String(d.getMonth()+1).padStart(2,"0")) : (String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0"));
    kids.push(svgEl("line",{x1:X(t),x2:X(t),y1:H-B,y2:H-B+4,stroke:"var(--mut)"}));
    kids.push(svgEl("text",{x:X(t),y:H-B+16,"text-anchor":i===0?"start":i===n?"end":"middle","font-size":"11",fill:"var(--mut)"},[lab])); }
  kids.push(svgEl("line",{x1:L,x2:W-R,y1:H-B,y2:H-B,stroke:"var(--mut)"}));
  kids.push(svgEl("line",{x1:L,x2:L,y1:T,y2:H-B,stroke:"var(--mut)"}));
  var d=pts.map(function(p,i){ return (i?"L":"M")+X(p.t).toFixed(1)+" "+Y(p.v).toFixed(1); }).join(" ");
  kids.push(svgEl("path",{d:d+" L"+X(t1)+" "+(H-B)+" L"+X(t0)+" "+(H-B)+" Z",fill:"var(--acc)","fill-opacity":"0.12",stroke:"none"}));
  kids.push(svgEl("path",{d:d,fill:"none",stroke:"var(--acc)","stroke-width":"2"}));
  kids.push(svgEl("text",{x:(L+W-R)/2,y:H-2,"text-anchor":"middle","font-size":"11",fill:"var(--mut)"},[o.x||"время"]));
  kids.push(svgEl("text",{x:12,y:(T+H-B)/2,"text-anchor":"middle","font-size":"11",fill:"var(--mut)",transform:"rotate(-90 12 "+((T+H-B)/2)+")"},[o.y||""]));
  box.appendChild(svgEl("svg",{viewBox:"0 0 "+W+" "+H,style:"width:100%;height:auto;display:block"},kids));
  return box;
}
function tabClan(m){
  var box=el("div"); m.appendChild(box);
  load(box,"/api/clan",function(d){
    if(d.none){ box.appendChild(card("Клан",[el("div",{class:"muted"},["Вы не состоите в клане."])])); return; }
    box.appendChild(card(d.name,[kv([["Участников",d.size+(d.max?" / "+d.max:"")],["Сейчас в игре",d.online],["Рейтинг",d.rating],
      ["Очки клана",d.clan_point],["Клан-технологий",d.tech.length]]),
      d.tech_named.length? el("div",{class:"chips",style:"margin-top:8px"},d.tech_named.map(function(x){ return el("span",{class:"chip"},[x.label]); })) : null]));
    box.appendChild(card("Состав",[el("div",{class:"scroll"},[table(["Игрок","Роль","Уровень","Специализация","Был"],d.members,function(x){
      return [el("span",{},[x.online? el("span",{class:"pill ok"},["●"]):null," "+x.name]), x.role_name, x.level==null?"":x.level,
        x.spec? "+"+x.spec.positive+" / −"+x.spec.negative : "",
        x.online? "сейчас" : (x.last_seen_h==null? "" : x.last_seen_h<48? x.last_seen_h+" ч назад" : Math.round(x.last_seen_h/24)+" дн назад")]; })])]));
    var h=d.history||{};
    if(h.series&&h.series.length>1){ var s=function(k){ return h.series.map(function(p){ return {t:p.t,v:p[k]}; }); };
      box.appendChild(card("Рост клана (30 дней)",[el("div",{class:"row",style:"align-items:flex-start;gap:16px"},[chart(s("rating"),{title:"Рейтинг",y:"рейтинг",x:"дата"}),chart(s("cp"),{title:"Очки клана",y:"очки",x:"дата"}),chart(s("size"),{title:"Состав",y:"участников",x:"дата",zero:true})])])); }
    if(h.events&&h.events.length) box.appendChild(card("События",[el("div",{class:"scroll small"},h.events.slice(0,100).map(function(e){
      return el("div",{},[el("span",{class:"muted"},[e.ts+"  "]), (CE[e.kind]||e.kind)+" ", e.name||"",
        e.kind==="role"? " "+e.was+" → "+e.role : e.kind==="tech"? " "+(e.label||e.tech) : ""]); }))]));
  });
}

// ---------------------------------------------------------------- сервер
function tabServer(m){
  var box=el("div"); m.appendChild(box);
  load(box,"/api/server",function(d){
    box.appendChild(card("Сервер",[kv([["Статус",el("span",{class:"pill "+(d.game_up?"ok":"err")},[d.game_up?"работает":"не запущен"])],
      ["Сейчас онлайн",d.online.length],["Пик за сутки",d.online_peak]]),
      el("div",{style:"margin-top:10px"},[chart(d.online_series,{title:"Онлайн за 24 часа",y:"игроков",x:"время",zero:true,wide:true})])]));
    function top(title,rows,col){ return card(title,[rows.length? table(["#","Игрок",col],rows,function(r){ return [rows.indexOf(r)+1, r.name, r.v]; })
      : el("div",{class:"muted"},["пока нет данных"])]); }
    box.appendChild(el("div",{class:"grid"},[
      card("Сезонный рейтинг",[d.rating.length? table(["#","Игрок","Награда"],d.rating,function(r){ return [d.rating.indexOf(r)+1,r.name,r.reward? r.reward+" ускор.":""]; }) : el("div",{class:"muted"},["пока нет данных"])]),
      top("Торговцы",d.traders,"Продаж"),
      card("Кланы",[d.clans.length? table(["Клан","Рейтинг","За 7 дней"],d.clans,function(c){ return [c.name,c.rating,c.growth==null?"":(c.growth>0?"+":"")+c.growth]; })
        : el("div",{class:"muted"},["пока нет данных"])])]));
    box.appendChild(card("Сейчас на сервере ("+d.online.length+")",[d.online.length?
      el("div",{class:"scroll"},[table(["#","Игрок","Уровень","Клан"],d.online,function(p){ return [d.online.indexOf(p)+1,p.name,p.level==null?"":p.level,p.clan||"—"]; })])
      : el("div",{class:"muted"},["никого"])]));
  });
}

api("/api/session").then(function(d){ S.nick=d.authed? d.nick:""; if(d.title) document.title=d.title; render(); })
  .catch(function(){ render(); });
</script>
</body>
</html>
"""
