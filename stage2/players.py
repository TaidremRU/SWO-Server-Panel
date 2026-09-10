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
import collections
import csv
import glob
import io
import json
import logging
import os
import re
import shutil
import time
import zipfile
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


def _online_series(events, bucket_sec=1800, buckets=336):
    """Реконструкция числа онлайн по времени из enter/exit. -> [{t, n}] + peak."""
    now = int(time.time())
    end = now - now % bucket_sec
    start = end - buckets * bucket_sec
    ev = sorted((e for e in events if e.get("epoch")), key=lambda e: e["epoch"])
    online, idx, out, peak = set(), 0, [], 0
    for bi in range(buckets + 1):
        bt = start + bi * bucket_sec
        while idx < len(ev) and ev[idx]["epoch"] <= bt:
            e = ev[idx]
            if e["kind"] == "enter":
                online.add(e["id"])
            elif e["kind"] == "exit":
                online.discard(e["id"])
            idx += 1
        out.append({"t": bt, "n": len(online)})
        peak = max(peak, len(online))
    return out, peak, (out[-1]["n"] if out else 0)


def _day(ts):
    return ts.split(" ", 1)[0] if ts else ""


def stats_bundle(cfg):
    """Сводная аналитика сервера: онлайн-график, регистрации/DAU, retention,
    лидерборды, клан-борд, топ месяца, бан-лист, стафф."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}

    per, events = parse_analytics(os.path.join(world_dir, "analytics.txt"))
    names = load_user_list(world_dir)
    details = load_user_details(world_dir)
    clans = load_clans(world_dir)

    series, peak7, now_online = _online_series(events)

    # регистрации и DAU по дням (последние ~21 день из событий)
    reg_by_day = collections.Counter()
    dau = collections.defaultdict(set)
    reg_day = {}
    active_days = collections.defaultdict(set)  # uid -> {дни}
    for e in events:
        d = _day(e["ts"])
        if not d:
            continue
        if e["kind"] == "register":
            reg_by_day[d] += 1
            reg_day.setdefault(e["id"], d)
        elif e["kind"] == "enter":
            dau[d].add(e["id"])
            active_days[e["id"]].add(d)
    days_sorted = sorted(set(list(reg_by_day) + list(dau)))[-21:]
    reg_series = [{"d": d, "reg": reg_by_day.get(d, 0), "dau": len(dau.get(d, ()))} for d in days_sorted]

    # retention D1 / D7 (когорты последних 30 дней с регистрацией)
    from datetime import timedelta
    d1_hit = d1_tot = d7_hit = d7_tot = 0
    for uid, rd in reg_day.items():
        try:
            base = datetime.strptime(rd, "%d.%m.%Y")
        except ValueError:
            continue
        if (datetime.now() - base).days > 33:
            continue
        ad = active_days.get(uid, set())
        d1 = (base + timedelta(days=1)).strftime("%d.%m.%Y")
        d1_tot += 1
        if d1 in ad:
            d1_hit += 1
        wk = {(base + timedelta(days=k)).strftime("%d.%m.%Y") for k in range(1, 8)}
        d7_tot += 1
        if ad & wk:
            d7_hit += 1

    # лидерборды / распределения из профилей
    us = []
    for uid, dd in details.items():
        us.append({"id": uid, "name": names.get(uid) or ("id %d" % uid),
                   "level": dd.get("level") or 0, "playtime_h": dd.get("playtime_h") or 0,
                   "role": dd.get("role", 0), "banned": bool(dd.get("banned")),
                   "clan": dd.get("clan", 0), "country": dd.get("country") or "?"})
    top_level = sorted(us, key=lambda x: -x["level"])[:20]
    top_time = sorted(us, key=lambda x: -x["playtime_h"])[:20]
    banned = [u for u in us if u["banned"]]
    staff = sorted((u for u in us if u["role"] > 0), key=lambda x: -x["role"])
    lvl_hist = collections.Counter(min(x["level"] // 5 * 5, 60) for x in us)
    country_hist = collections.Counter(x["country"] for x in us).most_common(12)

    # стафф-история
    role_hist = []
    for ln in _read_text(os.path.join(world_dir, "Logs", "user_role.txt")).splitlines():
        m = _ROLE_RX.search(ln)
        if m:
            role_hist.append({"target_id": int(m.group(1)), "target": m.group(2),
                              "by": m.group(4), "role": m.group(5)})

    # клан-борд
    clan_board = sorted(
        [{"id": cid, "name": c["name"], "rating": c["rating"] or 0,
          "clan_point": c["clan_point"], "size": len(c["members"]), "max": c["max_users"],
          "members": [{"id": m["id"], "name": names.get(m["id"]) or ("id %s" % m["id"]),
                       "role": m["role"], "rating": m["rating"]} for m in c["members"]]}
         for cid, c in clans.items()],
        key=lambda x: -(x["rating"] or 0))

    # топ месяца (reward_order.txt + new_month.txt)
    months = []
    cur = None
    for ln in _read_text(os.path.join(world_dir, "Logs", "reward_order.txt")).splitlines():
        if "---" in ln:
            m = re.match(r"^(\d{1,2}\.\d{1,2}\.\d{4})", ln)
            cur = {"date": m.group(1) if m else "?", "rewards": []}
            months.append(cur)
            continue
        m = _REWARD_RX.match(ln)
        if m and cur is not None:
            uid = int(m.group(2))
            cur["rewards"].append({"id": uid, "name": names.get(uid) or ("id %d" % uid),
                                   "reward": int(m.group(3))})

    return {
        "ok": True,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "online": {"series": series, "peak7": peak7, "now": now_online},
        "growth": {"days": reg_series,
                   "retention": {"d1": [d1_hit, d1_tot], "d7": [d7_hit, d7_tot]}},
        "top_level": top_level,
        "top_time": top_time,
        "banned": banned,
        "staff": staff,
        "role_history": role_hist[::-1],
        "level_hist": [{"bucket": b, "n": n} for b, n in sorted(lvl_hist.items())],
        "country_hist": [{"country": c, "n": n} for c, n in country_hist],
        "clan_board": clan_board[:50],
        "months": months[-6:][::-1],
        "totals": {"registered": len(names), "with_profile": len(details),
                   "clans": len(clans), "staff": len(staff), "banned": len(banned)},
    }


_PERF_RX = re.compile(r"phase=(\w+), durationMs=(\d+)")
_READY_RX = re.compile(r"Server ready: startupMs=(\d+), clusters=(\S+), managedMb=(\d+), workingSetMb=(\d+)")
_LAG_RX = re.compile(r"^(\d{1,2}\.\d{1,2}\.\d{4} \d{1,2}:\d{2}:\d{2}): ft?=(\S+) time = ([\d,]+)")
_CONNERR_RX = re.compile(r"id=(\d+) connect=(\w+)")


def server_health(cfg):
    """Здоровье сервера: последний Server ready (startup/память/кластеры),
    лаг-события (медленные тики из time_shedule*), ошибки коннектов."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    logs = os.path.join(world_dir, "Logs")
    names = load_user_list(world_dir)

    # --- world_performance.txt: последний "Server ready" + медленные фазы ---
    perf = _read_text(os.path.join(logs, "world_performance.txt")).splitlines()
    ready, cur_phases, slow_phases = None, [], []
    for ln in perf:
        rm = _READY_RX.search(ln)
        if rm:
            ready = {"startup_ms": int(rm.group(1)), "clusters": rm.group(2),
                     "managed_mb": int(rm.group(3)), "working_set_mb": int(rm.group(4)),
                     "ts": ln[:19]}
            slow_phases = sorted(cur_phases, key=lambda p: -p["ms"])[:6]  # фазы этого запуска
            cur_phases = []
        else:
            pm = _PERF_RX.search(ln)
            if pm:
                cur_phases.append({"phase": pm.group(1), "ms": int(pm.group(2))})

    # --- лаг-события: time_sheduleNet*.txt + shedule/time_schedule*.txt ---
    lag_files = glob.glob(os.path.join(logs, "time_shedule*.txt")) + \
        glob.glob(os.path.join(logs, "shedule", "time_shedule*.txt"))
    by_func = {}
    per_day = collections.Counter()
    recent = []
    total = 0
    for fp in lag_files:
        for ln in _read_text(fp, tail_bytes=600_000).splitlines():
            m = _LAG_RX.match(ln)
            if not m:
                continue
            total += 1
            ts, fn = m.group(1), m.group(2)
            try:
                val = float(m.group(3).replace(",", "."))
            except ValueError:
                continue
            e = by_func.setdefault(fn, {"n": 0, "max": 0.0})
            e["n"] += 1
            e["max"] = max(e["max"], val)
            per_day[_day(ts)] += 1
            recent.append({"ts": ts, "epoch": _to_epoch(ts), "func": fn, "val": round(val, 3)})
    recent.sort(key=lambda x: x["epoch"])
    days = sorted(per_day)[-12:]
    lag = {
        "total": total,
        "per_day": [{"d": d, "n": per_day[d]} for d in days],
        "by_func": sorted(({"func": k, "n": v["n"], "max": round(v["max"], 3)}
                           for k, v in by_func.items()), key=lambda x: -x["n"])[:15],
        "recent": [{k: r[k] for k in ("ts", "func", "val")} for r in recent[-40:][::-1]],
    }

    # --- ошибки коннектов error_game*.txt ---
    cerr_by = collections.Counter()
    cerr_recent = []
    for fp in glob.glob(os.path.join(logs, "error_game*.txt")):
        for ln in _read_text(fp).splitlines():
            m = _CONNERR_RX.search(ln)
            if m:
                uid = int(m.group(1))
                cerr_by[uid] += 1
                cerr_recent.append(uid)
    conn_errors = {
        "total": sum(cerr_by.values()),
        "by_player": sorted(({"id": i, "name": names.get(i) or ("id %d" % i), "n": n}
                             for i, n in cerr_by.items()), key=lambda x: -x["n"])[:20],
        "recent": [{"id": i, "name": names.get(i) or ("id %d" % i)} for i in cerr_recent[-20:][::-1]],
    }

    return {
        "ok": True,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ready": ready,
        "slow_phases": slow_phases,
        "lag": lag,
        "conn_errors": conn_errors,
        "memory_note": "memory_log.txt игрой не заполняется (все значения 0)",
    }


