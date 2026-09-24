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
LEADERS_CACHE_SEC = 300


class PlayerWeb:
    def __init__(self, cfg, state, ct_events=None, ct_points=None, tt_log=None):
        self.cfg = cfg
        self.state = state
        base = cfg.get("base_dir") or os.path.dirname(os.path.abspath(__file__))
        self._base = base
        self._ct_events = ct_events or os.path.join(base, "logs", "clan_events.jsonl")
        self._ct_points = ct_points or os.path.join(base, "logs", "clan_points.jsonl")
        self._tt_log = tt_log or os.path.join(base, "logs", "tech_track.jsonl")
        self._sessions = {}          # tok -> {uid, nick, ts}
        self._slock = threading.Lock()
        self.throttle = Throttle()                                   # по IP
        self.nick_throttle = Throttle(max_fail=8, window=900, block=900)  # по нику (перебор с разных IP)
        self._cache = {}
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

    def _session(self, h):
        c = http.cookies.SimpleCookie(h.headers.get("Cookie", ""))
        tok = c["psid"].value if "psid" in c else ""
        if not tok:
            return "", None
        with self._slock:
            s = self._sessions.get(tok)
            if s and time.time() - s["ts"] > SESSION_TTL:
                self._sessions.pop(tok, None)
                s = None
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
                    self._sessions.pop(tok, None)
                return self._json(h, {"ok": True}, set_cookie="psid=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
            if method != "GET":
                return self._json(h, {"error": "unknown"}, 404)
            fn = getattr(self, "_api_" + route.replace("-", "_"), None)
            if not fn:
                return self._json(h, {"error": "unknown"}, 404)
            d = fn(s["uid"], q)
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
        with self._slock:
            now = time.time()
            for t in [t for t, s in self._sessions.items() if now - s["ts"] > SESSION_TTL]:
                self._sessions.pop(t, None)
            self._sessions[tok] = {"uid": uid, "nick": nick, "ts": now}
        logging.info("playerweb: вход %s (#%s) с %s", nick, uid, ip)
        return self._json(h, {"ok": True, "nick": nick},
                          set_cookie="psid=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (tok, SESSION_TTL))

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
            "position": d.get("position"),
            "avatar": {"params": a.get("params"), "long_params": a.get("long_params"), "skills": a.get("skills"),
                       "abilities": a.get("abilities"),
                       "stash": inv(a.get("stash")), "carry": inv(a.get("carry")),
                       "stash_size": a.get("stash_size"), "carry_size": a.get("carry_size")},
            "sessions": {k: (d.get("sessions") or {}).get(k) for k in ("total", "total_h", "avg_min", "max_min")},
            "friends": [{"name": f.get("name")} for f in d.get("friends") or []],
            "deaths": ((d.get("activity") or {}).get("deaths") or [])[:10],
            "rewards": ((d.get("activity") or {}).get("rewards") or [])[:10],
        }

    def _api_tech_tree(self, uid, q):
        return self._cached("tech_tree", 600, lambda: players.tech_tree(self.cfg))

    def _api_craftable(self, uid, q):
        return players.player_craftable(self.cfg, uid)

    def _api_craft_catalog(self, uid, q):
        return self._cached("craft_catalog", 600, lambda: players.craft_catalog(self.cfg))

    def _api_craft_plan(self, uid, q):
        g = lambda k: (q.get(k) or [""])[0]
        return players.craft_plan(self.cfg, g("item")[:80], g("qty") or 1, uid, None)

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
            d["history"] = players.clan_history(self.cfg, cid, self._ct_events, self._ct_points, 30)
        return d

    def _api_server(self, uid, q):
        snap = self.state.data.get("last_snapshot") or {}
        wd = players.find_world_dir(self.cfg)
        online, total = [], 0
        if wd:
            names = players.load_user_list(wd)
            total = len(names)
            online = sorted(names.get(u) or ("id %s" % u) for u, on in players._online_now(wd).items() if on)
        lb = self._cached("leaders", LEADERS_CACHE_SEC,
                          lambda: players.leaderboards(self.cfg, self._tt_log, self._ct_points))
        rt = self._cached("rating", LEADERS_CACHE_SEC, lambda: players.rating_top(self.cfg, 10))
        # «id N» — аккаунта уже нет в user_list (удалён) — игрокам такие строки ни к чему
        strip = lambda rows: [{"name": r.get("name"), "v": r.get("v")} for r in rows or []
                              if not str(r.get("name") or "").startswith("id ")]
        return {
            "ok": True,
            "game_up": bool((snap.get("game") or {}).get("running")),
            "snapshot_age": int(time.time() - snap["ts"]) if snap.get("ts") else None,
            "online": online, "players_total": total,
            "rating": [{"name": r["name"], "reward": r["reward"]} for r in rt.get("top") or []],
            "research_week": strip(lb.get("research_week")),
            "research_total": strip(lb.get("research_total")),
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
  var nick=el("input",{placeholder:"Ник в игре",autocomplete:"username"});
  var code=el("input",{type:"password",placeholder:"Пароль из игры",autocomplete:"current-password"});
  var msg=el("div",{class:"msg err",style:"display:none"});
  function go(){ msg.style.display="none";
    api("/api/login",{nick:nick.value,code:code.value}).then(function(d){ S.nick=d.nick; render(); })
      .catch(function(e){ msg.textContent=errText(e)+(e.retry? " ("+e.retry+" с)":""); msg.style.display=""; }); }
  code.addEventListener("keydown",function(e){ if(e.key==="Enter") go(); });
  $("#main").appendChild(el("div",{class:"login card"},[el("h3",{},["Вход для игроков"]),
    el("p",{class:"muted small"},["Ник и пароль — те же, что при входе на сервер."]), nick, code,
    el("button",{class:"pri",style:"width:100%",onclick:go},["Войти"]), el("div",{style:"margin-top:10px"},[msg])]));
}

// ---------------------------------------------------------------- каркас
var TABS=[["me","Профиль"],["tech","Изучение"],["craft","Крафт"],["clan","Клан"],["server","Сервер"]];
function render(){
  var m=$("#main"), nav=$("#nav"), who=$("#who"); m.innerHTML=""; nav.innerHTML=""; who.innerHTML="";
  if(!S.nick){ nav.style.display="none"; return renderLogin(); }
  nav.style.display="";
  who.appendChild(document.createTextNode(S.nick+"  "));
  who.appendChild(el("button",{onclick:function(){ api("/api/logout",{}).finally(function(){ S.nick=""; render(); }); }},["Выйти"]));
  TABS.forEach(function(t){ nav.appendChild(el("button",{class:S.tab===t[0]?"on":"",onclick:function(){
    S.tab=t[0]; try{ localStorage.setItem("swp_tab",S.tab); }catch(e){} render(); }},[t[1]])); });
  ({me:tabMe,tech:tabTech,craft:tabCraft,clan:tabClan,server:tabServer}[S.tab]||tabMe)(m);
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
      p.banned? ["Бан", el("span",{class:"pill err"},[p.ban_expires_in_h? "ещё "+p.ban_expires_in_h+" ч":"да"])] : ["",""] ])]);
    var res=card("Исследования",[kv([
      ["Сейчас изучается", r.current? r.current_name+(r.remaining_min!=null?" · осталось "+fmtMin(r.remaining_min):"") : "ничего"],
      ["Изучено технологий", r.done_count],["Вложено времени", r.invested_h+" ч"],["Ускорители", r.booster]])]);
    var params=(d.avatar.params||[]).map(function(x){
      var pct=x.max? Math.max(0,Math.min(100,100*x.val/x.max)):null;
      return el("div",{style:"margin-bottom:6px"},[el("div",{class:"row small",style:"justify-content:space-between"},[
        el("span",{},[PARAM[x.type]||("#"+x.type)]), el("span",{class:"muted"},[(Math.round(x.val*10)/10)+(x.max?" / "+Math.round(x.max*10)/10:"")])]),
        pct!=null? el("div",{class:"bar"},[el("i",{style:"width:"+pct+"%"})]) : null]); });
    var lp=(d.avatar.long_params||[]).map(function(x){ return [LPARAM[x.type]||("#"+x.type), x.val]; });
    var av=card("Аватар",[kv(lp)].concat([el("div",{style:"margin-top:10px"},params)],
      (d.avatar.abilities||[]).length? [el("div",{class:"muted small",style:"margin-top:8px"},["Способности"]),
        el("div",{class:"chips"},d.avatar.abilities.map(function(a){ return el("span",{class:"chip"},[a]); }))] : []));
    var pos=d.position||{};
    var terr=(pos.territories||[]).length? table(["Карта","X","Y"],pos.territories,function(t){ return [t.map,t.x,t.y]; })
      : el("div",{class:"muted"},["нет территорий"]);
    var where=card("Где я",[kv([["Карта",pos.map],["Координаты",pos.x!=null? Math.round(pos.x)+", "+Math.round(pos.y):""],
      ["Точка возрождения",pos.respawn? "карта "+pos.respawn.map+" · "+Math.round(pos.respawn.x||0)+", "+Math.round(pos.respawn.y||0):""]]),
      el("div",{class:"muted small",style:"margin:10px 0 4px"},["Мои территории"]), terr]);
    box.appendChild(el("div",{class:"grid"},[head,res,av,where]));
    box.appendChild(el("div",{class:"grid"},[
      card("Склад"+(d.avatar.stash_size?" ("+d.avatar.stash.length+" / "+d.avatar.stash_size+")":""),[invTable(d.avatar.stash)]),
      card("С собой"+(d.avatar.carry_size?" ("+d.avatar.carry.length+" / "+d.avatar.carry_size+")":""),[invTable(d.avatar.carry)])]));
    var extra=[];
    if(d.friends.length) extra.push(card("Друзья",[el("div",{class:"chips"},d.friends.map(function(f){ return el("span",{class:"chip"},[f.name]); }))]));
    if(d.deaths.length) extra.push(card("Последние смерти",[table(["Когда","Что"],d.deaths,function(x){ return [x.ts,x.event]; })]));
    if(d.rewards.length) extra.push(card("Награды за рейтинг",[table(["Когда","Награда"],d.rewards,function(x){ return [x.ts,x.reward]; })]));
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
  var now=el("div"), plan=el("div");
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
  m.appendChild(now);
  load(now,"/api/craftable",function(d){
    function rec(r){ return el("span",{},r.res.map(function(x,i){ return el("span",{style:x.have<x.n?"color:var(--err)":""},[(i?", ":"")+x.name+" ×"+x.n]); })); }
    function nm(r){ return el("a",{href:"#",onclick:function(e){ e.preventDefault(); inp.value=r.name; doPlan(r.id); window.scrollTo(0,0); }},[r.name+(r.out>1?" ×"+r.out:"")]); }
    now.appendChild(card("Могу скрафтить из того, что есть ("+d.now.length+")",[
      el("p",{class:"muted small"},["Склад + инвентарь, с учётом моих технологий и технологий клана. Станки не учитываются."]),
      d.now.length? el("div",{class:"scroll"},[table(["Предмет","Сколько раз","Рецепт","Где"],d.now,function(r){ return [nm(r),r.max,rec(r),r.workbench]; })])
        : el("div",{class:"muted"},["пока ничего"])]));
    if(d.almost.length) now.appendChild(card("Не хватает одного ресурса",[el("div",{class:"scroll"},[
      table(["Предмет","Не хватает","Рецепт"],d.almost,function(r){ return [nm(r), r.short.name+" ×"+r.short.n, rec(r)]; })])]));
  });
}

