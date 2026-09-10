# -*- coding: utf-8 -*-
"""Игроки локального сервера Sigma World Online: список + статус онлайн.

Читает файлы, которые пишет сам локальный сервер игры, из каталога мира под
``%USERPROFILE%\\AppData\\LocalLow\\Crematorium of Time\\SigmaWorld\\SigmaWorld\\LocalServer\\<мир>``:

* ``analytics.txt`` — журнал событий, строки ``ДД.ММ.ГГГГ Ч:ММ:СС: <событие> <id> [<сек>]``
  (``register`` / ``enter`` / ``exit``; у ``exit`` последнее число — длительность
  сессии в секундах). Час бывает **однозначным** (``0:01:52``).
* ``Data\\users\\user_list.json`` — ``{"userInfo":[{"Id":..,"Name":..,"Code":..}]}``.
  ``Code`` — это пароль игрока, **наружу не отдаём**.
* ``Data\\users\\user<N>.json`` — профиль: ``timeGame`` (всего секунд в игре),
  ``unitLevel``, ``role`` (0 = игрок, 1 = модератор, 2 = админ, 3 = GM),
  ``isBlock`` / ``timeBan``, ``mapId``, ``clanId``, ``country``, ``unitId``.
* ``Logs\\game_state.txt`` — авторитетные счётчики онлайна по картам (без имён).

Онлайн игрока = его последнее событие в ``analytics.txt`` — ``enter``. После
перезапуска сервера ``exit`` может потеряться, поэтому рядом отдаём и разбивку из
``game_state.txt``.

``snapshot(cfg)`` -> dict (см. конец файла). Пароли (``code`` / ``Code``) в выдачу
не попадают ни в каком виде.
"""
import glob
import io
import json
import logging
import os
import re
import shutil
from datetime import datetime

_LINE_RX = re.compile(
    r"^\s*(\d{1,2}\.\d{1,2}\.\d{4} \d{1,2}:\d{2}:\d{2}): (register|enter|exit) (\d+)(?: (\d+))?\s*$"
)
_USER_FILE_RX = re.compile(r"^user(\d+)\.json$", re.I)

DEFAULT_LOCALSERVER_ROOT = os.path.join(
    os.path.expanduser("~"),
    "AppData", "LocalLow", "Crematorium of Time", "SigmaWorld", "SigmaWorld", "LocalServer",
)


def _cfg_pl(cfg):
    return (cfg.get("players", {}) or {})


def localserver_root(cfg):
    r = (_cfg_pl(cfg).get("localserver_root") or "").strip()
    return r or DEFAULT_LOCALSERVER_ROOT


def find_world_dir(cfg):
    """Каталог активного мира: явный ``players.world_dir`` / имя ``players.world``
    под корнем, иначе — тот, где ``analytics.txt`` свежее всех."""
    pl = _cfg_pl(cfg)
    explicit = (pl.get("world_dir") or "").strip()
    if explicit:
        return explicit if os.path.isdir(explicit) else None
    root = localserver_root(cfg)
    if not os.path.isdir(root):
        return None
    want = (pl.get("world") or "").strip()
    if want:
        d = os.path.join(root, want)
        return d if os.path.isdir(d) else None
    best, best_mt = None, -1.0
    for d in glob.glob(os.path.join(root, "*")):
        ap = os.path.join(d, "analytics.txt")
        if os.path.isfile(ap):
            mt = os.path.getmtime(ap)
            if mt > best_mt:
                best, best_mt = d, mt
    return best


def _read_text(path, tail_bytes=0):
    try:
        if tail_bytes and os.path.getsize(path) > tail_bytes:
            with open(path, "rb") as f:
                f.seek(-tail_bytes, os.SEEK_END)
                data = f.read()
            return data.decode("utf-8", "replace").split("\n", 1)[-1]
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _read_json(path, default=None):
    """json из файла, терпит BOM (справочники игры сохранены с BOM)."""
    try:
        with io.open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


def _to_epoch(ts):
    try:
        return datetime.strptime(ts, "%d.%m.%Y %H:%M:%S").timestamp()
    except ValueError:
        return 0.0


def server_time(world_dir):
    """Текущее «серверное время» (секунды) из Data\\game\\settings.json.

    К нему привязаны ``user.lastTimeGame`` / ``timeResearchTech`` / ``timeBan`` /
    ``timeAddRating`` в файлах игрока.
    """
    try:
        return float(_read_json(os.path.join(world_dir, "Data", "game", "settings.json")).get("serverTime") or 0.0)
    except (TypeError, ValueError):
        return 0.0


_REF_CACHE = {}  # (world_dir, fname) -> (mtime, {key: val})


def _load_ref(world_dir, fname, key_field, val_field):
    """id->name из справочника игры (Data\\<fname>), кэш по mtime."""
    path = os.path.join(world_dir, "Data", fname)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    ck = (world_dir, fname)
    hit = _REF_CACHE.get(ck)
    if hit and hit[0] == mt:
        return hit[1]
    data = _read_json(path)
    out = {}
    for it in data.get("items", []):
        if key_field in it:
            out[it[key_field]] = it.get(val_field)
    _REF_CACHE[ck] = (mt, out)
    return out


def load_items(world_dir):
    """{item_id(int): name}."""
    return _load_ref(world_dir, "items.json", "id", "name")