def players_csv(cfg):
    """CSV списка игроков (из snapshot). -> (bytes, filename) | (None, err)."""
    snap = snapshot(cfg)
    if not snap.get("ok"):
        return None, snap.get("error", "нет данных")
    buf = io.StringIO()
    cols = ["id", "name", "online", "level", "role", "banned", "playtime_h",
            "map", "x", "y", "country", "clan", "sessions", "first_seen",
            "last_enter", "last_exit"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for u in snap["users"]:
        w.writerow(u)
    fn = "players_%s_%s.csv" % (snap.get("world", "world"),
                                datetime.now().strftime("%Y%m%d_%H%M%S"))
    return buf.getvalue().encode("utf-8-sig"), fn


_BACKUP_STATE_PARTS = ("analytics.txt", os.path.join("Data", "users"),
                       os.path.join("Data", "units"), os.path.join("Data", "game"), "Logs")


def make_world_backup(cfg, scope="state"):
    """Zip каталога мира. scope: 'state' (users/units/game/logs + analytics — без
    бинарных карт) или 'full' (всё). -> (path, None) | (None, err). Хранит
    последние 5 в base_dir\\logs\\backups\\."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return None, "каталог мира не найден"
    base = cfg.get("base_dir", os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(base, "logs", "backups")
    os.makedirs(out_dir, exist_ok=True)
    wname = os.path.basename(world_dir.rstrip("\\/"))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    zpath = os.path.join(out_dir, "world_%s_%s_%s.zip" % (wname, scope, ts))
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            if scope == "full":
                for root, _dirs, files in os.walk(world_dir):
                    for f in files:
                        fp = os.path.join(root, f)
                        z.write(fp, os.path.relpath(fp, world_dir))
            else:
                for part in _BACKUP_STATE_PARTS:
                    p = os.path.join(world_dir, part)
                    if os.path.isfile(p):
                        z.write(p, part)
                    elif os.path.isdir(p):
                        for root, _dirs, files in os.walk(p):
                            for f in files:
                                fp = os.path.join(root, f)
                                z.write(fp, os.path.relpath(fp, world_dir))
    except OSError as e:
        return None, "не удалось собрать архив: %s" % e
    # оставить последние 5
    try:
        olds = sorted(glob.glob(os.path.join(out_dir, "world_*.zip")), key=os.path.getmtime)
        for old in olds[:-5]:
            os.remove(old)
    except OSError:
        pass
    return zpath, None


def world_map(cfg):
    """Карты и территории: онлайн по картам (game_state), число аватаров по
    `user.mapId`, все `userTerritories` с владельцами. Существа/животные хранятся
    в бинарных ``map*.dt`` — в JSON их нет."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    names = load_user_list(world_dir)
    gs = {x["map"]: x["count"] for x in parse_game_state(world_dir)}
    d = os.path.join(world_dir, "Data", "users")
    terr, avatars_by_map = [], collections.Counter()
    try:
        udir = os.listdir(d)
    except OSError:
        udir = []
    for nm in udir:
        m = _USER_FILE_RX.match(nm)
        if not m:
            continue
        raw = _read_json(os.path.join(d, nm))
        try:
            uid = int(raw.get("id") if raw.get("id") is not None else m.group(1))
        except (TypeError, ValueError):
            continue
        if raw.get("mapId") is not None:
            avatars_by_map[raw["mapId"]] += 1
        for tt in (raw.get("userTerritories") or []):
            p = tt.get("pos") or {}
            terr.append({"map": tt.get("mapId"), "x": p.get("x"), "y": p.get("y"),
                         "owner_id": uid, "owner": names.get(uid) or ("id %d" % uid)})
    terr_by_map = collections.Counter(t["map"] for t in terr)
    maps = sorted(set(list(gs) + list(avatars_by_map) + list(terr_by_map)), key=lambda x: (x is None, x))
    rows = [{"map": mp, "online": gs.get(mp, 0), "avatars": avatars_by_map.get(mp, 0),
             "territories": terr_by_map.get(mp, 0)} for mp in maps]
    rows.sort(key=lambda r: -(r["online"] * 100 + r["territories"]))
    terr.sort(key=lambda t: ((t["map"] if t["map"] is not None else 0), (t["owner"] or "").lower()))
    try:
        uf = os.listdir(os.path.join(world_dir, "Data", "units"))
    except OSError:
        uf = []
    bots = sum(1 for f in uf if f.startswith("bots") and f.endswith(".json"))
    avatars = sum(1 for f in uf if re.match(r"unit\d+\.json$", f))
    return {"ok": True, "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "maps": rows, "territories": terr[:3000],
            "totals": {"avatars": avatars, "bots": bots, "territories": len(terr),
                       "maps": len(rows)}}


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

    ip_groups = []
    for ip, nicks in by_ip.items():
        if len(nicks) < min_accounts:
            continue
        accs = [{
            "id": nick2id.get(nk), "name": nk, "connects": v["count"],
            "first_seen": v["first"], "last_seen": v["last"],
            "other_ips": sorted(by_nick_ips.get(nk, set()) - {ip}),
        } for nk, v in nicks.items()]
        accs.sort(key=lambda a: -a["connects"])
        ip_groups.append({"ip": ip, "count": len(nicks), "accounts": accs})
    ip_groups.sort(key=lambda g: -g["count"])

    # --- по паролю (code) и по «отпечатку» железа из user<N>.json ---
    by_code, by_fp = {}, {}
    d = os.path.join(world_dir, "Data", "users")
    try:
        listing = os.listdir(d)
    except OSError:
        listing = []
    for nm in listing:
        m = _USER_FILE_RX.match(nm)
        if not m:
            continue
        raw = _read_json(os.path.join(d, nm))
        try:
            uid = int(raw.get("id") if raw.get("id") is not None else m.group(1))
        except (TypeError, ValueError):
            continue
        code = raw.get("code")
        if code:
            by_code.setdefault(code, []).append(uid)
        gpu = (raw.get("videoCard") or "").strip()
        ss = raw.get("screenSize") or {}
        scr = ("%sx%s" % (ss.get("x"), ss.get("y"))) if ss else ""
        if gpu or scr:
            by_fp.setdefault((gpu, scr), []).append(uid)

    def _accs(uids):
        return sorted(({"id": u, "name": names.get(u) or ("id %d" % u)} for u in uids),
                      key=lambda a: a["id"])

    code_groups = [{"count": len(v), "hint": "%d симв." % len(c), "accounts": _accs(v)}
                   for c, v in by_code.items() if len(v) >= min_accounts]
    code_groups.sort(key=lambda g: -g["count"])
    fp_groups = [{"count": len(v), "gpu": k[0] or "?", "screen": k[1] or "?", "accounts": _accs(v)}
                 for k, v in by_fp.items() if len(v) >= max(3, min_accounts)]
    fp_groups.sort(key=lambda g: -g["count"])

    return {
        "ok": True,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "min_accounts": min_accounts,
        "ignored": sorted(ignore),
        "records": total,
        "ip_count": len(by_ip),
        "flagged_ips": len(ip_groups),
        "groups": ip_groups[:300],
        "code_groups": code_groups[:300],
        "code_flagged": len(code_groups),
        "code_accounts": sum(g["count"] for g in code_groups),
        "fp_groups": fp_groups[:200],
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


# ------------------------------------------- трекинг техов/бустеров (панель сама)
def _rotate(path, max_bytes):
    try:
        if os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass


def tech_track_scan(cfg, state_path, log_path):
    """Один проход: сравнить текущие techList/techBooster/researchTech всех игроков
    с прошлым снапшотом, дописать изменения в ``log_path`` (jsonl). -> список новых
    событий. У игры своего лога исследований/бустеров нет — панель ведёт его сама.
    """
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return []
    names = load_user_list(world_dir)
    prev_u = (_read_json(state_path) or {}).get("users", {})
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d = os.path.join(world_dir, "Data", "users")
    try:
        listing = os.listdir(d)
    except OSError:
        return []
    new_state, events = {}, []
    for nm in listing:
        m = _USER_FILE_RX.match(nm)
        if not m:
            continue
        raw = _read_json(os.path.join(d, nm))
        try:
            uid = int(raw.get("id") if raw.get("id") is not None else m.group(1))
        except (TypeError, ValueError):
            continue
        techs = list(raw.get("techList") or [])
        cur = {"n": len(techs), "boost": int(raw.get("techBooster") or 0),
               "research": raw.get("researchTech") or ""}
        new_state[str(uid)] = cur
        p = prev_u.get(str(uid))
        if p is None:
            continue  # первое наблюдение — не событие
        who = names.get(uid) or ("id %d" % uid)
        if cur["n"] > p.get("n", 0):
            events.append({"ts": ts, "uid": uid, "name": who, "kind": "tech_gained",
                           "techs": techs[p.get("n", 0):], "count": cur["n"] - p.get("n", 0),
                           "total": cur["n"]})
        db = cur["boost"] - p.get("boost", 0)
        if db < 0:
            events.append({"ts": ts, "uid": uid, "name": who, "kind": "booster_spent",
                           "delta": -db, "left": cur["boost"]})
        elif db > 0:
            events.append({"ts": ts, "uid": uid, "name": who, "kind": "booster_gained",
                           "delta": db, "total": cur["boost"]})
        if cur["research"] and cur["research"] != p.get("research"):
            events.append({"ts": ts, "uid": uid, "name": who, "kind": "research_changed",
                           "to": cur["research"], "from": p.get("research") or ""})

    try:
        tmp = state_path + ".swtmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated": ts, "users": new_state}, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, state_path)
    except OSError:
        logging.exception("tech_track: не удалось записать %s", state_path)
    if events:
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with io.open(log_path, "a", encoding="utf-8") as f:
                for e in events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            _rotate(log_path, 3_000_000)
        except OSError:
            logging.exception("tech_track: не удалось дописать %s", log_path)
    return events