// ---------------------------------------------------------------- клан
var CE={joined:"вступил",left:"ушёл",role:"роль",tech:"клан-технология",renamed:"переименован",slots:"слоты",created:"создан",disbanded:"распущен"};
function spark(pts,title){
  var W=300,H=70,P=4, box=el("div",{style:"flex:1;min-width:200px"},[el("div",{class:"small muted"},[title+(pts.length?": "+pts[pts.length-1].v:"")])]);
  if(pts.length<2) return box;
  var t0=pts[0].t,t1=pts[pts.length-1].t,lo=Infinity,hi=-Infinity; pts.forEach(function(p){ lo=Math.min(lo,p.v); hi=Math.max(hi,p.v); });
  if(hi===lo){ hi++; lo--; }
  var d=pts.map(function(p,i){ return (i?"L":"M")+(P+(W-2*P)*(p.t-t0)/((t1-t0)||1)).toFixed(1)+" "+(H-P-(H-2*P)*(p.v-lo)/(hi-lo)).toFixed(1); }).join(" ");
  box.appendChild(svgEl("svg",{viewBox:"0 0 "+W+" "+H,preserveAspectRatio:"none",style:"width:100%;height:70px"},[svgEl("path",{d:d,fill:"none",stroke:"var(--acc)","stroke-width":"2","vector-effect":"non-scaling-stroke"})]));
  return box;
}
function tabClan(m){
  var box=el("div"); m.appendChild(box);
  load(box,"/api/clan",function(d){
    if(d.none){ box.appendChild(card("Клан",[el("div",{class:"muted"},["Вы не состоите в клане."])])); return; }
    box.appendChild(card(d.name,[kv([["Участников",d.size+(d.max?" / "+d.max:"")],["Сейчас в игре",d.online],["Рейтинг",d.rating],
      ["Очки клана",d.clan_point],["Клан-технологий",d.tech.length]]),
      d.tech_named.length? el("div",{class:"chips",style:"margin-top:8px"},d.tech_named.map(function(x){ return el("span",{class:"chip"},[x.label]); })) : null]));
    box.appendChild(card("Состав",[el("div",{class:"scroll"},[table(["Игрок","Роль","Уровень","Специализация","Техов","Изучает","Был"],d.members,function(x){
      return [el("span",{},[x.online? el("span",{class:"pill ok"},["●"]):null," "+x.name]), x.role_name, x.level==null?"":x.level,
        x.spec? "+"+x.spec.positive+" / −"+x.spec.negative : "", x.tech_count, x.researching,
        x.online? "сейчас" : (x.last_seen_h==null? "" : x.last_seen_h<48? x.last_seen_h+" ч назад" : Math.round(x.last_seen_h/24)+" дн назад")]; })])]));
    var h=d.history||{};
    if(h.series&&h.series.length>1){ var s=function(k){ return h.series.map(function(p){ return {t:p.t,v:p[k]}; }); };
      box.appendChild(card("Рост клана (30 дней)",[el("div",{class:"row",style:"align-items:flex-start;gap:16px"},[spark(s("rating"),"Рейтинг"),spark(s("cp"),"Очки клана"),spark(s("size"),"Состав")])])); }
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
      ["Онлайн",d.online.length+" из "+d.players_total]]),
      d.online.length? el("div",{class:"chips",style:"margin-top:8px"},d.online.map(function(n){ return el("span",{class:"chip"},[n]); })) : null]));
    function top(title,rows,col){ return card(title,[rows.length? table(["#","Игрок",col],rows,function(r){ return [rows.indexOf(r)+1, r.name, r.v]; })
      : el("div",{class:"muted"},["пока нет данных"])]); }
    box.appendChild(el("div",{class:"grid"},[
      card("Сезонный рейтинг",[d.rating.length? table(["#","Игрок","Награда"],d.rating,function(r){ return [d.rating.indexOf(r)+1,r.name,r.reward? r.reward+" ускор.":""]; }) : el("div",{class:"muted"},["пока нет данных"])]),
      top("Исследования за неделю",d.research_week,"Техов"),
      top("Всего часов исследований",d.research_total,"Часов"),
      top("Торговцы",d.traders,"Продаж"),
      card("Кланы",[d.clans.length? table(["Клан","Рейтинг","За 7 дней"],d.clans,function(c){ return [c.name,c.rating,c.growth==null?"":(c.growth>0?"+":"")+c.growth]; })
        : el("div",{class:"muted"},["пока нет данных"])])]));
  });
}

api("/api/session").then(function(d){ S.nick=d.authed? d.nick:""; if(d.title) document.title=d.title; render(); })
  .catch(function(){ render(); });
</script>
</body>
</html>
"""