def load_abilities(world_dir):
    """{ability_uid(int): id(str)}."""
    return _load_ref(world_dir, "ability.json", "uid", "id")


def _name_inv(items, item_names):
    out = []
    for it in items or []:
        tid = it.get("type")
        out.append({
            "id": tid,
            "name": item_names.get(tid) or ("item %s" % tid),
            "count": it.get("count"),
            "durability": round(it.get("durability"), 1) if it.get("durability") else None,
        })
    return out


def load_friends(world_dir):
    """{user_id: [{id, accesses}]} из Data\\game\\friends.json (accesses = число выданных прав)."""
    data = _read_json(os.path.join(world_dir, "Data", "game", "friends.json"))
    out = {}
    for fl in data.get("friendLists", []):
        try:
            uid = int(fl["userId"])
        except (KeyError, TypeError, ValueError):
            continue
        out[uid] = [{"id": f.get("userId"),
                     "accesses": sum(1 for a in (f.get("accesses") or []) if a.get("isAccess"))}
                    for f in (fl.get("friends") or [])]
    return out


def load_clans(world_dir):
    """{clan_id: {name, rating, clan_point, max_users, members:[{id, role, rating, clan_point}]}}."""
    data = _read_json(os.path.join(world_dir, "Data", "game", "clans.json"))
    out = {}
    for c in data.get("clans", []):
        try:
            cid = int(c["id"])
        except (KeyError, TypeError, ValueError):
            continue
        out[cid] = {
            "name": c.get("name") or ("clan %d" % cid),
            "rating": c.get("rating"),
            "clan_point": c.get("clanPoint"),
            "max_users": c.get("maxUserCount"),
            "members": [{"id": u.get("userId"), "role": u.get("role", 0),
                        "rating": u.get("rating"), "clan_point": u.get("clanPoint")}
                       for u in c.get("users", [])],
        }
    return out