def tech_track_read(cfg, log_path, uid=None, kind=None, limit=400):
    rows = []
    for ln in _read_text(log_path, tail_bytes=1_500_000).splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if uid is not None and e.get("uid") != int(uid):
            continue
        if kind and e.get("kind") != kind:
            continue
        rows.append(e)
    try:
        limit = max(1, min(3000, int(limit)))
    except (TypeError, ValueError):
        limit = 400
    return {"ok": True, "total": len(rows), "events": rows[-limit:][::-1]}


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


# --------------------------------------------------------- модерация игрока (запись)
_ROLE_WORD = {0: "player", 1: "moderator", 2: "admin", 3: "master"}


def _edit_offline_user(cfg, uid, mutate, what):
    """Каркас правки ``user<N>.json`` оффлайн-игрока.

    ``mutate(raw)`` меняет словарь на месте и возвращает dict доп-полей для ответа
    (или ``{"_error": "..."}`` чтобы отменить). Оффлайн проверяется в начале И
    перед записью; бэкап + атомарная запись + guard по mtime.
    """
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return {"ok": False, "error": "неверный id"}
    if not _is_offline(world_dir, uid):
        return {"ok": False, "error": "игрок сейчас онлайн — %s только для оффлайн" % what}
    uf = _user_file(world_dir, uid)
    if not os.path.isfile(uf):
        return {"ok": False, "error": "файл игрока не найден"}
    mt = os.path.getmtime(uf)
    raw = _read_json(uf)
    extra = mutate(raw)
    if isinstance(extra, dict) and extra.get("_error"):
        return {"ok": False, "error": extra["_error"]}
    if not _is_offline(world_dir, uid):
        return {"ok": False, "error": "игрок зашёл в игру — запись отменена"}
    bak = _game_edit_backup(cfg, uf)
    ok, err = _write_json_compact(uf, raw, mt)
    if not ok:
        return {"ok": False, "error": err}
    out = {"ok": True, "id": uid, "backup": os.path.basename(os.path.dirname(bak))}
    if isinstance(extra, dict):
        out.update(extra)
    return out