def _user_sessions(analytics_path, uid):
    """Полная история сессий одного игрока из analytics.txt.

    -> {total, total_secs, avg_secs, max_secs, first_seen, last_enter, online,
        by_hour:[24], recent:[{enter, exit, secs}]}
    """
    enters = []          # незакрытые enter'ы (стек)
    sessions = []        # {enter, exit, secs}
    by_hour = [0] * 24
    first_seen = None
    last_enter = None
    online = False
    for ln in _read_text(analytics_path).splitlines():
        m = _LINE_RX.match(ln)
        if not m:
            continue
        ts, kind, u, extra = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if u != uid:
            continue
        if first_seen is None:
            first_seen = ts
        if kind == "enter":
            enters.append(ts)
            last_enter = ts
            online = True
            try:
                by_hour[int(ts.split()[1].split(":")[0])] += 1
            except (IndexError, ValueError):
                pass
        elif kind == "exit":
            secs = int(extra) if extra else 0
            en = enters.pop() if enters else None
            sessions.append({"enter": en, "exit": ts, "secs": secs})
            online = False
    closed = [s["secs"] for s in sessions if s["secs"]]
    return {
        "total": len(sessions) + len(enters),
        "total_secs": sum(closed),
        "avg_secs": (sum(closed) // len(closed)) if closed else 0,
        "max_secs": max(closed) if closed else 0,
        "first_seen": first_seen,
        "last_enter": last_enter,
        "online": online,
        "by_hour": by_hour,
        "recent": sessions[-15:][::-1],
    }


ROLE_NAMES = {0: "player", 1: "moderator", 2: "admin", 3: "GM"}

_CHAT_TS_RX = re.compile(r"^(\d{1,2}\.\d{1,2}\.\d{4} \d{1,2}:\d{2}:\d{2}): (.+)$")
_PRIV_RX = re.compile(r"^(\d{1,2}\.\d{1,2}\.\d{4} \d{1,2}:\d{2}:\d{2}): (.+?) > (.+?): (.*)$")
_NETIP_RX = re.compile(r"^(\d{1,2}\.\d{1,2}\.\d{4} \d{1,2}:\d{2}:\d{2}): (.+?) = (\S+) = \S+ = (\d+)\s*$")
_DEAD_RX = re.compile(r"^(\d{1,2}\.\d{1,2}\.\d{4} \d{1,2}:\d{2}:\d{2}): user = (.+?); (\w+)\s*$")
_LAND_RX = re.compile(r"^(\d{1,2}\.\d{1,2}\.\d{4} \d{1,2}:\d{2}:\d{2}): user=(\d+) map=(\d+) p=(\d+),(\d+)")
_ROLE_RX = re.compile(r"set role user (\d+)\[(.+?)\] admin=(\d+)\[(.+?)\] role=(\w+)")
_REWARD_RX = re.compile(r"^(\d{1,2}\.\d{1,2}\.\d{4} \d{1,2}:\d{2}:\d{2}): user=(\d+) ; reward=(\d+)")

_CHAT_CACHE = {}  # world_dir -> (mtimes_tuple, [ {ts, epoch, channel, nick, text} ])
_CHAT_CHANNELS = {0: "global", 1: "global2", 2: "ru", 3: "clan"}


def _all_chat(world_dir):
    """Все строки chat_0..3.txt: [{ts, epoch, channel, nick, text}] (кэш по mtime)."""
    paths = [(i, os.path.join(world_dir, "Logs", "chat_%d.txt" % i)) for i in range(4)]
    mts = tuple(os.path.getmtime(p) if os.path.isfile(p) else 0 for _, p in paths)
    hit = _CHAT_CACHE.get(world_dir)
    if hit and hit[0] == mts:
        return hit[1]
    rows = []
    for ch, p in paths:
        for ln in _read_text(p).splitlines():
            m = _CHAT_TS_RX.match(ln)
            if not m:
                continue
            rest = m.group(2)
            if ": " not in rest:
                continue
            nick, text = rest.split(": ", 1)
            rows.append({"ts": m.group(1), "epoch": _to_epoch(m.group(1)),
                         "channel": _CHAT_CHANNELS.get(ch, str(ch)), "nick": nick, "text": text})
    rows.sort(key=lambda r: r["epoch"])
    _CHAT_CACHE[world_dir] = (mts, rows)
    return rows


def _nick2id(world_dir):
    out = {}
    for i, n in load_user_list(world_dir).items():
        out.setdefault(n, i)
    return out


def server_chat(cfg, limit=200, channel=None, q=None):
    """Публичный чат сервера (chat_0..3) — все каналы, фильтр по каналу и подстроке."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    n2i = _nick2id(world_dir)
    rows = _all_chat(world_dir)
    if channel and channel not in ("all", ""):
        rows = [r for r in rows if r["channel"] == channel]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["text"].lower() or ql in r["nick"].lower()]
    try:
        limit = max(1, min(2000, int(limit)))
    except (TypeError, ValueError):
        limit = 200
    msgs = [{"ts": r["ts"], "channel": r["channel"], "nick": r["nick"],
             "id": n2i.get(r["nick"]), "text": r["text"]} for r in rows[-limit:][::-1]]
    return {"ok": True, "total": len(rows), "channels": list(_CHAT_CHANNELS.values()),
            "messages": msgs}


def server_private_chat(cfg, limit=300, q=None):
    """Все приватные сообщения сервера (chat_privat.txt). Чувствительно — под админ-паролем."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    n2i = _nick2id(world_dir)
    rows = []
    for ln in _read_text(os.path.join(world_dir, "Logs", "chat_privat.txt")).splitlines():
        m = _PRIV_RX.match(ln)
        if not m:
            continue
        frm, to, txt = m.group(2), m.group(3), m.group(4)
        if q and q.lower() not in (frm + to + txt).lower():
            continue
        rows.append({"ts": m.group(1), "from": frm, "to": to, "text": txt,
                     "from_id": n2i.get(frm), "to_id": n2i.get(to)})
    try:
        limit = max(1, min(3000, int(limit)))
    except (TypeError, ValueError):
        limit = 300
    return {"ok": True, "total": len(rows), "messages": rows[-limit:][::-1]}


def server_events(cfg, limit=250, kinds=None):
    """Сводная лента событий сервера: входы/выходы/регистрации/смерти/снос земель.

    Смены ролей (`user_role.txt`) без таймстампа — отдаются отдельным списком.
    """
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    names = load_user_list(world_dir)
    n2i = _nick2id(world_dir)
    want = set(kinds) if kinds else None
    ev = []

    def add(ts, kind, aid, actor, detail=""):
        if want and kind not in want:
            return
        ev.append({"ts": ts, "epoch": _to_epoch(ts), "kind": kind,
                   "id": aid, "actor": actor, "detail": detail})

    for ln in _read_text(os.path.join(world_dir, "analytics.txt")).splitlines():
        m = _LINE_RX.match(ln)
        if not m:
            continue
        ts, k, uid, extra = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        nm = names.get(uid, "id %d" % uid)
        if k == "enter":
            add(ts, "join", uid, nm)
        elif k == "exit":
            add(ts, "leave", uid, nm, _fmt_secs(extra))
        elif k == "register":
            add(ts, "register", uid, nm)

    for ln in _read_text(os.path.join(world_dir, "Logs", "dead_user.txt")).splitlines():
        m = _DEAD_RX.match(ln)
        if m:
            add(m.group(1), "death", n2i.get(m.group(2)), m.group(2), m.group(3))

    logs = os.path.join(world_dir, "Logs")
    try:
        for f in os.listdir(logs):
            if f.startswith("delete_land") and f.endswith(".txt"):
                for ln in _read_text(os.path.join(logs, f)).splitlines():
                    m = _LAND_RX.match(ln)
                    if m:
                        uid = int(m.group(2))
                        add(m.group(1), "land", uid, names.get(uid, "id %d" % uid),
                            "map %s @ %s,%s" % (m.group(3), m.group(4), m.group(5)))
    except OSError:
        pass

    ev.sort(key=lambda e: e["epoch"])
    roles = []
    for ln in _read_text(os.path.join(world_dir, "Logs", "user_role.txt")).splitlines():
        m = _ROLE_RX.search(ln)
        if m:
            roles.append({"target_id": int(m.group(1)), "target": m.group(2),
                          "by_id": int(m.group(3)), "by": m.group(4), "role": m.group(5)})
    try:
        limit = max(1, min(3000, int(limit)))
    except (TypeError, ValueError):
        limit = 250
    return {"ok": True, "total": len(ev), "events": ev[-limit:][::-1], "role_grants": roles}


def _fmt_secs(s):
    try:
        s = int(s)
    except (TypeError, ValueError):
        return ""
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return ("%dч %dм" % (h, m)) if h else ("%dм %dс" % (m, s)) if m else ("%dс" % s)


def player_chat(cfg, uid, limit=60):
    """Публичные сообщения игрока (chat_0..3) — новые сверху."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    names = load_user_list(world_dir)
    try:
        nick = names.get(int(uid))
    except (TypeError, ValueError):
        nick = None
    if not nick:
        return {"ok": False, "error": "игрок не найден"}
    msgs = [{"ts": r["ts"], "channel": r["channel"], "text": r["text"]}
            for r in _all_chat(world_dir) if r["nick"] == nick]
    return {"ok": True, "nick": nick, "count": len(msgs), "messages": msgs[-limit:][::-1]}


def _activity(world_dir, uid, nick):
    logs = os.path.join(world_dir, "Logs")
    deaths = []
    for ln in _read_text(os.path.join(logs, "dead_user.txt")).splitlines():
        m = _DEAD_RX.match(ln)
        if m and m.group(2) == nick:
            deaths.append({"ts": m.group(1), "event": m.group(3)})
    roles = []
    for ln in _read_text(os.path.join(logs, "user_role.txt")).splitlines():
        m = _ROLE_RX.search(ln)
        if not m:
            continue
        tgt, tgt_nm, by, by_nm, rname = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4), m.group(5)
        if tgt == uid or by == uid:
            roles.append({"target_id": tgt, "target": tgt_nm, "by_id": by, "by": by_nm,
                          "role": rname, "as_target": tgt == uid})
    lands = []
    try:
        for f in os.listdir(logs):
            if f.startswith("delete_land") and f.endswith(".txt"):
                for ln in _read_text(os.path.join(logs, f)).splitlines():
                    m = _LAND_RX.match(ln)
                    if m and int(m.group(2)) == uid:
                        lands.append({"ts": m.group(1), "map": int(m.group(3)),
                                      "x": int(m.group(4)), "y": int(m.group(5))})
    except OSError:
        pass
    lands.sort(key=lambda x: _to_epoch(x["ts"]))
    rewards = []
    for ln in _read_text(os.path.join(logs, "reward_order.txt")).splitlines():
        m = _REWARD_RX.match(ln)
        if m and int(m.group(2)) == uid:
            rewards.append({"ts": m.group(1), "reward": int(m.group(3))})
    return {
        "deaths": deaths[-20:][::-1],
        "role_grants": roles,
        "land_deletions": lands[-20:][::-1],
        "rewards": rewards[::-1],
    }


def twink_report(cfg, min_accounts=2):
    """Твинк-детект: какие аккаунты подключались с одного IP (из Logs\\log_net_ip.txt).

    Чувствительно (IP + связывание аккаунтов) — вызывается только эндпоинтом,
    проверившим админ-пароль. IP из ``players.twink_ignore_ips`` пропускаются
    (напр. локальный релей ``127.0.0.2``).
    """
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    ignore = set((_cfg_pl(cfg).get("twink_ignore_ips") or []))
    names = load_user_list(world_dir)
    nick2id = {}
    for i, n in names.items():
        nick2id.setdefault(n, i)

    by_ip = {}          # ip -> {nick: {count, first, last}}
    by_nick_ips = {}     # nick -> set(ip)
    total = 0
    for ln in _read_text(os.path.join(world_dir, "Logs", "log_net_ip.txt")).splitlines():
        m = _NETIP_RX.match(ln)
        if not m:
            continue
        ts, nick, ip = m.group(1), m.group(2), m.group(3)
        if ip in ignore:
            continue
        total += 1
        e = by_ip.setdefault(ip, {}).setdefault(nick, {"count": 0, "first": ts, "last": ts})
        e["count"] += 1
        e["last"] = ts
        by_nick_ips.setdefault(nick, set()).add(ip)

    groups = []
    for ip, nicks in by_ip.items():
        if len(nicks) < min_accounts:
            continue
        accs = [{
            "id": nick2id.get(nk), "name": nk, "connects": v["count"],
            "first_seen": v["first"], "last_seen": v["last"],
            "other_ips": sorted(by_nick_ips.get(nk, set()) - {ip}),
        } for nk, v in nicks.items()]
        accs.sort(key=lambda a: -a["connects"])
        groups.append({"ip": ip, "count": len(nicks), "accounts": accs})
    groups.sort(key=lambda g: -g["count"])
    return {
        "ok": True,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "min_accounts": min_accounts,
        "ignored": sorted(ignore),
        "records": total,
        "ip_count": len(by_ip),
        "flagged_ips": len(groups),
        "groups": groups[:300],
    }


def player_sensitive(cfg, uid):
    """Приватные сообщения игрока + история IP. ТОЛЬКО после проверки админ-пароля
    вызывающим эндпоинтом. Аудит — там же."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    names = load_user_list(world_dir)
    try:
        nick = names.get(int(uid))
        uid = int(uid)
    except (TypeError, ValueError):
        nick = None
    if not nick:
        return {"ok": False, "error": "игрок не найден"}
    logs = os.path.join(world_dir, "Logs")

    priv = []
    for ln in _read_text(os.path.join(logs, "chat_privat.txt")).splitlines():
        m = _PRIV_RX.match(ln)
        if not m:
            continue
        frm, to = m.group(2), m.group(3)
        if frm == nick or to == nick:
            priv.append({"ts": m.group(1), "from": frm, "to": to, "text": m.group(4),
                         "outgoing": frm == nick})

    ips = []
    seen = set()
    for ln in _read_text(os.path.join(logs, "log_net_ip.txt")).splitlines():
        m = _NETIP_RX.match(ln)
        if m and m.group(2) == nick:
            key = m.group(3)
            ips.append({"ts": m.group(1), "ip": m.group(3), "port": int(m.group(4)), "new": key not in seen})
            seen.add(key)
    return {"ok": True, "nick": nick, "private": priv[-200:][::-1],
            "ips": ips[-40:][::-1], "distinct_ips": sorted(seen)}


def player_detail(cfg, uid):
    """Полная карточка игрока (пароль ``code`` НЕ включается — см. ``player_code``)."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден", "root": localserver_root(cfg)}
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad id"}

    names = load_user_list(world_dir)
    uf = os.path.join(world_dir, "Data", "users", "user%d.json" % uid)
    raw = _read_json(uf)
    if not raw and uid not in names:
        return {"ok": False, "error": "игрок %d не найден" % uid}
    raw.pop("code", None)

    st = server_time(world_dir)
    sess = _user_sessions(os.path.join(world_dir, "analytics.txt"), uid)

    unit = {}
    if raw.get("unitId") is not None:
        unit = _read_json(os.path.join(world_dir, "Data", "units", "unit%s.json" % raw["unitId"]))

    clans = load_clans(world_dir)
    clan = clans.get(raw.get("clanId") or 0)
    clan_role = None
    if clan:
        for mm in clan["members"]:
            if mm["id"] == uid:
                clan_role = mm["role"]
                break
    friends = [{"id": f["id"], "name": names.get(f["id"]) or ("id %s" % f["id"]),
                "accesses": f["accesses"]}
               for f in load_friends(world_dir).get(uid, [])]

    def _rem_min(t):
        return round((float(t) - st) / 60.0, 1) if (t and st and float(t) > st) else None

    tb = float(raw.get("timeBan") or 0)
    pos = (unit.get("pos") or {})
    resp = (unit.get("respawnPoint") or {})
    item_names = load_items(world_dir)
    abil_names = load_abilities(world_dir)
    inv_u = (raw.get("Inventory") or {}).get("items", []) or []
    inv_a = (unit.get("Inventory") or {}).get("items", []) or []

    return {
        "ok": True,
        "id": uid,
        "name": names.get(uid) or raw.get("name") or ("id %d" % uid),
        "online": sess["online"],
        "role": raw.get("role", 0),
        "profile": {
            "level": raw.get("unitLevel"),
            "country": raw.get("country") or "",
            "video_card": raw.get("videoCard") or "",
            "screen": raw.get("screenSize") or None,
            "clan_id": raw.get("clanId") or 0,
            "clan_name": clan["name"] if clan else "",
            "clan_role": clan_role,
            "clan_point": next((m["clan_point"] for m in (clan["members"] if clan else []) if m["id"] == uid), None),
            "rating": raw.get("addRating"),
            "playtime_h": round(float(raw.get("timeGame") or 0) / 3600.0, 1),
            "first_seen": sess["first_seen"],
            "last_session_ago_h": round((st - float(raw.get("lastTimeGame") or 0)) / 3600.0, 1)
                                  if (st and raw.get("lastTimeGame")) else None,
            "banned": bool(raw.get("isBlock")) or tb > st > 0,
            "ban_expires_in_h": round((tb - st) / 3600.0, 1) if tb > st > 0 else None,
        },
        "research": {
            "current": raw.get("researchTech") or "",
            "remaining_min": _rem_min(raw.get("timeResearchTech")),
            "done_count": len(raw.get("techList") or []),
            "tech_list": raw.get("techList") or [],
            "booster": raw.get("techBooster"),
        },
        "missions": {"current": raw.get("currentMission"), "month": raw.get("missionMonth")},
        "position": {
            "map": raw.get("mapId"),
            "x": pos.get("x"), "y": pos.get("y"),
            "respawn": {"map": resp.get("mapId"), "x": (resp.get("pos") or {}).get("x"),
                        "y": (resp.get("pos") or {}).get("y")} if resp else None,
            "territories": [{"map": t.get("mapId"), "x": (t.get("pos") or {}).get("x"),
                             "y": (t.get("pos") or {}).get("y")} for t in (raw.get("userTerritories") or [])],
        },
        "avatar": {
            "species": unit.get("speciesId"),
            "gender": unit.get("gender"),
            "is_grown": unit.get("isGrown"),
            "params": [{"type": p.get("type"), "val": p.get("val"), "max": p.get("valMax")}
                       for p in (unit.get("paramList") or [])],
            "long_params": [{"type": p.get("type"), "val": p.get("val")}
                            for p in (unit.get("paramLongList") or [])],
            "skills": [{"type": s.get("type"), "val": s.get("val")} for s in (unit.get("skillLevels") or [])],
            "abilities": [abil_names.get(a) or ("#%s" % a) for a in (unit.get("ability") or [])],
            "buffs": len(unit.get("buffs") or []),
            "stash_count": len(inv_u),
            "carry_count": len(inv_a),
            "stash": _name_inv(inv_u, item_names),
            "carry": _name_inv(inv_a, item_names),
        },
        "sessions": {
            "total": sess["total"],
            "total_h": round(sess["total_secs"] / 3600.0, 1),
            "avg_min": round(sess["avg_secs"] / 60.0, 1),
            "max_min": round(sess["max_secs"] / 60.0, 1),
            "by_hour": sess["by_hour"],
            "recent": sess["recent"],
        },
        "clan": {"name": clan["name"], "rating": clan["rating"], "clan_point": clan["clan_point"],
                 "max_users": clan["max_users"]} if clan else None,
        "clan_members": [{"id": m["id"], "name": names.get(m["id"]) or ("id %s" % m["id"]),
                          "role": m["role"], "rating": m["rating"], "clan_point": m["clan_point"]}
                         for m in (clan["members"] if clan else [])],
        "friends": friends,
        "activity": _activity(world_dir, uid, names.get(uid) or raw.get("name") or ""),
    }


# --------------------------------------------------------------- правка инвентаря
LIFE_FACTOR = 90000  # life в записи ≈ items.json.life × 90000 (проверено на tech_booster/silver_coin)
_ITEMS_FULL_CACHE = {}  # world_dir -> (mtime, {id: def}, {name: def})


def _items_full(world_dir):
    path = os.path.join(world_dir, "Data", "items.json")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}, {}
    hit = _ITEMS_FULL_CACHE.get(world_dir)
    if hit and hit[0] == mt:
        return hit[1], hit[2]
    full = _read_json(path).get("items", [])
    by_id = {it["id"]: it for it in full if "id" in it}
    by_name = {it["name"]: it for it in full if it.get("name")}
    _ITEMS_FULL_CACHE[world_dir] = (mt, by_id, by_name)
    return by_id, by_name


def item_catalog(cfg):
    """[{id, name, stack}] — все предметы игры, для выбора при выдаче."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    by_id, _ = _items_full(world_dir)
    out = [{"id": i, "name": d.get("name"), "stack": d.get("stack", 1)}
           for i, d in by_id.items()]
    out.sort(key=lambda x: (x["name"] or ""))
    return {"ok": True, "items": out}


def _resolve_item(world_dir, item):
    by_id, by_name = _items_full(world_dir)
    s = str(item).strip()
    if s.isdigit() and int(s) in by_id:
        return int(s), by_id[int(s)]
    if s in by_name:
        return by_name[s]["id"], by_name[s]
    return None, None


def _last_event(world_dir, uid):
    last = None
    for ln in _read_text(os.path.join(world_dir, "analytics.txt")).splitlines():
        m = _LINE_RX.match(ln)
        if m and int(m.group(3)) == uid:
            last = m.group(2)
    return last  # "enter" | "exit" | "register" | None


def _is_offline(world_dir, uid):
    return _last_event(world_dir, uid) != "enter"


def _game_edit_backup(cfg, path):
    base = cfg.get("base_dir", os.path.dirname(os.path.abspath(__file__)))
    dst_dir = os.path.join(base, "logs", "game_edits", datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, os.path.basename(path))
    shutil.copy2(path, dst)
    return dst


def _write_json_compact(path, data, expect_mtime):
    """Атомарная запись компактного JSON (как пишет игра). Если mtime изменился
    между чтением и записью — отмена."""
    try:
        cur = os.path.getmtime(path)
    except OSError as e:
        return False, "файл исчез: %s" % e
    if expect_mtime is not None and abs(cur - expect_mtime) > 0.002:
        return False, "файл изменился между чтением и записью — отмена (игрок мог зайти)"
    tmp = path + ".swtmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)
    return True, None


def _user_file(world_dir, uid):
    return os.path.join(world_dir, "Data", "users", "user%d.json" % uid)


def _inv_container(world_dir, uid, where):
    """-> (path, mtime, root_obj, inv_dict) для 'stash' (файл игрока) или 'carry'
    (файл его юнита). Или (None, None, None, err_str)."""
    uf = _user_file(world_dir, uid)
    if not os.path.isfile(uf):
        return None, None, None, "файл игрока не найден"
    if where == "stash":
        mt = os.path.getmtime(uf)
        root = _read_json(uf)
        return uf, mt, root, root.setdefault("Inventory", {})
    # carry -> unit file
    root_u = _read_json(uf)
    unit_id = root_u.get("unitId")
    if unit_id is None:
        return None, None, None, "у игрока нет юнита"
    pf = os.path.join(world_dir, "Data", "units", "unit%s.json" % unit_id)
    if not os.path.isfile(pf):
        return None, None, None, "файл юнита не найден"
    mt = os.path.getmtime(pf)
    root = _read_json(pf)
    return pf, mt, root, root.setdefault("Inventory", {})


def give_stash_items(cfg, uid, item, count):
    """Добавить предмет на СКЛАД игрока (только оффлайн). -> результат-словарь."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    try:
        uid, count = int(uid), int(count)
    except (TypeError, ValueError):
        return {"ok": False, "error": "неверные параметры"}
    if not (1 <= count <= 1_000_000_000):
        return {"ok": False, "error": "count должен быть 1..1e9"}
    item_id, idef = _resolve_item(world_dir, item)
    if item_id is None:
        return {"ok": False, "error": "предмет не найден: %r" % item}
    if not _is_offline(world_dir, uid):
        return {"ok": False, "error": "игрок сейчас онлайн — правка склада только для оффлайн"}

    path, mt, root, inv = _inv_container(world_dir, uid, "stash")
    if inv is None:
        return {"ok": False, "error": root}  # root == err-строка
    items = inv.setdefault("items", [])
    stack = max(1, int(idef.get("stack") or 1))
    same = next((e for e in items if e.get("type") == item_id), None)
    if same:
        life, dur, ext = same.get("life", 0), same.get("durability", 0.0), same.get("extData", 0)
    else:
        life = round(float(idef.get("life") or 0) * LIFE_FACTOR)
        dur = float(idef.get("durability") or 0) if stack == 1 else 0.0
        ext = 0

    remaining, new_entries = count, 0
    for e in items:  # сперва долить неполные стеки того же типа
        if remaining <= 0:
            break
        if e.get("type") == item_id and e.get("extData", 0) == ext:
            room = stack - int(e.get("count") or 0)
            if room > 0:
                add = min(room, remaining)
                e["count"] = int(e.get("count") or 0) + add
                remaining -= add
    while remaining > 0:
        add = min(stack, remaining)
        items.append({"type": item_id, "count": add, "durability": dur,
                      "extData": ext, "life": life, "ext": None})
        remaining -= add
        new_entries += 1

    if not _is_offline(world_dir, uid):  # финальная проверка перед записью
        return {"ok": False, "error": "игрок зашёл в игру — запись отменена"}
    bak = _game_edit_backup(cfg, path)
    ok, err = _write_json_compact(path, root, mt)
    if not ok:
        return {"ok": False, "error": err}
    names = load_items(world_dir)
    return {"ok": True, "op": "give", "where": "stash", "item": item_id,
            "name": names.get(item_id), "count": count, "new_entries": new_entries,
            "backup": os.path.basename(os.path.dirname(bak)),
            "stash": _name_inv(items, names)}


def take_items(cfg, uid, where, item, count):
    """Изъять предмет из склада ('stash') или инвентаря при себе ('carry'). Только оффлайн."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    if where not in ("stash", "carry"):
        return {"ok": False, "error": "where: stash|carry"}
    try:
        uid, count = int(uid), int(count)
    except (TypeError, ValueError):
        return {"ok": False, "error": "неверные параметры"}
    if count < 1:
        return {"ok": False, "error": "count >= 1"}
    item_id, _idef = _resolve_item(world_dir, item)
    if item_id is None:
        try:
            item_id = int(str(item))
        except (TypeError, ValueError):
            return {"ok": False, "error": "предмет не найден: %r" % item}
    if not _is_offline(world_dir, uid):
        return {"ok": False, "error": "игрок сейчас онлайн — изъятие только для оффлайн"}

    path, mt, root, inv = _inv_container(world_dir, uid, where)
    if inv is None:
        return {"ok": False, "error": root}
    items = inv.get("items") or []
    have = sum(int(e.get("count") or 0) for e in items if e.get("type") == item_id)
    if have == 0:
        return {"ok": False, "error": "у игрока нет этого предмета в %s" % where}
    to_remove = min(count, have)
    left, out = to_remove, []
    for e in items:
        if e.get("type") == item_id and left > 0:
            c = int(e.get("count") or 0)
            if c <= left:
                left -= c
                continue
            e["count"] = c - left
            left = 0
        out.append(e)
    inv["items"] = out

    if not _is_offline(world_dir, uid):
        return {"ok": False, "error": "игрок зашёл в игру — запись отменена"}
    bak = _game_edit_backup(cfg, path)
    ok, err = _write_json_compact(path, root, mt)
    if not ok:
        return {"ok": False, "error": err}
    names = load_items(world_dir)
    return {"ok": True, "op": "take", "where": where, "item": item_id,
            "name": names.get(item_id), "removed": to_remove, "had": have,
            "backup": os.path.basename(os.path.dirname(bak)),
            "items": _name_inv(out, names)}


def player_code(cfg, uid):
    """Пароль игрока (``code`` из user<N>.json / ``Code`` из user_list.json).

    ОТДЕЛЬНАЯ функция — вызывается только эндпоинтом, который уже проверил
    админский пароль. В обычную карточку (``player_detail``) code не попадает.
    """
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return None
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return None
    raw = _read_json(os.path.join(world_dir, "Data", "users", "user%d.json" % uid))
    if raw.get("code"):
        return str(raw["code"])
    ul = _read_json(os.path.join(world_dir, "Data", "users", "user_list.json"))
    for u in ul.get("userInfo", []):
        try:
            if int(u["Id"]) == uid:
                return str(u.get("Code") or "") or None
        except (KeyError, TypeError, ValueError):
            continue
    return None


def parse_analytics(path):
    """-> (per_user: {id: {...}}, events: [ {ts, epoch, kind, id, secs} ] в порядке файла)."""
    per = {}
    events = []
    for ln in _read_text(path).splitlines():
        m = _LINE_RX.match(ln)
        if not m:
            continue
        ts, kind, uid, extra = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        ep = _to_epoch(ts)
        events.append({"ts": ts, "epoch": ep, "kind": kind, "id": uid,
                       "secs": int(extra) if extra else None})
        u = per.setdefault(uid, {"id": uid, "online": False, "first_seen": ts,
                                 "last_enter": None, "last_exit": None,
                                 "session_secs": None, "sessions": 0, "last_epoch": 0.0})
        if ep and ep < _to_epoch(u["first_seen"]):
            u["first_seen"] = ts
        if kind == "enter":
            u["online"] = True
            u["last_enter"] = ts
            u["sessions"] += 1
        elif kind == "exit":
            u["online"] = False
            u["last_exit"] = ts
            u["session_secs"] = int(extra) if extra else 0
        u["last_epoch"] = ep
    return per, events


def load_user_list(world_dir):
    """{id(int): name(str)} из user_list.json. Пароли (Code) отбрасываются."""
    p = os.path.join(world_dir, "Data", "users", "user_list.json")
    try:
        data = json.loads(_read_text(p) or "{}")
    except ValueError:
        logging.warning("players: не разобрать %s", p)
        return {}
    out = {}
    for u in data.get("userInfo", []):
        try:
            out[int(u["Id"])] = str(u.get("Name", "")).strip() or ("id %s" % u["Id"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def load_user_details(world_dir):
    """{id(int): {level, role, banned, playtime_h, map, clan, country, unit_id}} из user<N>.json.
    Пароль (code) не читается в выдачу."""
    d = os.path.join(world_dir, "Data", "users")
    out = {}
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for nm in names:
        m = _USER_FILE_RX.match(nm)
        if not m:
            continue
        try:
            raw = json.loads(_read_text(os.path.join(d, nm)) or "{}")
        except ValueError:
            continue
        try:
            uid = int(raw.get("id", m.group(1)))
        except (TypeError, ValueError):
            continue
        out[uid] = {
            "level": raw.get("unitLevel"),
            "role": raw.get("role", 0),
            "banned": bool(raw.get("isBlock")) or float(raw.get("timeBan") or 0) > 0,
            "playtime_h": round(float(raw.get("timeGame") or 0) / 3600.0, 1),
            "map": raw.get("mapId"),
            "clan": raw.get("clanId") or 0,
            "country": raw.get("country") or "",
            "unit_id": raw.get("unitId"),
        }
    return out


def load_unit_positions(world_dir, unit_ids):
    """{unit_id: {x, y, map}} из Data\\units\\unit<id>.json (позиция аватара игрока).

    ``pos`` в файле юнита — координаты на основной карте; когда игрок внутри
    под-локации (``user.mapId`` != ``unit.mapId``), это его последняя позиция на
    основной карте.
    """
    d = os.path.join(world_dir, "Data", "units")
    out = {}
    for uid in unit_ids:
        if uid is None:
            continue
        fp = os.path.join(d, "unit%s.json" % uid)
        if not os.path.isfile(fp):
            continue
        try:
            raw = json.loads(_read_text(fp) or "{}")
        except ValueError:
            continue
        pos = raw.get("pos") or {}
        if "x" in pos and "y" in pos:
            out[uid] = {"x": pos.get("x"), "y": pos.get("y"), "map": raw.get("mapId")}
    return out


def parse_game_state(world_dir):
    """-> [ {map:int, count:int} ] из Logs\\game_state.txt (счётчики онлайна по картам)."""
    txt = _read_text(os.path.join(world_dir, "Logs", "game_state.txt"))
    out, cur = [], None
    for ln in txt.splitlines():
        ln = ln.strip()
        m = re.match(r"^map\s*=\s*(\d+)$", ln)
        if m:
            cur = int(m.group(1))
            continue
        m = re.match(r"^users?\s*count\s*=\s*(\d+)$", ln)
        if m and cur is not None:
            out.append({"map": cur, "count": int(m.group(1))})
            cur = None
    return out


def snapshot(cfg, recent_limit=40):
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False,
                "error": "каталог мира не найден; задайте players.localserver_root / players.world_dir",
                "root": localserver_root(cfg)}

    per, events = parse_analytics(os.path.join(world_dir, "analytics.txt"))
    names = load_user_list(world_dir)
    details = load_user_details(world_dir)
    positions = load_unit_positions(world_dir, {d.get("unit_id") for d in details.values()})
    by_map = parse_game_state(world_dir)

    ids = set(names) | set(per) | set(details)
    users = []
    for uid in sorted(ids):
        a = per.get(uid, {})
        d = details.get(uid, {})
        pos = positions.get(d.get("unit_id")) or {}
        users.append({
            "id": uid,
            "name": names.get(uid) or a.get("name") or ("id %s" % uid),
            "online": bool(a.get("online")),
            "first_seen": a.get("first_seen"),
            "last_enter": a.get("last_enter"),
            "last_exit": a.get("last_exit"),
            "session_secs": a.get("session_secs"),
            "sessions": a.get("sessions", 0),
            "level": d.get("level"),
            "role": d.get("role", 0),
            "banned": bool(d.get("banned")),
            "playtime_h": d.get("playtime_h"),
            "map": d.get("map"),
            "x": pos.get("x"),
            "y": pos.get("y"),
            "clan": d.get("clan", 0),
            "country": d.get("country", ""),
        })

    online_ids = [u["id"] for u in users if u["online"]]
    recent = []
    for e in events[-recent_limit:][::-1]:
        recent.append({"ts": e["ts"], "kind": e["kind"], "id": e["id"],
                       "name": names.get(e["id"], "id %s" % e["id"]), "secs": e["secs"]})

    return {
        "ok": True,
        "world": os.path.basename(world_dir.rstrip("\\/")),
        "world_dir": world_dir,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "totals": {
            "registered": len(names),
            "with_profile": len(details),
            "online_analytics": len(online_ids),
            "online_game_state": sum(x["count"] for x in by_map),
        },
        "by_map": by_map,
        "users": users,
        "recent": recent,
    }