def player_set_ban(cfg, uid, blocked, hours=0):
    """``isBlock`` + ``timeBan`` (serverTime + hours*3600; hours=0/blocked=False -> 0)."""
    wd = find_world_dir(cfg)
    st = server_time(wd) if wd else 0.0
    try:
        hours = float(hours or 0)
    except (TypeError, ValueError):
        hours = 0.0

    def m(raw):
        raw["isBlock"] = bool(blocked)
        raw["timeBan"] = (st + hours * 3600.0) if (blocked and hours > 0 and st) else 0.0
        return {"isBlock": raw["isBlock"], "timeBan": raw["timeBan"],
                "perm": bool(blocked) and hours <= 0}

    return _edit_offline_user(cfg, uid, m, "бан")


def player_set_role(cfg, uid, role, by_user="panel"):
    try:
        role = int(role)
    except (TypeError, ValueError):
        return {"ok": False, "error": "роль 0..3"}
    if role not in _ROLE_WORD:
        return {"ok": False, "error": "роль 0..3"}
    wd = find_world_dir(cfg)
    names = load_user_list(wd) if wd else {}

    def m(raw):
        raw["role"] = role
        return {"role": role, "role_word": _ROLE_WORD[role]}

    res = _edit_offline_user(cfg, uid, m, "смена роли")
    if res.get("ok") and wd:
        try:
            nick = names.get(int(uid), "id %s" % uid)
            with io.open(os.path.join(wd, "Logs", "user_role.txt"), "a", encoding="utf-8") as f:
                f.write("set role user %s[%s] admin=panel[%s] role=%s\n"
                        % (uid, nick, by_user, _ROLE_WORD[role]))
        except OSError:
            logging.warning("player_set_role: не удалось дописать user_role.txt")
    return res


def player_set_position(cfg, uid, map_id, x, y, also_respawn=False):
    """``unit.pos`` (+ ``unit.mapId`` и ``user.mapId``, если map_id задан) +
    опционально ``unit.respawnPoint``. Оффлайн."""
    wd = find_world_dir(cfg)
    if not wd:
        return {"ok": False, "error": "каталог мира не найден"}
    try:
        uid = int(uid)
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return {"ok": False, "error": "координаты — числа"}
    mid = None
    if str(map_id).strip() not in ("", "None"):
        try:
            mid = int(map_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "карта — число"}
    if not _is_offline(wd, uid):
        return {"ok": False, "error": "игрок сейчас онлайн — телепорт только для оффлайн"}
    uf = _user_file(wd, uid)
    ru = _read_json(uf)
    unit_id = ru.get("unitId")
    if unit_id is None:
        return {"ok": False, "error": "у игрока нет юнита"}
    pf = os.path.join(wd, "Data", "units", "unit%s.json" % unit_id)
    if not os.path.isfile(pf):
        return {"ok": False, "error": "файл юнита не найден"}
    pmt = os.path.getmtime(pf)
    unit = _read_json(pf)
    unit.setdefault("pos", {})["x"] = x
    unit["pos"]["y"] = y
    if mid is not None:
        unit["mapId"] = mid
    if also_respawn:
        unit["respawnPoint"] = {"mapId": unit.get("mapId", 0), "pos": {"x": x, "y": y}}
    if not _is_offline(wd, uid):
        return {"ok": False, "error": "игрок зашёл — отмена"}
    bak = _game_edit_backup(cfg, pf)
    ok, err = _write_json_compact(pf, unit, pmt)
    if not ok:
        return {"ok": False, "error": err}
    if mid is not None:  # держим user.mapId в согласии
        try:
            umt = os.path.getmtime(uf)
            ru2 = _read_json(uf)
            ru2["mapId"] = mid
            _game_edit_backup(cfg, uf)
            _write_json_compact(uf, ru2, umt)
        except OSError:
            pass
    return {"ok": True, "id": uid, "map": mid if mid is not None else unit.get("mapId"),
            "x": x, "y": y, "respawn": also_respawn,
            "backup": os.path.basename(os.path.dirname(bak))}


def player_add_tech(cfg, uid, tech):
    wd = find_world_dir(cfg)
    if not wd:
        return {"ok": False, "error": "каталог мира не найден"}
    valid = {it["id"] for it in _read_json(os.path.join(wd, "Data", "tech.json")).get("items", []) if "id" in it}
    reqs = [x.strip() for x in re.split(r"[\s,]+", str(tech)) if x.strip()]
    if not reqs:
        return {"ok": False, "error": "не указаны техи"}
    bad = [x for x in reqs if x not in valid]
    if bad:
        return {"ok": False, "error": "нет таких техов: %s" % ", ".join(bad[:10])}

    def m(raw):
        cur = raw.setdefault("techList", [])
        added = [x for x in reqs if x not in cur]
        cur.extend(added)
        return {"added": added, "tech_count": len(cur)}

    return _edit_offline_user(cfg, uid, m, "выдача техов")


def player_set_stat(cfg, uid, field, value):
    if field not in ("unitLevel", "addRating"):
        return {"ok": False, "error": "field: unitLevel|addRating"}
    try:
        value = int(value)
    except (TypeError, ValueError):
        return {"ok": False, "error": "значение — число"}
    if not (0 <= value <= 1_000_000_000):
        return {"ok": False, "error": "0..1e9"}

    def m(raw):
        raw[field] = value
        return {field: value}

    return _edit_offline_user(cfg, uid, m, "правка " + field)


def player_reset_code(cfg, uid, newcode):
    newcode = str(newcode or "")
    if not (1 <= len(newcode) <= 64):
        return {"ok": False, "error": "пароль 1..64 символа"}
    wd = find_world_dir(cfg)

    def m(raw):
        raw["code"] = newcode
        return {}

    res = _edit_offline_user(cfg, uid, m, "сброс пароля игрока")
    if res.get("ok") and wd:  # синхронизируем user_list.json Code
        ulp = os.path.join(wd, "Data", "users", "user_list.json")
        try:
            umt = os.path.getmtime(ulp)
            ul = _read_json(ulp)
            for u in ul.get("userInfo", []):
                try:
                    if int(u.get("Id", -1)) == int(uid):
                        u["Code"] = newcode
                except (TypeError, ValueError):
                    pass
            _game_edit_backup(cfg, ulp)
            _write_json_compact(ulp, ul, umt)
        except OSError:
            logging.warning("player_reset_code: user_list.json не обновлён")
    return {"ok": res.get("ok"), "id": res.get("id"), "backup": res.get("backup"),
            "error": res.get("error")}  # сам код в ответ НЕ кладём


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
