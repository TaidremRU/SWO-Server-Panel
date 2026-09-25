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
падения/перезапуска сервера ``exit`` не пишется, и такой игрок обычным способом
«висел» бы онлайн навсегда — поэтому ``parse_analytics()`` сверяет время
последнего ``enter`` с последним ``Server ready`` из ``Logs\\world_performance.txt``
(см. ``_last_restart_epoch``): ``enter`` раньше последнего рестарта сервера в
счёт не идёт, это точно оборванная старым процессом сессия. Плюс рядом всегда
отдаём авторитетную сумму из ``game_state.txt`` (сервер переписывает его живым
снапшотом каждые несколько секунд) — для сверки.

``snapshot(cfg)`` -> dict (см. конец файла). Пароли (``code`` / ``Code``) в выдачу
не попадают ни в каком виде.
"""
import collections
import csv
import glob
import io
import itertools
import json
import logging
import math
import os
import re
import shutil
import struct
import time
import zipfile
from datetime import datetime

try:
    import mapdt
except Exception:  # noqa: BLE001
    mapdt = None

_MACHINE_NAMES = ("not", "furnace", "crusher", "extractor", "distiller", "press")

# Процесс игры-сервера: не запущен — сервер оффлайн, все игроки оффлайн (analytics.txt при остановке
# оставляет «висящие» сессии). Имя exe выставляет webui из config.json (game_exe).
GAME_EXE = "sigmaworld.exe"
_GAME_RUN = [0.0, True]


def game_running():
    """Запущен ли процесс игры (кэш 10 с). Без psutil — считаем запущенным (ничего не ломаем)."""
    now = time.time()
    if now - _GAME_RUN[0] < 10:
        return _GAME_RUN[1]
    try:
        import psutil
        run = any((p.info.get("name") or "").lower() == GAME_EXE for p in psutil.process_iter(["name"]))
    except Exception:  # noqa: BLE001
        run = True
    _GAME_RUN[0], _GAME_RUN[1] = now, run
    return run
_MAPDT_CACHE = {}       # path -> (mtime, summary)
_MAPDT_FIND_CACHE = {}  # (path, frozenset(want)) -> (mtime, result)

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


def online_series_recent(cfg, hours=24):
    """Онлайн-игроки по часам за последние ``hours`` часов (реконструкция из
    analytics.txt, см. ``_online_series``). -> {"ok", "series":[{t,n}], "peak", "now"}
    | {"ok":False,"error"}."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    _, events = parse_analytics(os.path.join(world_dir, "analytics.txt"))
    series, peak, now_online = _online_series(events, bucket_sec=3600, buckets=hours)
    return {"ok": True, "series": series, "peak": peak, "now": now_online}


REWARD_TIERS = (24, 20, 16, 12, 8, 4, 0)  # призовые ускорители исследования по месту 1..7


def rating_top(cfg, top_n=7):
    """Топ сезонного рейтинга (``Data/users/rating.json``) по убыванию
    ``orderRating``. Награда — не поле ``reward`` из файла, а фиксированная
    шкала по месту (``REWARD_TIERS``): 1 место = 24 ускорителя исследования,
    ..., 7 место = 0.
    -> {"ok", "top":[{id, name, reward}]} | {"ok":False,"error"}."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    raw = _read_json(os.path.join(world_dir, "Data", "users", "rating.json"), default={})
    names = load_user_list(world_dir)
    rows = sorted(raw.get("users") or [], key=lambda u: -(u.get("orderRating") or 0))[:top_n]
    top = [{"id": u.get("userId"),
            "name": names.get(u.get("userId")) or ("id %s" % u.get("userId")),
            "reward": REWARD_TIERS[i] if i < len(REWARD_TIERS) else 0}
           for i, u in enumerate(rows)]
    return {"ok": True, "top": top}


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

    # стоимость техов (research-очки; по механике игры 1 очко ≈ 1 час на базовой ставке)
    tech_cost = {}
    for it in _read_json(os.path.join(world_dir, "Data", "tech.json")).get("items", []):
        if "id" in it:
            tech_cost[it["id"]] = it.get("cost")
    tmeta = tech_meta(world_dir)
    _tlabel = lambda tch: (tmeta.get(tch) or {}).get("label") or tch

    # лидерборды / распределения из профилей
    us = []
    for uid, dd in details.items():
        us.append({"id": uid, "name": names.get(uid) or ("id %d" % uid),
                   "level": dd.get("level") or 0, "playtime_h": dd.get("playtime_h") or 0,
                   "role": dd.get("role", 0), "banned": bool(dd.get("banned")),
                   "clan": dd.get("clan", 0), "country": dd.get("country") or "?",
                   "tech_count": len(dd.get("techs") or []),
                   "research_h": round(sum(tech_cost.get(t) or 0 for t in (dd.get("techs") or [])) / 60.0, 1),
                   "research": dd.get("research") or "",
                   "research_name": _tlabel(dd["research"]) if dd.get("research") else ""})
    top_level = sorted(us, key=lambda x: -x["level"])[:20]
    top_time = sorted(us, key=lambda x: -x["playtime_h"])[:20]
    top_tech_players = sorted(us, key=lambda x: -x["tech_count"])[:20]
    total_research_h = round(sum(u["research_h"] for u in us), 1)
    banned = [u for u in us if u["banned"]]
    staff = sorted((u for u in us if u["role"] > 0), key=lambda x: -x["role"])
    lvl_hist = collections.Counter(min(x["level"] // 5 * 5, 60) for x in us)
    country_hist = collections.Counter(x["country"] for x in us).most_common(12)

    # популярность техов: сколько игроков изучили каждый + кто что изучает сейчас
    tcnt, rcnt = collections.Counter(), collections.Counter()
    for dd in details.values():
        for tch in dd.get("techs", []):
            tcnt[tch] += 1
        if dd.get("research"):
            rcnt[dd["research"]] += 1
    def _ch(tch):
        c = tech_cost.get(tch)
        return round(c / 60.0, 1) if c else None

    top_tech = [{"tech": tch, "name": _tlabel(tch), "n": n,
                 "cost": tech_cost.get(tch), "cost_h": _ch(tch)}
                for tch, n in tcnt.most_common(30)]
    researching = [{"tech": tch, "name": _tlabel(tch), "n": n,
                    "cost": tech_cost.get(tch), "cost_h": _ch(tch)}
                   for tch, n in rcnt.most_common(20)]

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
        "top_tech_players": top_tech_players,
        "banned": banned,
        "staff": staff,
        "role_history": role_hist[::-1],
        "level_hist": [{"bucket": b, "n": n} for b, n in sorted(lvl_hist.items())],
        "country_hist": [{"country": c, "n": n} for c, n in country_hist],
        "top_tech": top_tech,
        "researching": researching,
        "total_research_h": total_research_h,
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


_ARH_RX = re.compile(r"^arh(\d+)\.zip$", re.I)


def rotate_world_backups(cfg, keep_recent=48, keep_days=14, dry_run=False, now=None):
    """Ротация архивов, которые игра сама пишет в ``<мир>\\backup\\arhN.zip``
    (раз в ``timeBackupServer`` секунд из Data\\config.json, старые не удаляет).

    Хранится: ``keep_recent`` самых свежих + по одному (последнему за день) за
    ``keep_days`` дней; остальное удаляется. Оставшиеся перенумеровываются подряд
    ``arh0..arhN`` по времени — как бы игра ни выбирала следующий номер (по числу
    файлов, по максимуму или своим счётчиком), она не перезапишет сохранённый
    архив. Вызывать в «тихом окне» между бэкапами (см. webui). -> отчёт."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    bdir = os.path.join(world_dir, "backup")
    now = now or time.time()
    try:
        files = []
        for f in os.listdir(bdir):
            if _ARH_RX.match(f):
                st = os.stat(os.path.join(bdir, f))
                files.append({"name": f, "mtime": st.st_mtime, "size": st.st_size})
    except OSError:
        return {"ok": True, "total": 0, "deleted": 0, "freed_mb": 0, "kept": 0, "renamed": 0}
    files.sort(key=lambda x: -x["mtime"])
    if any(now - x["mtime"] < 120 for x in files):
        return {"ok": False, "error": "игра только что писала архив — пропускаю"}
    keep, days = [], set()
    for i, x in enumerate(files):
        day = datetime.fromtimestamp(x["mtime"]).strftime("%Y-%m-%d")
        if i < keep_recent:
            keep.append(x)
            days.add(day)          # сутки, уже покрытые свежими, второй «дневной» не нужен
        elif now - x["mtime"] <= keep_days * 86400 and day not in days:
            keep.append(x)
            days.add(day)
    kept_names = {x["name"] for x in keep}
    drop = [x for x in files if x["name"] not in kept_names]
    rep = {"ok": True, "total": len(files), "kept": len(keep), "deleted": len(drop),
           "freed_mb": round(sum(x["size"] for x in drop) / 1048576.0, 1),
           "kept_mb": round(sum(x["size"] for x in keep) / 1048576.0, 1), "renamed": 0, "dry_run": bool(dry_run)}
    if dry_run:
        return rep
    for x in drop:
        try:
            os.remove(os.path.join(bdir, x["name"]))
        except OSError as e:
            logging.warning("backup-rotate: не удалить %s: %s", x["name"], e)
    # перенумерация по времени: сначала во временные имена, потом в arh0..arhN
    order = sorted(keep, key=lambda x: x["mtime"])
    if [x["name"].lower() for x in order] != ["arh%d.zip" % i for i in range(len(order))]:
        tmp = []
        for i, x in enumerate(order):
            t = "__rot_%d.zip" % i
            os.rename(os.path.join(bdir, x["name"]), os.path.join(bdir, t))
            tmp.append(t)
        for i, t in enumerate(tmp):
            os.rename(os.path.join(bdir, t), os.path.join(bdir, "arh%d.zip" % i))
        rep["renamed"] = len(tmp)
    return rep


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


_MAPDIM_CACHE = {}


def map_dim(world_dir, map_id):
    """Размер карты (w, h) из 16-байтного заголовка Data\\maps\\map<N>.dt.

    Формат .dt (несжатый): int32 версия(=7) | float ~9.x | int16 width | int16 height |
    далее сетка тайлов + списки координат сущностей (полная расшифровка — отдельный
    reverse-engineering; заголовок читается тривиально).
    """
    key = (world_dir, map_id)
    if key in _MAPDIM_CACHE:
        return _MAPDIM_CACHE[key]
    res = None
    try:
        with open(os.path.join(world_dir, "Data", "maps", "map%d.dt" % int(map_id)), "rb") as f:
            hdr = f.read(16)
        if len(hdr) == 16:
            import struct
            ver = struct.unpack("<i", hdr[0:4])[0]
            w, h = struct.unpack("<hh", hdr[12:16])
            if ver == 7 and 0 < w <= 4096 and 0 < h <= 4096:
                res = {"w": w, "h": h}
    except (OSError, ValueError):
        pass
    _MAPDIM_CACHE[key] = res
    return res


def _load_blocks(world_dir):
    return _load_ref(world_dir, "blocks.json", "id", "name")


def mapdt_summary(cfg, map_id):
    """Разбор бинарной карты Data\\maps\\map<N>.dt (см. mapdt.py) с именами
    блоков/предметов/машин. Кэш по mtime (map1.dt 40 МБ парсится ~12 c)."""
    if mapdt is None:
        return {"ok": False, "error": "модуль mapdt недоступен"}
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    try:
        map_id = int(map_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad id"}
    path = os.path.join(world_dir, "Data", "maps", "map%d.dt" % map_id)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {"ok": False, "error": "нет файла map%d.dt" % map_id}
    hit = _MAPDT_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]

    t0 = time.time()
    d = mapdt.parse(path, world_dir=world_dir, keep_grid=False)
    if d.get("ok"):
        items = load_items(world_dir)
        blocks = _load_blocks(world_dir)
        names = load_user_list(world_dir)
        for row in d.get("blocks_by_type", []):
            row["name"] = _block_label(blocks.get(row["type"])) or ("block#%s" % row["type"])
        for key in ("res_in_blocks", "container_items"):
            for row in d.get(key, []):
                row["name"] = items.get(row["type"]) or ("item#%s" % row["type"])
        for row in d.get("stone_types", []):  # stonePos.type — тот же id, что и у blocks.json
            row["name"] = _block_label(blocks.get(row["type"])) or ("block#%s" % row["type"])
        for row in d.get("machines", []):
            i = row["type"]
            row["name"] = _MACHINE_NAMES[i] if 0 <= i < len(_MACHINE_NAMES) else ("machine#%s" % i)
        for row in d.get("land_owners", []):
            row["name"] = names.get(row["owner"]) or ("id %s" % row["owner"])
        d["map"] = map_id
        d["parse_sec"] = round(time.time() - t0, 2)
        d["file_mb"] = round(mt and os.path.getsize(path) / 1048576.0, 2)
    _MAPDT_CACHE[path] = (mt, d)
    return d


_MAPDT_CONTAINERS_CACHE = {}


def mapdt_containers(cfg, map_id, min_items=1, cap=2000):
    """«Контейнер (x,y) → что лежит» по всей карте map<N>.dt (все контейнеры,
    без привязки к конкретному предмету — в отличие от mapdt_find). Кэш по mtime."""
    if mapdt is None:
        return {"ok": False, "error": "модуль mapdt недоступен"}
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    try:
        map_id = int(map_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad id"}
    path = os.path.join(world_dir, "Data", "maps", "map%d.dt" % map_id)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {"ok": False, "error": "нет файла map%d.dt" % map_id}
    ck = (path, cap)
    hit = _MAPDT_CONTAINERS_CACHE.get(ck)
    if hit and hit[0] == mt:
        d = hit[1]
    else:
        t0 = time.time()
        items = load_items(world_dir)
        d = mapdt.list_containers(path, world_dir=world_dir, item_names=items, cap=cap, min_items=1)
        if d.get("ok"):
            d["map"] = map_id
            d["parse_sec"] = round(time.time() - t0, 2)
        _MAPDT_CONTAINERS_CACHE[ck] = (mt, d)
    if not d.get("ok") or min_items <= 1:
        return d
    out = dict(d)
    out["containers"] = [c for c in d["containers"] if c["total"] >= min_items]
    out["total_spots"] = len(out["containers"])
    return out


def mapdt_index(cfg):
    """Список карт с базовой инфой + отметкой, разобрана ли уже (в кэше)."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    md = os.path.join(world_dir, "Data", "maps")
    out = []
    try:
        files = os.listdir(md)
    except OSError:
        files = []
    for f in files:
        m = re.match(r"map(\d+)\.dt$", f)
        if not m:
            continue
        fp = os.path.join(md, f)
        dim = map_dim(world_dir, int(m.group(1)))
        cached = _MAPDT_CACHE.get(fp)
        out.append({
            "map": int(m.group(1)),
            "size": ("%dx%d" % (dim["w"], dim["h"])) if dim else None,
            "file_mb": round(os.path.getsize(fp) / 1048576.0, 2),
            "parsed": bool(cached and cached[0] == os.path.getmtime(fp)),
        })
    out.sort(key=lambda x: -x["file_mb"])
    return {"ok": True, "maps": out}


_BLOCKCLASS_CACHE = {}   # world_dir -> (mtime, {block_type: cat})
_MAPIMG_CACHE = {}       # (path, scale, claims, owner) -> (mtime, png bytes)
_MAPOWN_CACHE = {}       # path -> (mtime, {w,h,um_w,um_h,grid,names})
_AQUA_BLOCKS = {3, 16}   # кувшинка, водоросли — не считать растениями


def _block_class(world_dir):
    """{block_type(int): 'mtn'|'ore'|'wall'|'floor'|'built'|'plant'|'aqua'} из blocks.json."""
    p = os.path.join(world_dir, "Data", "blocks.json")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return {}
    hit = _BLOCKCLASS_CACHE.get(world_dir)
    if hit and hit[0] == mt:
        return hit[1]
    out = {}
    for b in (_read_json(p) or {}).get("items", []):
        bid = b.get("id")
        if bid is None:
            continue
        if bid in _AQUA_BLOCKS:
            cat = "aqua"
        elif b.get("isMountain") and b.get("isOre"):
            cat = "ore"
        elif b.get("isMountain"):
            cat = "mtn"
        elif b.get("isWall"):
            cat = "wall"
        elif b.get("isFloor"):
            cat = "floor"
        elif (b.get("growthRnd") or 0) > 0:
            cat = "plant"
        elif (b.get("buildTime") or 0) > 0 or b.get("machine") or b.get("container") \
                or b.get("isCar") or b.get("isRocket") or b.get("isBed") or b.get("isEngine"):
            cat = "built"
        else:
            cat = ""
        if cat:
            out[bid] = cat
    _BLOCKCLASS_CACHE[world_dir] = (mt, out)
    return out


def clan_colors(world_dir):
    """{clan_id: (r,g,b)} — стабильный цвет клана (по порядку id) и
    {user_id: clan_id} — для раскраски карты по кланам."""
    clans = sorted(_clans_raw(world_dir), key=lambda c: c.get("id") or 0)
    pal = mapdt._CLAIM_PAL if mapdt else [(200, 80, 80)]
    col, uclan = {}, {}
    for i, c in enumerate(clans):
        col[c.get("id")] = pal[i % len(pal)]
        for u in c.get("users") or []:
            uclan[u.get("userId")] = c.get("id")
    return col, uclan


def map_clans(cfg, map_id):
    """Легенда «карта по кланам»: у каких кланов есть земля на карте, сколько
    блоков 8×8, цвет; + участки без клана. Нужна отрисованная/разобранная карта
    (берёт сетку владения из кэша mapdt_owners)."""
    d = mapdt_owners(cfg, map_id)
    if not d.get("ok"):
        return d
    world_dir = find_world_dir(cfg)
    col, uclan = clan_colors(world_dir)
    names = {c.get("id"): c.get("name") for c in _clans_raw(world_dir)}
    area, owners, free = {}, {}, 0
    for o in d["grid"]:
        if not o:
            continue
        cid = uclan.get(o)
        if cid is None:
            free += 1
            continue
        area[cid] = area.get(cid, 0) + 1
        owners.setdefault(cid, set()).add(o)
    out = [{"id": cid, "name": names.get(cid) or ("clan %s" % cid), "rgb": list(col.get(cid) or (0, 0, 0)),
            "blocks8": n, "owners": len(owners.get(cid) or ())} for cid, n in area.items()]
    out.sort(key=lambda x: -x["blocks8"])
    return {"ok": True, "clans": out, "no_clan_blocks8": free}


def mapdt_image(cfg, map_id, scale=None, claims=True, owner=None, force=False, by_clan=False, shops=False):
    """PNG-картинка карты: вода/суша/горы/природа/постройки + клаймы.
    ``force=True`` игнорирует кэш по mtime и перерисовывает картинку заново
    (кнопка «пересмотреть» — на случай, если файл обновился, а mtime почему-то
    не сдвинулся, напр. на сетевом диске).
    -> ``(png_bytes, filename, meta)`` либо ``({"ok":False,"error":...}, None, None)``."""
    if mapdt is None:
        return {"ok": False, "error": "модуль mapdt недоступен"}, None, None
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}, None, None
    try:
        map_id = int(map_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad id"}, None, None
    path = os.path.join(world_dir, "Data", "maps", "map%d.dt" % map_id)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {"ok": False, "error": "нет файла map%d.dt" % map_id}, None, None
    try:
        owner = int(owner) if owner else 0
    except (TypeError, ValueError):
        owner = 0
    sc = None if scale in (None, "", "auto") else max(1, min(16, int(scale)))
    ck = (path, sc, bool(claims), owner, bool(by_clan), bool(shops))
    hit = _MAPIMG_CACHE.get(ck)
    if not force and hit and hit[0] == mt:
        return hit[1], "map%d.png" % map_id, {"cached": True}
    owner_color = None
    if by_clan:
        col, uclan = clan_colors(world_dir)
        owner_color = {u: col.get(c) for u, c in uclan.items()}
    res = mapdt.render_png(path, world_dir=world_dir, block_class=_block_class(world_dir),
                           scale=sc, claims=claims or bool(by_clan), only_owner=owner,
                           owner_color=owner_color, mark_shops=bool(shops))
    if not res.get("ok"):
        return res, None, None
    png = res["png"]
    _MAPIMG_CACHE[ck] = (mt, png)
    if len(_MAPIMG_CACHE) > 24:
        _MAPIMG_CACHE.pop(next(iter(_MAPIMG_CACHE)))
    if res.get("owner_grid") is not None:
        _store_owner_grid(path, mt, world_dir, res)
    return png, "map%d.png" % map_id, {"w": res["w"], "h": res["h"], "scale": res["scale"]}


def map_pixels(cfg, map_id):
    """Сырые пиксели карты без клаймов: -> ``{ok, w, h, pixels}`` (RGB, w*h*3,
    строка 0 — верх картинки = игровой y = h-1). Для панели игроков — туман войны
    накладывается поверх, наружу целая карта не уходит."""
    if mapdt is None:
        return {"ok": False, "error": "модуль mapdt недоступен"}
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    path = os.path.join(world_dir, "Data", "maps", "map%d.dt" % int(map_id))
    if not os.path.exists(path):
        return {"ok": False, "error": "нет файла map%d.dt" % int(map_id)}
    d = mapdt.parse(path, world_dir=world_dir, keep_grid=False, paint=True,
                    block_class=_block_class(world_dir), claims=False)
    if not d.get("ok"):
        return d
    return {"ok": True, "w": d["w"], "h": d["h"], "pixels": bytes(d["pixels"])}


def _store_owner_grid(path, mt, world_dir, res):
    grid = list(res["owner_grid"] or [])
    names = load_user_list(world_dir)
    cnames = {c.get("id"): c.get("name") for c in _clans_raw(world_dir)}
    _col, uclan = clan_colors(world_dir)
    _MAPOWN_CACHE[path] = (mt, {
        "w": res["w"], "h": res["h"], "um_w": res["um_w"], "um_h": res["um_h"],
        "grid": grid,
        "names": {str(o): (names.get(o) or ("id %d" % o)) for o in set(grid) if o},
        "clans": {str(o): cnames.get(uclan[o]) for o in set(grid) if o and uclan.get(o) is not None},
    })
    if len(_MAPOWN_CACHE) > 12:
        _MAPOWN_CACHE.pop(next(iter(_MAPOWN_CACHE)))


def mapdt_owners(cfg, map_id):
    """Сетка владения землёй (userMap 8×8) для наведения на картинке.
    -> ``{ok, w, h, um_w, um_h, grid:[owner_id…] (x-мажор), names:{id:name}}``."""
    if mapdt is None:
        return {"ok": False, "error": "модуль mapdt недоступен"}
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    try:
        map_id = int(map_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad id"}
    path = os.path.join(world_dir, "Data", "maps", "map%d.dt" % map_id)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {"ok": False, "error": "нет файла map%d.dt" % map_id}
    hit = _MAPOWN_CACHE.get(path)
    if not (hit and hit[0] == mt):
        d = mapdt.parse(path, world_dir=world_dir, owners=True)
        if not d.get("ok"):
            return d
        _store_owner_grid(path, mt, world_dir,
                          {"owner_grid": d.get("owner_grid"), "w": d["w"], "h": d["h"],
                           "um_w": d["um_w"], "um_h": d["um_h"]})
        hit = _MAPOWN_CACHE.get(path)
    out = dict(hit[1])
    out["ok"] = True
    return out


def _resolve_item_query(world_dir, query):
    """query = id | точное имя | подстрока имени -> (want:set[int], matched:[{id,name}])."""
    by_id, by_name = _items_full(world_dir)
    s = str(query or "").strip()
    if not s:
        return set(), []
    m0 = re.search(r"#(\d+)\s*$", s)
    if m0 and int(m0.group(1)) in by_id:
        s = m0.group(1)
    if s.isdigit() and int(s) in by_id:
        i = int(s)
        return {i}, [{"id": i, "name": by_id[i].get("name")}]
    if s in by_name:
        i = by_name[s]["id"]
        return {i}, [{"id": i, "name": s}]
    low = s.lower()
    m = [{"id": i, "name": d.get("name")} for i, d in by_id.items()
         if d.get("name") and low in d["name"].lower()]
    m.sort(key=lambda x: (len(x["name"]), x["name"]))
    return {x["id"] for x in m}, m[:40]


# --- поиск предметов у игроков (склад + при себе) ----------------------------
_INV_IDX_CACHE = {}  # world_dir -> (ts, {uid: {stash: Counter, carry: Counter}})


def _inventory_index(world_dir, ttl=30):
    """{uid: {"stash": Counter{type:count}, "carry": Counter}} по всем игрокам.
    Склад — `user<N>.json → Inventory.items`, при себе — `unit<unitId>.json`.
    Кэш на ``ttl`` секунд (полный скан ~285 пар файлов)."""
    now = time.time()
    hit = _INV_IDX_CACHE.get(world_dir)
    if hit and now - hit[0] < ttl:
        return hit[1]
    ud = os.path.join(world_dir, "Data", "users")
    nd = os.path.join(world_dir, "Data", "units")
    idx = {}
    try:
        files = os.listdir(ud)
    except OSError:
        files = []
    for nm in files:
        m = _USER_FILE_RX.match(nm)
        if not m:
            continue
        raw = _read_json(os.path.join(ud, nm))
        if not raw:
            continue
        try:
            uid = int(raw.get("id", m.group(1)))
        except (TypeError, ValueError):
            continue
        stash = collections.Counter()
        for it in (raw.get("Inventory") or {}).get("items", []) or []:
            t = it.get("type")
            if t is not None:
                stash[t] += it.get("count") or 0
        carry = collections.Counter()
        unid = raw.get("unitId")
        if unid is not None:
            un = _read_json(os.path.join(nd, "unit%s.json" % unid)) or {}
            for it in (un.get("Inventory") or {}).get("items", []) or []:
                t = it.get("type")
                if t is not None:
                    carry[t] += it.get("count") or 0
        idx[uid] = {"stash": stash, "carry": carry}
    _INV_IDX_CACHE[world_dir] = (now, idx)
    return idx


def player_item_search(cfg, item):
    """Кто из игроков держит предмет ``item`` (id / имя / подстрока) — на складе
    и/или при себе. -> ``{ok, query, matched[{id,name}], want, players[{id,name,
    online,stash,carry,total}], totals{players,stash,carry,total}}`` (сорт по total)."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    want, matched = _resolve_item_query(world_dir, item)
    if not want:
        return {"ok": False, "error": "предмет не найден: %r" % item}
    names = load_user_list(world_dir)
    per, _ = parse_analytics(os.path.join(world_dir, "analytics.txt"))
    idx = _inventory_index(world_dir)
    rows = []
    t_st = t_ca = 0
    for uid, inv in idx.items():
        st = sum(inv["stash"].get(t, 0) for t in want)
        ca = sum(inv["carry"].get(t, 0) for t in want)
        if not (st or ca):
            continue
        t_st += st
        t_ca += ca
        rows.append({"id": uid, "name": names.get(uid) or ("id %d" % uid),
                     "online": bool((per.get(uid) or {}).get("online")),
                     "stash": st, "carry": ca, "total": st + ca})
    rows.sort(key=lambda r: (-r["total"], r["name"].lower()))
    return {
        "ok": True, "query": str(item), "matched": matched, "want": sorted(want),
        "players": rows,
        "totals": {"players": len(rows), "stash": t_st, "carry": t_ca,
                   "total": t_st + t_ca},
    }


# --- человеко-читаемые подписи к техам --------------------------------------
# tech.json не хранит названий (только id/parent/cost/level). _TECH_NAMES собран
# офлайн из craft.json + локализации клиента (StreamingAssets/EmbeddedServer +
# resources.assets <str id=...>): что каждый тех открывает в крафте. Ветка/тир —
# из дерева tech.json (parent/level). См. scratchpad/_gen_technames.py.
_TECH_FAMILY = {
    "b": "Строительство", "bb": "Мосты", "bt": "Лодки/амфибии", "car": "Автомобили",
    "ce": "Двигатели авто", "ceh": "Двигатели авто", "cet": "Двигатели авто", "ceb": "Двигатели авто",
    "ac": "Танки", "c": "Хранение", "e": "Верстаки/энергия", "r": "Ракеты",
    "re": "Ракетные двигатели", "s": "Радары", "sc": "Сборщики", "o": "Кислород/скафандры",
    "a": "Броня", "w": "Оружие ближнее/резаки", "wr": "Стрелковое", "wc": "Тяжёлое оружие",
    "ws": "Корабельные пушки", "ax": "Топоры/пилы", "p": "Кирки/буры", "sh": "Лопаты",
    "h": "Мотыги", "m": "Медицина/добыча", "hm": "Молоты/ремонт", "rb": "Ускорители",
    "clan": "Клан", "rob": "Клан·роботы", "proc": "Клан·процессоры",
    "progm": "Клан·модули PMR", "rcp": "Клан·пульт роботов", "cl": "Эндгейм",
}

_TECH_NAMES = {
    'b1': 'Деревянный пол',
    'b2': 'Кровать',
    'b3': 'Деревянная стена / Деревянная дверь',
    'b4': 'Каменная стена / Каменная дверь +1',
    'b5': 'Кирпичная стена / Кирпичная дверь +1',
    'b6': 'Железная стена / Железная дверь +1',
    'c1': 'Деревянный сундук',
    'e1': 'Печь',
    'e2': 'Кулинарный стол',
    'e3': 'Дробилка / Стальной порошок',
    'e4': 'Индустриальный верстак',
    'e5': 'Насос / Экстрактор',
    'e6': 'Дистиллятор',
    'r0': 'Ракета Jalopy / Ракетный двигатель Jalopy +1',
    'r1': 'Ракета R1',
    're1': 'Ракетный двигатель RE-1',
    's1': 'Космический радар SR-1',
    'o0': 'Кислородный ящик',
    'o1': 'Генератор кислорода OG-1',
    'o2': 'Скафандр',
    'a1': 'Кожанная одежда',
    'a2': 'Железная броня',
    'a3': 'Стальная броня',
    'a4': 'Титановая броня',
    'w1': 'Костяное копье',
    'w2': 'Железный нож',
    'w3': 'Железный меч',
    'w4': 'Железное копье',
    'wr1': 'Деревянный лук / Костяная стрела',
    'wr2': 'Арбалет',
    'wr3': 'Железная стрела',
    'wr4': 'Порох / Мушкет +1',
    'wr5': 'Револьвер / Револьверная пуля',
    'wr6': 'Ружье / Ружейная пуля',
    'wr7': 'Пистолет-пулемет / Пуля B-1',
    'ax1': 'Каменный топор',
    'ax2': 'Железный топор',
    'ax3': 'Стальной топор',
    'ax4': 'Титановый топор',
    'p1': 'Каменная кирка',
    'p2': 'Железная кирка',
    'p3': 'Стальная кирка',
    'p4': 'Титановая кирка',
    'sh1': 'Костяная лопата',
    'sh2': 'Железная лопата',
    'sh3': 'Стальная лопата',
    'sh4': 'Титановая лопата',
    'h1': 'Костяная мотыга',
    'h2': 'Железная мотыга',
    'h3': 'Стальная мотыга',
    'h4': 'Титановая мотыга',
    'bt1': 'Деревянная лодка',
    'm1': 'Лечебная мазь',
    'e3_1': 'Пресс',
    'e4_1': 'Торговая станция',
    'e4_2': 'Торговый сканнер',
    'm2': 'Сигары',
    'hm1': 'Каменный молот',
    'hm2': 'Стальной молот',
    'e1_1': 'Железный верстак',
    'c2': 'Ящик',
    'c3': 'Контейнер',
    'c4': 'Большой контейнер',
    'b2_1': 'Комфортная кровать',
    'b2_2': 'Титановая кровать',
    'b2_3': 'Вольфрамовая кровать',
    'b7': 'Грунтовый куб / Землекоп',
    'bb1': 'Деревянный мост',
    'bb2': 'Каменный мост',
    'bb3': 'Кирпичный мост',
    'bb4': 'Железный мост',
    'car1': 'Фургон V-1',
    'ce1': 'Автомобильный двигатель CE-1',
    'car2': 'Фургон V-2',
    'car3': 'Фургон V-3',
    'ce2': 'Автомобильный двигатель CE-2',
    'ce3': 'Автомобильный двигатель CE-3',
    'ac1': 'Бронеавтомобиль "Енот"',
    'ceh1': 'Автомобильный двигатель CEH-1',
    'ceh2': 'Автомобильный двигатель CEH-2',
    'wc1': 'Тяжелый пулемет "Кобра" / Пулеметные пули HB-1',
    'car4': 'Багги',
    'e6_1': 'Кобальтовая печь',
    'e6_2': 'Кобальтовая дробилка',
    'e6_3': 'Кобальтовый экстрактор',
    'e6_4': 'Кобальтовый дистиллятор',
    'e6_5': 'Кобальтовый пресс',
    'ws1': 'Корабельная пушка "Гадюка" / Снаряды SL-1',
    'sc1': 'Сборщик предметов "Хомяк"',
    'o3': 'Скафандр S2',
    'o4': 'Генератор кислорода OG-2',
    'm3': 'Аптечка',
    'm4': 'Энергосмесь',
    'r2': 'Ракета R2',
    'r3': 'Ракета R3',
    'r4': 'Ракета RС1',
    'r5': 'Ракета RС2',
    're2': 'Ракетный двигатель RE-2',
    're3': 'Ракетный двигатель RE-3',
    're4': 'Ракетный двигатель SFE-1',
    're5': 'Ракетный двигатель SFE-2',
    're6': 'Ракетный двигатель SFE-3',
    'rb1': 'Ксинитрон / Ракетный ускоритель RB-1',
    'w5': 'Титановый резак',
    'w6': 'Кобальтовый резак',
    'w7': 'Вольфрамовый резак',
    'w8': 'Омикрониумный резак',
    'ws2': 'Корабельная пушка "Анаконда" / Снаряды SL-2',
    'ws3': 'Корабельная пушка "Тайпан" / Снаряды SL-3',
    'ax5': 'Кобальтовая пила',
    'ax6': 'Вольфрамовая пила',
    'ax7': 'Омикрониумная пила',
    'p5': 'Кобальтовый бур',
    'p6': 'Вольфрамовый бур',
    'p7': 'Омикрониумный бур',
    'bt2': 'Железная лодка',
    'e7': 'Лабораторный стол',
    'b8': 'Посадочная площадка',
    'e7_1': 'Биосканер',
    'hm3': 'Ремонтный ящик',
    'a5': 'Кобальтовая броня',
    'a6': 'Вольфрамовая броня',
    'a7': 'Омикрониумная броня',
    'e8': 'Киберверстак',
    'wr8': 'Пулемет MG-1',
    'wr9': 'Ружье V-1 / Пуля B-2',
    'wr10': 'Автомат M-1',
    'wr11': 'Пулемет MG-2',
    'wr12': 'Ружье V-2',
    'wr13': 'Огнемет',
    'wr14': 'Электропушка / Заряд E-1',
    'ceh3': 'Автомобильный двигатель CEH-3',
    'ceh4': 'Автомобильный двигатель CEH-4',
    'bt3': 'Катер "Карась"',
    'bt4': 'Катер "Щука"',
    'bt5': 'Амфибия "Верблюд"',
    'bt6': 'Амфибия "Слон"',
    'bt7': 'Амфибия "Хорек"',
    'bt8': 'Амфибия "Черепаха"',
    'ac2': 'Броневик "Бурундук"',
    'ac3': 'Танк "Выхухоль"',
    'ac4': 'Танк "Лиса"',
    'ac5': 'Танк "Крокодил"',
    'ac6': 'Танк "Волк"',
    'ac7': 'Танк "Носорог"',
    'ac8': 'Танк "Медведь"',
    'wc2': 'Тяжелый пулемет "Гюрза" / Пулеметные пули HB-2',
    'wc3': 'Пушка "Полоз" / Снаряд S-1',
    'wc4': 'Пушка "Эфа" / Снаряд S-2',
    'wc5': 'Пушка "Мамба" / Снаряд S-3',
    'wc6': 'Пушка "Аспид" / Снаряд S-4',
    'wc7': 'Пушка "Удав" / Снаряд S-5',
    'wc8': 'Пушка "Питон" / Снаряд S-6',
    're7': 'Иониум / Ионный двигатель I-1',
    're8': 'Ионный двигатель I-2',
    're9': 'Ядерный двигатель N-1',
    're10': 'Ядерный двигатель N-2',
    'r6': 'Ракета "Птеродактиль"',
    'r7': 'Ракета "Карнотавр"',
    'r8': 'Ракета "Тираннозавр"',
    'r9': 'Ракета "Стегозавр"',
    'r10': 'Ракета "Зауропод"',
    's2': 'Космический радар SR-2',
    'r11': 'Модуль станции / Панель управления станцией',
    're11': 'Гипердвигатель',
    'o5': 'Бронескафандр S3',
    'o6': 'Бронескафандр S4',
    'e1_2': 'Титановый верстак',
    'e1_3': 'Вольфрамовый верстак',
    'ws4': 'Корабельная пушка "Бумсланг" / Снаряды SL-4',
    'ws5': 'Корабельная пушка "Крайт" / Снаряды SL-5',
    'b9': 'Блок гидропоники',
    'e7_2': 'Планетарный сканер',
    'c5': 'Предметы хранящиеся в холодильнике не портятся',
    'b10': 'Генератор озеленения',
    'hm4': 'Омикрониумный насос',
    'clan1': 'Ракета "Спинозавр"',
    'rob1': 'Робот ROB-1',
    'rob2': 'Робот ROB-2',
    'rob3': 'Робот ROB-3',
    'rob4': 'Робот ROB-4',
    'proc1': 'Процессор P-1',
    'proc2': 'Процессор P-2',
    'proc3': 'Процессор P-3',
    'progm1': 'Модуль PMR Mining',
    'b11': 'Предмет не дает локациям выгружаться в течении 120 часов при отсутствии игроков',
    'progm2': 'Модуль PMR Refueling',
    'progm3': 'Модуль PMR Farm',
    'car5': 'Автомобиль "Кенгуру"',
    'progm4': 'Модуль PMR Transfer',
    'progm5': 'Модуль PMR Recycling',
    'e7_3': 'Для блокировки планеты от посадки ракет на игровой месяц',
    'e9': 'Перерабатывающий верстак',
    'rcp': 'Пульт управления роботами',
    'hm5': 'Кнут',
    'c6': 'Кормушка',
    'c7': 'Кормушка-холодильник',
    'e10': 'Индустриальный миксер',
    'progm6': 'Модуль PMR Collector',
    'progm7': 'Модуль PMR Robot Refuel',
    'cet': 'Автомобильный двигатель CET',
    'ceb': 'Автомобильный двигатель CEB',
    'e11': 'Контейнер-собиратель',
    'cl1': 'Коллайдер / Блок коллайдера',
    'e12': 'Квантовый верстак',
    'p8': 'Иридиевый бур',
    'p9': 'Эпсилон бур',
    'p10': 'Квантовый бур',
    'proc4': 'Процессор P-4',
    'proc5': 'Процессор P-5',
    'w9': 'Иридиевый резак',
    'w10': 'Эпсилон резак',
    'w11': 'Квантовый резак',
    'b6_1': 'Титановая стена / Титановая дверь +1',
    're12': 'Квантовый ракетный двигатель',
    'r12': 'Ракета "Диплодок"',
    'a8': 'Иридиевая броня',
    'a9': 'Эпсилон броня',
    'e6_6': 'Омикрониумная печь',
    'e6_7': 'Омикрониумная дробилка',
    'e6_8': 'Омикрониумный экстрактор',
    'e6_9': 'Омикрониумный дистиллятор',
    'e6_10': 'Омикрониумный пресс',
    're13': 'Квантовый гипердвигатель',
    'b12': 'Экстра гидропоника',
    'cl2': 'Портал',
    'e13': 'Ящик аннигилятор',
}
_TECH_META_CACHE = {}  # path -> (mtime, {id: {...}})


def tech_meta(world_dir):
    """{tech_id: {name, family, root, depth, level, cost_min, cost_h, clan, label}}.
    label = реальное название (что открывает) либо «ветка · тир N» как запасной."""
    if not world_dir:
        return {}
    p = os.path.join(world_dir, "Data", "tech.json")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return {}
    hit = _TECH_META_CACHE.get(p)
    if hit and hit[0] == mt:
        return hit[1]
    raw = _read_json(p) or {}
    items = {it["id"]: it for it in raw.get("items", []) if "id" in it}
    out = {}
    for tid, it in items.items():
        root, depth, cur, seen = tid, 0, tid, set()
        while True:
            par = (items.get(cur) or {}).get("parent")
            if not par or par in seen:
                break
            seen.add(par)
            root, cur, depth = par, par, depth + 1
        pref = re.match(r"[a-zA-Z]+", tid)
        pref = pref.group(0) if pref else tid
        fam = _TECH_FAMILY.get(pref) or _TECH_FAMILY.get(root) or ("ветка " + pref)
        cost = it.get("cost")
        nm = _TECH_NAMES.get(tid)
        out[tid] = {
            "name": nm, "family": fam, "root": root, "depth": depth,
            "level": it.get("level"), "cost_min": cost,
            "cost_h": round(cost / 60.0, 1) if cost else None,
            "clan": bool(it.get("isClan")),
            "label": nm or ("%s · тир %d" % (fam, depth + 1)),
        }
    _TECH_META_CACHE[p] = (mt, out)
    return out


def tech_label(world_dir, tid):
    if not tid:
        return ""
    m = tech_meta(world_dir).get(tid)
    return ("%s — %s" % (tid, m["label"])) if m else tid


def tech_tree(cfg):
    """Дерево технологий для схемы изучения (игрок/клан) — порядок как в
    ``tech.json`` (так же идёт в игре). -> ``{ok, nodes:[{id, parent, label,
    family, cost_h, level, clan}]}``."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    raw = _read_json(os.path.join(world_dir, "Data", "tech.json")) or {}
    meta = tech_meta(world_dir)
    unl = tech_unlocks(world_dir)
    nodes = []
    for it in raw.get("items", []):
        tid = it.get("id")
        if not tid:
            continue
        m = meta.get(tid) or {}
        nodes.append({"id": tid, "parent": it.get("parent"), "label": m.get("label") or tid,
                      "family": m.get("family") or "", "cost_h": m.get("cost_h"),
                      "level": it.get("level"), "clan": bool(it.get("isClan")),
                      "unlocks": unl.get(tid, [])})
    return {"ok": True, "nodes": nodes}


# ZData.ClanRole / ClanSlotType (Il2CppDumper, GameAssembly.dll), подписи — из
# клиентской локализации (role_*, textClan*).
_CLAN_ROLES_RU = {0: "Лидер", 1: "Офицер", 2: "Участник", 3: "Капрал"}
_CLAN_SLOT_TYPES_RU = {0: "Бой", 1: "Производство", 2: "Наука", 3: "Фермерство", 4: "Пилот"}


def _clans_raw(world_dir):
    return (_read_json(os.path.join(world_dir, "Data", "game", "clans.json")) or {}).get("clans") or []


def clans_list(cfg):
    """Все кланы: размер, рейтинг, CP, лидер, сколько онлайн, клан-технологии."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    names = load_user_list(world_dir)
    online = _online_now(world_dir)
    out = []
    for c in _clans_raw(world_dir):
        users = c.get("users") or []
        leader = next((u.get("userId") for u in users if u.get("role") == 0), None)
        out.append({
            "id": c.get("id"), "name": c.get("name") or ("clan %s" % c.get("id")),
            "size": len(users), "max": c.get("maxUserCount"),
            "rating": c.get("rating"), "clan_point": c.get("clanPoint"),
            "trading": bool(c.get("isTrading")),
            "leader": {"id": leader, "name": names.get(leader) or ("id %s" % leader)} if leader is not None else None,
            "online": sum(1 for u in users if online.get(u.get("userId"))),
            "tech_count": len(c.get("tech") or []),
        })
    out.sort(key=lambda x: -(x["rating"] or 0))
    return {"ok": True, "clans": out}


def clan_detail(cfg, cid):
    """Клан целиком: состав (с уровнем/часами/исследованиями/специализацией),
    клан-технологии, слоты специализаций и «покрытие» — сколько участников
    знают каждую личную технологию."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad id"}
    c = next((x for x in _clans_raw(world_dir) if x.get("id") == cid), None)
    if not c:
        return {"ok": False, "error": "клан %d не найден" % cid}
    names = load_user_list(world_dir)
    online = _online_now(world_dir)
    st = server_time(world_dir)
    tcost = _load_ref(world_dir, "tech.json", "id", "cost")
    slot_of = {}
    slots = []
    for i, s in enumerate(c.get("slots") or []):
        uid = s.get("userId") or 0
        row = {"idx": i, "blocked": bool(s.get("isBlock")),
               "positive": _CLAN_SLOT_TYPES_RU.get(s.get("positive"), "#%s" % s.get("positive")),
               "negative": _CLAN_SLOT_TYPES_RU.get(s.get("negative"), "#%s" % s.get("negative")),
               "user": {"id": uid, "name": names.get(uid) or ("id %s" % uid)} if uid else None}
        slots.append(row)
        if uid:
            slot_of[uid] = row
    members, coverage = [], {}
    for u in c.get("users") or []:
        uid = u.get("userId")
        raw = _read_json(_user_file(world_dir, uid)) if uid is not None else {}
        techs = raw.get("techList") or []
        for t in techs:
            coverage[t] = coverage.get(t, 0) + 1
        sl = slot_of.get(uid)
        members.append({
            "id": uid, "name": names.get(uid) or raw.get("name") or ("id %s" % uid),
            "role": u.get("role"), "role_name": _CLAN_ROLES_RU.get(u.get("role"), "#%s" % u.get("role")),
            "rating": u.get("rating"), "clan_point": u.get("clanPoint"),
            "online": bool(online.get(uid)),
            "level": raw.get("unitLevel"),
            "playtime_h": round(float(raw.get("timeGame") or 0) / 3600.0, 1),
            "last_seen_h": round((st - float(raw.get("lastTimeGame") or 0)) / 3600.0, 1)
                           if (st and raw.get("lastTimeGame")) else None,
            "tech_count": len(techs),
            "research_h": round(sum(tcost.get(t) or 0 for t in techs) / 60.0, 1),
            "researching": tech_label(world_dir, raw.get("researchTech")) if raw.get("researchTech") else "",
            "techs": techs,
            "spec": {"positive": sl["positive"], "negative": sl["negative"]} if sl else None,
        })
    members.sort(key=lambda m: (m["role"] if m["role"] is not None else 9, -(m["rating"] or 0)))
    ctech = c.get("tech") or []
    return {
        "ok": True, "id": cid, "name": c.get("name") or ("clan %d" % cid),
        "size": len(members), "max": c.get("maxUserCount"),
        "rating": c.get("rating"), "clan_point": c.get("clanPoint"),
        "trading": bool(c.get("isTrading")),
        "online": sum(1 for m in members if m["online"]),
        "tech": ctech, "tech_named": [{"id": t, "label": tech_label(world_dir, t)} for t in ctech],
        "members": members, "slots": slots, "coverage": coverage,
    }


# --- русские названия предметов (slug из Data\items.json -> текст из клиентской
# локализации, секция items; 446/446 покрыто на 2026-09-24) ------------------
_ITEM_NAMES_RU = {
    'seed_grass': 'Семена травы', 'seed_bush': 'Семена куста', 'seed_waterlily': 'Семена кувшинки',
    'seed_tree': 'Семена дерева', 'wood': 'Дерево', 'leaf': 'Лист', 'grass': 'Трава',
    'berry': 'Ягода', 'wall_wood': 'Деревянная стена', 'door_wood': 'Деревянная дверь',
    'floor_wood': 'Деревянный пол', 'workbench': 'Верстак', 'club': 'Дубина',
    'axe_wood': 'Деревянный топор', 'chest_wood': 'Деревянный сундук', 'bed': 'Кровать',
    'stone': 'Камень', 'coal': 'Уголь', 'iron_ore': 'Железная руда',
    'pick_wood': 'Деревянная кирка', 'furnace': 'Печь', 'iron_bar': 'Железный слиток',
    'meat': 'Мясо', 'bone': 'Кость', 'bow_wooden': 'Деревянный лук',
    'arrow_bone': 'Костяная стрела', 'spear': 'Костяное копье', 'roast': 'Жаренное мясо',
    'wooden_boat': 'Деревянная лодка', 'seed_spruce': 'Семена ели', 'seed_birch': 'Семена березы',
    'seed_seaweed': 'Семена водорослей', 'copper_ore': 'Медная руда', 'nitrocalite': 'Нитрокалит',
    'lead_ore': 'Свинцовая руда', 'titanium_ore': 'Титановая руда', 'uranium_ore': 'Урановая руда',
    'leather': 'Кожа', 'sulfur': 'Сера', 'silver_ore': 'Серебрянная руда',
    'gold_ore': 'Золотая руда', 'iron_powder': 'Железный порошок',
    'coal_powder': 'Угольный порошок', 'iron_coal_powder': 'Стальной порошок',
    'steel_bar': 'Стальной слиток', 'copper_bar': 'Медный слиток',
    'nitrocalite_powder': 'Порошок нитрокалита', 'lead_bar': 'Свинцовый слиток',
    'titanium_bar': 'Титановый слиток', 'uranium_bar': 'Урановый слиток',
    'silver_bar': 'Серебрянный слиток', 'gold_bar': 'Золотой слиток', 'powder': 'Порох',
    'crusher': 'Дробилка', 'iron_knife': 'Железный нож', 'iron_sword': 'Железный меч',
    'crossbow': 'Арбалет', 'axe_stone': 'Каменный топор', 'axe_iron': 'Железный топор',
    'pick_stone': 'Каменная кирка', 'pick_iron': 'Железная кирка', 'salve': 'Лечебная мазь',
    'leather_clothes': 'Кожанная одежда', 'seed_rubber_tree': 'Семена каучукового дерева',
    'rubber': 'Каучук', 'gum': 'Резина', 'oil': 'Нефть', 'kerosene': 'Керосин', 'pump': 'Насос',
    'industrial_workbench': 'Индустриальный верстак', 'extractor': 'Экстрактор',
    'distiller': 'Дистиллятор', 'sand': 'Песок', 'glass': 'Стекло', 'rocket_r1': 'Ракета R1',
    'rocket_engine_re1': 'Ракетный двигатель RE-1', 'space_radar_sr1': 'Космический радар SR-1',
    'oxygen_generator_og1': 'Генератор кислорода OG-1', 'meat_fish': 'Рыба',
    'meat_human': 'Мясо человека', 'shovel_bone': 'Костяная лопата', 'hoe_bone': 'Костяная мотыга',
    'seed_potatoes': 'Семена картофеля', 'potatoes': 'Картофель', 'seed_rice': 'Семена риса',
    'rice': 'Рис', 'seed_corn': 'Семена кукурузы', 'corn': 'Кукуруза',
    'seed_tomatoes': 'Семена помидоров', 'tomatoes': 'Помидоры', 'seed_onion': 'Семена лука',
    'onion': 'Лук', 'seed_mushrooms': 'Семена грибов', 'mushrooms': 'Грибы',
    'seed_carrot': 'Семена моркови', 'carrot': 'Морковь', 'seed_cabbage': 'Семена капусты',
    'cabbage': 'Капуста', 'seed_pumpkin': 'Семена тыквы', 'pumpkin': 'Тыква',
    'seed_beet': 'Семена свеклы', 'beet': 'Свекла', 'seed_dill': 'Семена укропа', 'dill': 'Укроп',
    'seed_wheat': 'Семена пшеницы', 'wheat': 'Пшеница', 'seed_cucumber': 'Семена огурца',
    'cucumber': 'Огурец', 'seed_strawberry': 'Семена клубники', 'strawberry': 'Клубника',
    'seed_apple': 'Семена яблока', 'apple': 'Яблоко', 'seed_garlic': 'Семена чеснока',
    'garlic': 'Чеснок', 'seed_grape': 'Семена винограда', 'grape': 'Виноград',
    'seed_pepper': 'Семена перца', 'pepper': 'Перец',
    'seed_sugar_cane': 'Семена сахарного тростника', 'sugar_cane': 'Сахарный тростник',
    'seed_bananas': 'Семена бананов', 'bananas': 'Банан', 'seed_oranges': 'Семена апельсина',
    'oranges': 'Апельсин', 'seed_lemons': 'Семена лимона', 'lemons': 'Лимон',
    'seed_bell_pepper': 'Семена сладкого перца', 'bell_pepper': 'Сладкий перец',
    'seed_pineapple': 'Семена ананаса', 'pineapple': 'Ананас', 'seed_watermelon': 'Семена арбуза',
    'watermelon': 'Арбуз', 'seed_coconut': 'Семена кокоса', 'coconut': 'Кокос', 'flour': 'Мука',
    'sugar': 'Сахар', 'salt': 'Соль', 'seed_tropical_tree': 'Семена тропического дерева',
    'seed_baobab': 'Семена баобаба', 'seed_sequoia': 'Семена секвойи', 'cobalt': 'Кобальт',
    'tungsten': 'Вольфрам', 'platinum': 'Платина', 'omicronium': 'Омикрониум', 'xirium': 'Ксириум',
    'thaumin': 'Таумин', 'plutonium': 'Плутоний', 'faunitron': 'Фаунитрон', 'protonite': 'Протонит',
    'cobalt_bar': 'Слиток кобальта', 'tungsten_bar': 'Слиток вольфрама',
    'platinum_bar': 'Слиток платины', 'omicronium_bar': 'Слиток омикрониума',
    'plutonium_bar': 'Слиток плутония', 'culinary_table': 'Кулинарный стол', 'dish': 'Блюдо',
    'space_suit': 'Скафандр', 'wall_stone': 'Каменная стена', 'door_stone': 'Каменная дверь',
    'floor_stone': 'Каменный пол', 'wall_brick': 'Кирпичная стена', 'door_brick': 'Кирпичная дверь',
    'floor_brick': 'Кирпичный пол', 'wall_iron': 'Железная стена', 'door_iron': 'Железная дверь',
    'floor_iron': 'Железный пол', 'iron_armor': 'Железная броня', 'steel_armor': 'Стальная броня',
    'titan_armor': 'Титановая броня', 'iron_spear': 'Железное копье',
    'arrow_iron': 'Железная стрела', 'musket': 'Мушкет', 'musket_bullet': 'Мушкетная пуля',
    'revolver': 'Револьвер', 'revolver_bullet': 'Револьверная пуля', 'rifle': 'Ружье',
    'rifle_bullet': 'Ружейная пуля', 'submachine_gun': 'Пистолет-пулемет',
    'submachine_gun_bullet': 'Пуля B-1', 'fried_fish': 'Жаренная рыба',
    'shovel_iron': 'Железная лопата', 'shovel_steel': 'Стальная лопата',
    'shovel_titan': 'Титановая лопата', 'hoe_iron': 'Железная мотыга',
    'hoe_steel': 'Стальная мотыга', 'hoe_titan': 'Титановая мотыга', 'pick_steel': 'Стальная кирка',
    'pick_titan': 'Титановая кирка', 'axe_steel': 'Стальной топор', 'axe_titan': 'Титановый топор',
    'press': 'Пресс', 'silver_coin': 'Серебрянные монеты', 'gold_coin': 'Золотые монеты',
    'platinum_coin': 'Платиновые монеты', 'trading_station': 'Торговая станция',
    'trade_scanner': 'Торговый сканнер', 'cigar': 'Сигары', 'stone_hammer': 'Каменный молот',
    'steel_hammer': 'Стальной молот', 'iron_workbench': 'Железный верстак', 'box': 'Ящик',
    'container': 'Контейнер', 'large_container': 'Большой контейнер',
    'comfortable_bed': 'Комфортная кровать', 'titanium_bed': 'Титановая кровать',
    'tungsten_bed': 'Вольфрамовая кровать', 'ground_cube': 'Грунтовый куб', 'ditch_dig': 'Землекоп',
    'wooden_bridge': 'Деревянный мост', 'stone_bridge': 'Каменный мост',
    'brick_bridge': 'Кирпичный мост', 'iron_bridge': 'Железный мост', 'car_1': 'Фургон V-1',
    'car_engine1': 'Автомобильный двигатель CE-1', 'car_2': 'Фургон V-2', 'car_3': 'Фургон V-3',
    'car_engine2': 'Автомобильный двигатель CE-2', 'car_engine3': 'Автомобильный двигатель CE-3',
    'armored_car1': 'Бронеавтомобиль "Енот"', 'car_engine_ceh1': 'Автомобильный двигатель CEH-1',
    'car_engine_ceh2': 'Автомобильный двигатель CEH-2',
    'heavy_machine_gun1': 'Тяжелый пулемет "Кобра"',
    'heavy_machine_gun_bullet1': 'Пулеметные пули HB-1', 'car_buggy': 'Багги',
    'tech_booster': 'Ускоритель исследования', 'cobalt_furnace': 'Кобальтовая печь',
    'cobalt_crusher': 'Кобальтовая дробилка', 'cobalt_extractor': 'Кобальтовый экстрактор',
    'cobalt_distiller': 'Кобальтовый дистиллятор', 'cobalt_press': 'Кобальтовый пресс',
    'rocket_jalopy': 'Ракета Jalopy', 'rocket_engine_jalopy': 'Ракетный двигатель Jalopy',
    'oxygen_box': 'Кислородный ящик', 'space_gun_viper': 'Корабельная пушка "Гадюка"',
    'space_gun_shell1': 'Снаряды SL-1', 'item_collector1': 'Сборщик предметов "Хомяк"',
    'space_suit2': 'Скафандр S2', 'oxygen_generator_og2': 'Генератор кислорода OG-2',
    'fat_tail': 'Курдючный жир', 'brain': 'Мозги', 'medicine_chest': 'Аптечка',
    'energy_mixture': 'Энергосмесь', 'rocket_r2': 'Ракета R2', 'rocket_r3': 'Ракета R3',
    'rocket_cargo1': 'Ракета RС1', 'rocket_cargo2': 'Ракета RС2',
    'rocket_engine_re2': 'Ракетный двигатель RE-2', 'rocket_engine_re3': 'Ракетный двигатель RE-3',
    'rocket_engine_sfe1': 'Ракетный двигатель SFE-1',
    'rocket_engine_sfe2': 'Ракетный двигатель SFE-2',
    'rocket_engine_sfe3': 'Ракетный двигатель SFE-3', 'xinitron': 'Ксинитрон',
    'rocket_booster_rb1': 'Ракетный ускоритель RB-1', 'bone_flour': 'Костная мука',
    'titan_cutter': 'Титановый резак', 'cobalt_cutter': 'Кобальтовый резак',
    'tungsten_cutter': 'Вольфрамовый резак', 'omicronium_cutter': 'Омикрониумный резак',
    'space_gun_anaconda': 'Корабельная пушка "Анаконда"', 'space_gun_shell2': 'Снаряды SL-2',
    'space_gun_taipan': 'Корабельная пушка "Тайпан"', 'space_gun_shell3': 'Снаряды SL-3',
    'cobalt_drill': 'Кобальтовый бур', 'cobalt_saw': 'Кобальтовая пила',
    'tungsten_drill': 'Вольфрамовый бур', 'tungsten_saw': 'Вольфрамовая пила',
    'omicronium_drill': 'Омикрониумный бур', 'omicronium_saw': 'Омикрониумная пила',
    'iron_boat': 'Железная лодка', 'kidneys': 'Почки', 'liver': 'Печень', 'stomach': 'Желудок',
    'lungs': 'Легкие', 'eyes': 'Глаза', 'spleen': 'Селезенка', 'ears': 'Уши', 'cartilage': 'Хрящи',
    'heart': 'Сердце', 'lab_table': 'Лабораторный стол', 'mixture': 'Микстура',
    'landing_area': 'Посадочная площадка', 'bio_scanner': 'Биосканер', 'poultry': 'Мясо птицы',
    'repair_box': 'Ремонтный ящик', 'cobalt_armor': 'Кобальтовая броня',
    'tungsten_armor': 'Вольфрамовая броня', 'omicronium_armor': 'Омикрониумная броня',
    'cyber_workbench': 'Киберверстак', 'machine_gun_mg1': 'Пулемет MG-1', 'rifle_v1': 'Ружье V-1',
    'bullet_b2': 'Пуля B-2', 'automat_m1': 'Автомат M-1', 'machine_gun_mg2': 'Пулемет MG-2',
    'rifle_v2': 'Ружье V-2', 'fire_gun': 'Огнемет', 'electro_gun': 'Электропушка',
    'bullet_e1': 'Заряд E-1', 'car_engine_ceh3': 'Автомобильный двигатель CEH-3',
    'car_engine_ceh4': 'Автомобильный двигатель CEH-4',
    'armored_car_chipmunk': 'Броневик "Бурундук"', 'tank_muskrat': 'Танк "Выхухоль"',
    'tank_fox': 'Танк "Лиса"', 'tank_crocodile': 'Танк "Крокодил"', 'tank_wolf': 'Танк "Волк"',
    'tank_rhinoceros': 'Танк "Носорог"', 'tank_bear': 'Танк "Медведь"',
    'boat_crucian': 'Катер "Карась"', 'boat_pike': 'Катер "Щука"',
    'amphibian_camel': 'Амфибия "Верблюд"', 'amphibian_elephant': 'Амфибия "Слон"',
    'amphibian_polecat': 'Амфибия "Хорек"', 'amphibian_turtle': 'Амфибия "Черепаха"',
    'iron_block': 'Железный блок', 'steel_block': 'Стальной блок', 'copper_block': 'Медный блок',
    'lead_block': 'Свинцовый блок', 'titanium_block': 'Титановый блок',
    'cobalt_block': 'Кобальтовый блок', 'gum_block': 'Резиновый блок',
    'heavy_machine_gun2': 'Тяжелый пулемет "Гюрза"', 'light_cannon1': 'Пушка "Полоз"',
    'light_cannon2': 'Пушка "Эфа"', 'medium_cannon1': 'Пушка "Мамба"',
    'medium_cannon2': 'Пушка "Аспид"', 'heavy_cannon1': 'Пушка "Удав"',
    'heavy_cannon2': 'Пушка "Питон"', 'heavy_machine_gun_bullet2': 'Пулеметные пули HB-2',
    'gun_shell1': 'Снаряд S-1', 'gun_shell2': 'Снаряд S-2', 'gun_shell3': 'Снаряд S-3',
    'gun_shell4': 'Снаряд S-4', 'gun_shell5': 'Снаряд S-5', 'gun_shell6': 'Снаряд S-6',
    'ionium': 'Иониум', 'rocket_engine_i1': 'Ионный двигатель I-1',
    'rocket_engine_i2': 'Ионный двигатель I-2', 'rocket_engine_n1': 'Ядерный двигатель N-1',
    'rocket_engine_n2': 'Ядерный двигатель N-2', 'rocket_pterodactyl': 'Ракета "Птеродактиль"',
    'rocket_carnotaurus': 'Ракета "Карнотавр"', 'rocket_tyrannosaurus': 'Ракета "Тираннозавр"',
    'rocket_stegosaurus': 'Ракета "Стегозавр"', 'rocket_sauropod': 'Ракета "Зауропод"',
    'space_radar_sr2': 'Космический радар SR-2', 'station_module': 'Модуль станции',
    'station_control_panel': 'Панель управления станцией', 'nuclear_rod': 'Ядерный стержень',
    'tungsten_block': 'Вольфрамовый блок', 'omicronium_block': 'Омикрониумный блок',
    'hyperdrive': 'Гипердвигатель', 'space_suit3': 'Бронескафандр S3',
    'space_suit4': 'Бронескафандр S4', 'titan_workbench': 'Титановый верстак',
    'tungsten_workbench': 'Вольфрамовый верстак',
    'space_gun_boomslang': 'Корабельная пушка "Бумсланг"',
    'space_gun_krait': 'Корабельная пушка "Крайт"', 'space_gun_shell4': 'Снаряды SL-4',
    'space_gun_shell5': 'Снаряды SL-5', 'hydroponics_unit': 'Блок гидропоники', 'paper': 'Бумага',
    'drawing_table': 'Чертежный стол', 'white_drawing': 'Белый чертеж',
    'yellow_drawing': 'Желтый чертеж', 'green_drawing': 'Зеленый чертеж',
    'blue_drawing': 'Синий чертеж', 'red_drawing': 'Красный чертеж',
    'black_drawing': 'Черный чертеж', 'planetary_scanner': 'Планетарный сканер',
    'refrigerator': 'Холодильник', 'poisonous_moss': 'Ядовитый мох',
    'sulfuric_acid': 'Серная кислота', 'biofuel': 'Биотопливо',
    'landscaping_generator': 'Генератор озеленения', 'omicronium_pump': 'Омикрониумный насос',
    'rocket_spinosaurus': 'Ракета "Спинозавр"', 'robot_rob1': 'Робот ROB-1',
    'robot_rob2': 'Робот ROB-2', 'robot_rob3': 'Робот ROB-3', 'robot_rob4': 'Робот ROB-4',
    'processor_p1': 'Процессор P-1', 'processor_p2': 'Процессор P-2',
    'processor_p3': 'Процессор P-3', 'prog_module_mining': 'Модуль PMR Mining',
    'prog_module_refueling': 'Модуль PMR Refueling', 'prog_module_farm': 'Модуль PMR Farm',
    'planetary_stabilizer': 'Планетарный стабилизатор', 'car_kangaroo': 'Автомобиль "Кенгуру"',
    'prog_module_transfer': 'Модуль PMR Transfer', 'prog_module_recycling': 'Модуль PMR Recycling',
    'planet_blocker': 'Блокиратор планеты', 'recycling_workbench': 'Перерабатывающий верстак',
    'robot_control_panel': 'Пульт управления роботами', 'whip': 'Кнут', 'feeder': 'Кормушка',
    'feeder_refrigerator': 'Кормушка-холодильник', 'industrial_mixer': 'Индустриальный миксер',
    'prog_module_collector': 'Модуль PMR Collector',
    'prog_module_robot_refuel': 'Модуль PMR Robot Refuel',
    'car_engine_cet': 'Автомобильный двигатель CET',
    'car_engine_ceb': 'Автомобильный двигатель CEB', 'milk': 'Молоко', 'red_caviar': 'Красная икра',
    'black_caviar': 'Черная икра', 'egg': 'Яйцо', 'honey': 'Мед', 'butter': 'Масло',
    'curd': 'Творог', 'collector_container': 'Контейнер-собиратель', 'iridium': 'Иридий',
    'electronium': 'Электрониум', 'epsilon_metal': 'Эпсилон-металл', 'vulcanite': 'Вулканит',
    'cosmochlor': 'Космохлор', 'iridium_bar': 'Слиток иридиума',
    'electronium_bar': 'Слиток электрониума', 'epsilon_metal_bar': 'Слиток эпсилон-металла',
    'collider': 'Коллайдер', 'collider_block': 'Блок коллайдера', 'antimatter': 'Антиматерия',
    'null_matter': 'Нуль-материя', 'quantum_workbench': 'Квантовый верстак',
    'iridium_drill': 'Иридиевый бур', 'epsilon_drill': 'Эпсилон бур',
    'quantum_drill': 'Квантовый бур', 'processor_p4': 'Процессор P-4',
    'processor_p5': 'Процессор P-5', 'iridium_cutter': 'Иридиевый резак',
    'epsilon_cutter': 'Эпсилон резак', 'quantum_cutter': 'Квантовый резак',
    'wall_titan': 'Титановая стена', 'door_titan': 'Титановая дверь',
    'floor_titan': 'Титановый пол', 'rocket_engine_quantum': 'Квантовый ракетный двигатель',
    'rocket_diplodocus': 'Ракета "Диплодок"', 'iridium_armor': 'Иридиевая броня',
    'epsilon_armor': 'Эпсилон броня', 'omicronium_furnace': 'Омикрониумная печь',
    'omicronium_crusher': 'Омикрониумная дробилка',
    'omicronium_extractor': 'Омикрониумный экстрактор',
    'omicronium_distiller': 'Омикрониумный дистиллятор', 'omicronium_press': 'Омикрониумный пресс',
    'quantum_hyperdrive': 'Квантовый гипердвигатель', 'extra_hydroponics': 'Экстра гидропоника',
    'rescue_capsule': 'Спасательная капсула', 'portal': 'Портал',
    'box_annihilator': 'Ящик аннигилятор',
}


def item_label(slug):
    return _ITEM_NAMES_RU.get(slug) or slug or ""


# --- крафт: Data\craft.json (ZData.CraftInfo) + Data\machines.json -----------
_CRAFT_CACHE = {}  # world_dir -> (mtimes, data)


def _craft_data(world_dir):
    """-> {recipes: {slug: {time,count,tech,group,workbench,res:[(slug,n)]}},
    machine: {product: [(machine, material, energy)]}, uses: {slug: [slug]}}."""
    pc = os.path.join(world_dir, "Data", "craft.json")
    pm = os.path.join(world_dir, "Data", "machines.json")
    try:
        mts = (os.path.getmtime(pc), os.path.getmtime(pm) if os.path.exists(pm) else 0)
    except OSError:
        return None
    hit = _CRAFT_CACHE.get(world_dir)
    if hit and hit[0] == mts:
        return hit[1]
    recipes, machine, uses = {}, {}, {}
    for it in (_read_json(pc) or {}).get("items", []):
        sid = it.get("id")
        if not sid:
            continue
        res = [(r.get("id"), int(r.get("count") or 0)) for r in (it.get("res") or []) if r.get("id")]
        recipes[sid] = {"time": float(it.get("time") or 0), "count": int(it.get("count") or 1) or 1,
                        "tech": it.get("tech") or "", "group": it.get("group") or "",
                        "workbench": it.get("workbench") or "", "res": res}
        for rid, _n in res:
            uses.setdefault(rid, []).append(sid)
    for mc in (_read_json(pm) or {}).get("machines", []):
        for x in mc.get("items") or []:
            if x.get("product") and x.get("material"):
                machine.setdefault(x["product"], []).append((mc.get("id"), x["material"], x.get("energy")))
                uses.setdefault(x["material"], []).append(x["product"])
    data = {"recipes": recipes, "machine": machine, "uses": uses}
    _CRAFT_CACHE[world_dir] = (mts, data)
    return data


def craft_catalog(cfg):
    """Все предметы, которые можно получить крафтом или станком."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    cd = _craft_data(world_dir)
    if not cd:
        return {"ok": False, "error": "нет Data\\craft.json"}
    out = []
    for sid, r in cd["recipes"].items():
        out.append({"id": sid, "name": item_label(sid), "group": r["group"],
                    "workbench": item_label(r["workbench"]) if r["workbench"] else "",
                    "tech": r["tech"]})
    for sid, srcs in cd["machine"].items():
        if sid not in cd["recipes"]:
            out.append({"id": sid, "name": item_label(sid), "group": "machine",
                        "workbench": item_label(srcs[0][0]), "tech": ""})
    out.sort(key=lambda x: x["name"].lower())
    return {"ok": True, "items": out}


def _who_techs(world_dir, uid=None, clan_id=None):
    """-> (set техов, подпись) для проверки «изучено ли»: игрок — его techList,
    клан — объединение techList участников + клановые техи."""
    if uid not in (None, ""):
        raw = _read_json(_user_file(world_dir, int(uid)))
        nm = load_user_list(world_dir).get(int(uid)) or raw.get("name") or ("id %s" % uid)
        return set(raw.get("techList") or []), nm
    if clan_id not in (None, ""):
        c = next((x for x in _clans_raw(world_dir) if x.get("id") == int(clan_id)), None)
        if not c:
            return set(), ""
        s = set(c.get("tech") or [])
        for u in c.get("users") or []:
            s.update(_read_json(_user_file(world_dir, u.get("userId"))).get("techList") or [])
        return s, c.get("name") or ""
    return None, ""


def craft_plan(cfg, item, qty=1, uid=None, clan_id=None):
    """Раскладка предмета до сырья: дерево крафта, итоговое сырьё, промежуточные
    крафты, время, верстаки/станки и нужные технологии (с отметкой, изучены ли
    они у игрока ``uid`` или у клана ``clan_id``)."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    cd = _craft_data(world_dir)
    if not cd:
        return {"ok": False, "error": "нет Data\\craft.json"}
    try:
        qty = max(1, min(100000, int(qty or 1)))
    except (TypeError, ValueError):
        qty = 1
    slug = str(item or "").strip()
    if slug not in cd["recipes"] and slug not in cd["machine"]:
        by_id, by_name = _items_full(world_dir)
        low = slug.lower()
        cand = [s for s in list(cd["recipes"]) + list(cd["machine"])
                if s.lower() == low or item_label(s).lower() == low]
        if not cand:
            return {"ok": False, "error": "у «%s» нет рецепта крафта" % slug}
        slug = cand[0]
    rec, mach = cd["recipes"], cd["machine"]
    raw_tot, inter, techs, benches = {}, {}, {}, {}
    total_time = [0.0]

    def expand(sid, need, path):
        node = {"id": sid, "name": item_label(sid), "need": need}
        if sid in rec and sid not in path and len(path) < 14:
            r = rec[sid]
            crafts = -(-need // r["count"])
            node.update(via="craft", crafts=crafts, out=crafts * r["count"],
                        workbench=item_label(r["workbench"]) if r["workbench"] else "",
                        tech=r["tech"], time=round(crafts * r["time"], 1))
            total_time[0] += crafts * r["time"]
            if r["tech"]:
                techs[r["tech"]] = techs.get(r["tech"], 0) + crafts
            if r["workbench"]:
                benches[r["workbench"]] = True
            if path:
                x = inter.setdefault(sid, {"id": sid, "name": item_label(sid), "need": 0, "crafts": 0})
                x["need"] += need
                x["crafts"] += crafts
            node["children"] = [expand(rid, n * crafts, path | {sid}) for rid, n in r["res"]]
        elif sid in mach and len(path) < 14 and any(m[1] not in path for m in mach[sid]):
            mid, mat, energy = next(m for m in mach[sid] if m[1] not in path)
            node.update(via="machine", machine=item_label(mid), energy=energy,
                        alts=[item_label(m[1]) for m in mach[sid] if m[1] != mat])
            benches[mid] = True
            if path:
                x = inter.setdefault(sid, {"id": sid, "name": item_label(sid), "need": 0, "crafts": 0})
                x["need"] += need
            node["children"] = [expand(mat, need, path | {sid})]
        else:
            node["via"] = "raw"
            raw_tot[sid] = raw_tot.get(sid, 0) + need
        return node

    tree = expand(slug, qty, frozenset())
    known, who = _who_techs(world_dir, uid, clan_id)
    meta = tech_meta(world_dir)
    tech_list = []
    for tid in techs:
        # нужна вся цепочка до теха, не только он сам
        chain, cur, seen = [], tid, set()
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = _tech_parent(world_dir, cur)
        missing = [t for t in chain if known is not None and t not in known]
        tech_list.append({"id": tid, "label": tech_label(world_dir, tid),
                          "known": (tid in known) if known is not None else None,
                          "missing_chain": len(missing),
                          "missing_h": round(sum((meta.get(t) or {}).get("cost_min") or 0 for t in missing) / 60.0, 1)})
    tech_list.sort(key=lambda x: (x["known"] is True, x["label"]))
    return {
        "ok": True, "item": slug, "name": item_label(slug), "qty": qty, "tree": tree,
        "raw": sorted(({"id": k, "name": item_label(k), "count": v} for k, v in raw_tot.items()),
                      key=lambda x: -x["count"]),
        "intermediate": sorted(inter.values(), key=lambda x: x["name"].lower()),
        "time_s": round(total_time[0], 1),
        "benches": sorted(item_label(b) for b in benches),
        "techs": tech_list, "who": who,
        "used_in": sorted({item_label(u) for u in cd["uses"].get(slug, [])}),
    }


def _tech_parent(world_dir, tid):
    p = os.path.join(world_dir, "Data", "tech.json")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    hit = _TECH_PARENT_CACHE.get(p)
    if not hit or hit[0] != mt:
        hit = (mt, {it["id"]: it.get("parent") for it in (_read_json(p) or {}).get("items", []) if "id" in it})
        _TECH_PARENT_CACHE[p] = hit
    return hit[1].get(tid)


_TECH_PARENT_CACHE = {}


def tech_unlocks(world_dir):
    """{tech_id: [RU-название предмета, ...]} — что открывает технология (craft.json)."""
    cd = _craft_data(world_dir) if world_dir else None
    out = {}
    for sid, r in (cd or {}).get("recipes", {}).items():
        if r["tech"]:
            out.setdefault(r["tech"], []).append(item_label(sid))
    return out


# --- история кланов: панель сама снимает clans.json (у игры истории нет) -----
def clan_track_scan(cfg, state_path, events_path, points_path, point_every=3600):
    """Сравнить clans.json с прошлым снимком: события (вступил/ушёл/роль/
    клан-тех/переименование/создан/распущен) -> ``events_path`` (jsonl);
    раз в ``point_every`` секунд — точка рейтинга/CP/состава всех кланов ->
    ``points_path`` (jsonl). -> список новых событий."""
    world_dir = find_world_dir(cfg)
    if not world_dir or not os.path.exists(os.path.join(world_dir, "Data", "game", "clans.json")):
        return []
    names = load_user_list(world_dir)
    st = _read_json(state_path) or {}
    prev = st.get("clans")
    now = time.time()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = {}
    for c in _clans_raw(world_dir):
        cur[str(c.get("id"))] = {
            "name": c.get("name") or "", "rating": c.get("rating"), "cp": c.get("clanPoint"),
            "max": c.get("maxUserCount"), "tech": list(c.get("tech") or []),
            "users": {str(u.get("userId")): u.get("role") for u in c.get("users") or []},
        }
    events = []

    def ev(cid, kind, **kw):
        e = {"ts": ts, "clan": int(cid), "clan_name": (cur.get(cid) or prev.get(cid) or {}).get("name"), "kind": kind}
        e.update(kw)
        events.append(e)

    def uname(uid):
        return names.get(int(uid)) or ("id %s" % uid)

    if prev is not None:
        for cid, c in cur.items():
            p = prev.get(cid)
            if p is None:
                ev(cid, "created", size=len(c["users"]))
                continue
            for uid, role in c["users"].items():
                if uid not in p["users"]:
                    ev(cid, "joined", uid=int(uid), name=uname(uid))
                elif p["users"][uid] != role:
                    ev(cid, "role", uid=int(uid), name=uname(uid), role=_CLAN_ROLES_RU.get(role, role),
                       was=_CLAN_ROLES_RU.get(p["users"][uid], p["users"][uid]))
            for uid in p["users"]:
                if uid not in c["users"]:
                    ev(cid, "left", uid=int(uid), name=uname(uid))
            for t in c["tech"]:
                if t not in p["tech"]:
                    ev(cid, "tech", tech=t, label=tech_label(world_dir, t))
            if c["name"] != p["name"]:
                ev(cid, "renamed", was=p["name"])
            if (c["max"] or 0) > (p.get("max") or 0):
                ev(cid, "slots", max=c["max"], was=p.get("max"))
        for cid in prev:
            if cid not in cur:
                ev(cid, "disbanded")
    last_point = float(st.get("last_point") or 0)
    write_point = now - last_point >= point_every
    try:
        if events:
            os.makedirs(os.path.dirname(events_path), exist_ok=True)
            with io.open(events_path, "a", encoding="utf-8") as f:
                for e in events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            _rotate(events_path, 3_000_000)
        if write_point:
            os.makedirs(os.path.dirname(points_path), exist_ok=True)
            pt = {"ts": ts, "t": int(now), "c": {cid: [c["rating"], c["cp"], len(c["users"])]
                                                 for cid, c in cur.items()}}
            with io.open(points_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(pt, ensure_ascii=False, separators=(",", ":")) + "\n")
            _rotate(points_path, 5_000_000)
            last_point = now
        tmp = state_path + ".swtmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated": ts, "last_point": last_point, "clans": cur}, f,
                      ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, state_path)
    except OSError:
        logging.exception("clan_track: запись")
    return events


def clan_history(cfg, cid, events_path, points_path, days=60):
    """Графики рейтинга/CP/состава клана + журнал событий по нему."""
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad id"}
    since = time.time() - max(1, int(days or 60)) * 86400
    series = []
    for ln in _read_text(points_path, tail_bytes=4_000_000).splitlines():
        try:
            p = json.loads(ln)
        except ValueError:
            continue
        v = (p.get("c") or {}).get(str(cid))
        if v and p.get("t", 0) >= since:
            series.append({"t": p["t"], "rating": v[0], "cp": v[1], "size": v[2]})
    events = []
    for ln in _read_text(events_path, tail_bytes=2_000_000).splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if e.get("clan") == cid:
            events.append(e)
    return {"ok": True, "series": series, "events": events[-300:][::-1]}


# --- торговля: терминалы игроков (terminals.dt2) + магазины на картах (.dt) ----
_MAPSCAN_CACHE = {}  # map path -> (mtime, {"shops": [...], "items": {type: n}})


def _maps_scan(world_dir):
    """Один проход по всем map*.dt (кэш по mtime каждой карты): магазины и
    полный счёт предметов в сундуках/контейнерах. Общий для «Торговли» и
    «Экономики» — первый раз десятки секунд, дальше только изменённые карты."""
    shops, items, _veh = _maps_scan_full(world_dir)
    return shops, items


def vehicles_all(world_dir):
    """Весь транспорт на картах (ракеты/машины/лодки, стоящие на планетах) из того же
    прохода по картам, что и торговля: [{map, x, y, type, user_id, energy, health, units, cargo}]."""
    return _maps_scan_full(world_dir)[2]


def ground_vehicles(world_dir):
    """Транспорт на картах (машины, лодки, танки, амфибии, припаркованные ракеты): блоки с
    ``transport`` в map*.dt (из общего прохода по картам) + юниты-коробки в
    Data\\units\\bots*\\units.dat. -> [{map, x, y, box_type, owner, energy, health, units, cargo}]"""
    out = [{"map": v["map"], "x": v["x"], "y": v["y"], "box_type": v["type"], "owner": v.get("user_id"),
            "energy": v.get("energy"), "health": v.get("health"), "units": v.get("units"), "cargo": v.get("cargo") or []}
           for v in vehicles_all(world_dir)]
    ud = os.path.join(world_dir, "Data", "units")
    try:
        dirs = os.listdir(ud)
    except OSError:
        dirs = []
    for d in dirs:
        p = os.path.join(ud, d, "units.dat")
        if not d.startswith("bots") or not os.path.exists(p):
            continue
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        hit = _UNITSCAN_CACHE.get(p)
        if not hit or hit[0] != mt:
            r = mapdt.parse_units(p, world_dir=world_dir)
            hit = (mt, r.get("vehicles") or [])
            _UNITSCAN_CACHE[p] = hit
        out.extend(hit[1])
    return out


_UNITSCAN_CACHE = {}


def _maps_scan_full(world_dir):
    md = os.path.join(world_dir, "Data", "maps")
    shops, items, vehicles = [], {}, []
    try:
        files = sorted(os.listdir(md))
    except OSError:
        return shops, items, vehicles
    for f in files:
        m = re.match(r"map(\d+)\.dt$", f)
        if not m:
            continue
        fp = os.path.join(md, f)
        try:
            mt = os.path.getmtime(fp)
        except OSError:
            continue
        hit = _MAPSCAN_CACHE.get(fp)
        if not hit or hit[0] != mt or hit[1].get("vver") != 2:     # vver: транспорт из MapCell.Box (с 25.09)
            d = mapdt.parse(fp, world_dir=world_dir, list_shops=True)
            mid = int(m.group(1))
            val = {"shops": [dict(s, map=mid) for s in (d.get("shops") or [])],
                   "items": d.get("chest_items_all") or {},
                   "vehicles": [dict(v, map=mid) for v in (d.get("vehicles_list") or [])], "vver": 2} if d.get("ok") \
                else {"shops": [], "items": {}, "vehicles": [], "vver": 2}
            hit = (mt, val)
            _MAPSCAN_CACHE[fp] = hit
        shops.extend(hit[1]["shops"])
        vehicles.extend(hit[1]["vehicles"])
        for t, n in hit[1]["items"].items():
            items[t] = items.get(t, 0) + n
    return shops, items, vehicles


def _shops_all(world_dir):
    return _maps_scan(world_dir)[0]


def trade_report(cfg):
    """Все торговые предложения сервера: терминалы игроков (лот: отдаёт → хочет)
    и магазины на картах (слот: товар → цена). Первый проход по картам долгий
    (десятки секунд на больших мирах), дальше — кэш по mtime каждой карты."""
    if mapdt is None:
        return {"ok": False, "error": "модуль mapdt недоступен"}
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    t0 = time.time()
    names = load_user_list(world_dir)
    items = load_items(world_dir)
    st = server_time(world_dir)
    online = _online_now(world_dir)
    clan_of = {u.get("userId"): c.get("name") for c in _clans_raw(world_dir) for u in c.get("users") or []}

    def it(x):
        slug = items.get(x["type"])
        return {"id": x["type"], "name": item_label(slug) if slug else ("#%s" % x["type"]), "count": x["count"]}

    def who(uid):
        return {"id": uid, "name": names.get(uid) or ("id %s" % uid), "online": bool(online.get(uid)),
                "clan": clan_of.get(uid) or ""}

    offers, terminals, shops = [], [], []
    tp = os.path.join(world_dir, "Data", "game", "terminals.dt2")
    if os.path.exists(tp):
        td = mapdt.parse_terminals(tp, world_dir)
        for t in (td.get("terminals") or []) if td.get("ok") else []:
            row = {"owner": who(t["user_id"]), "sales": t["sales"], "lots": len(t["lots"]),
                   "idle_h": round((st - t["last_time"]) / 3600.0, 1) if st and t.get("last_time") else None,
                   "storage": [it(x) for x in t["storage"]]}
            terminals.append(row)
            for lot in t["lots"]:
                offers.append({"src": "terminal", "owner": row["owner"], "where": "",
                               "give": [it(x) for x in lot["items"]], "want": [it(x) for x in lot["cost"]]})
    for s in _shops_all(world_dir):
        row = {"owner": who(s.get("owner") or 0), "map": s["map"], "x": s["x"], "y": s["y"],
               "sales": s["sales"], "slots": len(s["goods"]), "storage": [it(x) for x in s["storage"]]}
        shops.append(row)
        for i, g in enumerate(s["goods"]):
            if not g.get("count"):
                continue
            pr = s["price"][i] if i < len(s["price"]) else None
            offers.append({"src": "shop", "owner": row["owner"], "where": "map %d @ %d,%d" % (s["map"], s["x"], s["y"]),
                           "give": [it(g)], "want": [it(pr)] if pr else []})
    # цена за единицу — только для простых лотов «1 товар за 1 вид оплаты»
    for o in offers:
        if len(o["give"]) == 1 and len(o["want"]) == 1 and o["give"][0]["count"]:
            o["unit"] = round(o["want"][0]["count"] / float(o["give"][0]["count"]), 4)
    terminals.sort(key=lambda x: -x["sales"])
    shops.sort(key=lambda x: -x["sales"])
    return {"ok": True, "offers": offers, "terminals": terminals, "shops": shops,
            "scan_sec": round(time.time() - t0, 1)}


# --- экономика: сколько чего в мире (игроки + сундуки на картах + торговля) ----
def _player_items(world_dir):
    """{uid: {type: count}} — склад (user.Inventory) + при себе (unit.Inventory)."""
    d = os.path.join(world_dir, "Data", "users")
    out = {}
    try:
        listing = os.listdir(d)
    except OSError:
        return out
    for nm in listing:
        m = _USER_FILE_RX.match(nm)
        if not m:
            continue
        raw = _read_json(os.path.join(d, nm))
        try:
            uid = int(raw.get("id") if raw.get("id") is not None else m.group(1))
        except (TypeError, ValueError):
            continue
        inv = {}
        for it in (raw.get("Inventory") or {}).get("items") or []:
            if it.get("count"):
                inv[it.get("type")] = inv.get(it.get("type"), 0) + int(it["count"])
        if raw.get("unitId") is not None:
            unit = _read_json(os.path.join(world_dir, "Data", "units", "unit%s.json" % raw["unitId"]))
            for it in (unit.get("Inventory") or {}).get("items") or []:
                if it.get("count"):
                    inv[it.get("type")] = inv.get(it.get("type"), 0) + int(it["count"])
        if inv:
            out[uid] = inv
    return out


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0) if n else None


def economy_report(cfg, hist_path):
    """Сводка по каждому предмету: у игроков / в сундуках на картах / в торговле,
    держатели, медианный курс по предложениям, изменение за 1 и 7 дней (по
    ежедневным снимкам ``hist_path``; снимок за сегодня дописывается здесь же)."""
    if mapdt is None:
        return {"ok": False, "error": "модуль mapdt недоступен"}
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    t0 = time.time()
    names = load_user_list(world_dir)
    items = load_items(world_dir)
    per = _player_items(world_dir)
    _shops, cont = _maps_scan(world_dir)
    tr = trade_report(cfg)
    in_players, holders = {}, {}
    for uid, inv in per.items():
        for t, n in inv.items():
            in_players[t] = in_players.get(t, 0) + n
            holders.setdefault(t, []).append((n, uid))
    in_trade = {}

    def add_tr(lst):
        for x in lst:
            in_trade[x["id"]] = in_trade.get(x["id"], 0) + x["count"]
    for o in tr.get("offers") or []:
        add_tr(o["give"])
    for t in (tr.get("terminals") or []) + (tr.get("shops") or []):
        add_tr(t["storage"])
    # курс: цена 1 предмета в самой частой валюте его предложений
    rates = {}
    for o in tr.get("offers") or []:
        if o.get("unit"):
            rates.setdefault(o["give"][0]["id"], {}).setdefault(o["want"][0]["id"], []).append(o["unit"])
    hist = []
    for ln in _read_text(hist_path, tail_bytes=6_000_000).splitlines():
        try:
            hist.append(json.loads(ln))
        except ValueError:
            continue
    now = time.time()

    def total_at(age_days):
        best = None
        for h in hist:
            if h.get("t", 0) <= now - age_days * 86400 + 3600:
                best = h
        return (best or {}).get("totals")
    ago1, ago7 = total_at(1), total_at(7)
    all_ids = set(in_players) | set(cont) | set(in_trade)
    out, totals = [], {}
    for t in all_ids:
        pn, cn, tn = in_players.get(t, 0), cont.get(t, 0), in_trade.get(t, 0)
        tot = pn + cn + tn
        if not tot:
            continue
        totals[str(t)] = tot
        slug = items.get(t)
        hs = sorted(holders.get(t, []), reverse=True)
        rate = None
        if t in rates:
            pay, vals = max(rates[t].items(), key=lambda kv: len(kv[1]))
            ps = items.get(pay)
            rate = {"pay_id": pay, "pay": item_label(ps) if ps else "#%s" % pay,
                    "median": round(_median(vals), 4), "n": len(vals)}
        out.append({
            "id": t, "name": item_label(slug) if slug else "#%s" % t,
            "players": pn, "containers": cn, "trade": tn, "total": tot,
            "holders": len(hs),
            "top": [{"id": u, "name": names.get(u) or ("id %s" % u), "count": n} for n, u in hs[:5]],
            "rate": rate,
            "d1": (tot - (ago1 or {}).get(str(t), 0)) if ago1 is not None else None,
            "d7": (tot - (ago7 or {}).get(str(t), 0)) if ago7 is not None else None,
        })
    out.sort(key=lambda x: -x["total"])
    today = datetime.now().strftime("%Y-%m-%d")
    if not hist or hist[-1].get("date") != today:
        try:
            os.makedirs(os.path.dirname(hist_path), exist_ok=True)
            with io.open(hist_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"date": today, "t": int(now), "totals": totals},
                                   separators=(",", ":")) + "\n")
            _rotate(hist_path, 20_000_000)
        except OSError:
            logging.exception("economy: снимок")
    return {"ok": True, "items": out, "players_scanned": len(per), "snapshots": len(hist) + (0 if hist and hist[-1].get("date") == today else 1),
            "scan_sec": round(time.time() - t0, 1)}


# --- активность и удержание -------------------------------------------------
def _level_bucket(lv):
    try:
        lv = int(lv or 0)
    except (TypeError, ValueError):
        lv = 0
    if lv <= 0:
        return "0"
    if lv <= 5:
        return "1-5"
    lo = (lv - 1) // 10 * 10 + 1 if lv > 10 else 6
    hi = 10 if lv <= 10 else lo + 9
    return "%d-%d" % (lo, hi)


def activity_report(cfg, churn_days=14):
    """Регистрации/активные по дням, когорты по неделям с удержанием
    D1/D7/D30, тепловая карта онлайна (день недели × час, среднее число
    игроков за 4 недели), на каком уровне бросают (не заходили ``churn_days``)."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    per, events = parse_analytics(os.path.join(world_dir, "analytics.txt"))
    if not events:
        return {"ok": False, "error": "analytics.txt пуст"}
    now = max(e["epoch"] for e in events if e["epoch"]) or time.time()
    reg, enters = {}, collections.defaultdict(list)
    day_reg, day_act = collections.Counter(), collections.defaultdict(set)
    open_s, sessions = {}, []
    for e in events:
        ep = e["epoch"]
        if not ep:
            continue
        d = datetime.fromtimestamp(ep).strftime("%Y-%m-%d")
        if e["kind"] == "register":
            reg.setdefault(e["id"], ep)
            day_reg[d] += 1
        elif e["kind"] == "enter":
            enters[e["id"]].append(ep)
            day_act[d].add(e["id"])
            open_s[e["id"]] = ep
        elif e["kind"] == "exit":
            st = open_s.pop(e["id"], None)
            if st is None and e.get("secs"):
                st = ep - e["secs"]
            if st:
                sessions.append((st, ep))
    for uid, st in open_s.items():          # ещё в игре
        if per.get(uid, {}).get("online"):
            sessions.append((st, now))
    # дни
    days = []
    for i in range(59, -1, -1):
        d = datetime.fromtimestamp(now - i * 86400).strftime("%Y-%m-%d")
        days.append({"d": d, "reg": day_reg.get(d, 0), "active": len(day_act.get(d, ()))})
    # когорты по неделям
    def returned(uid, a, b):
        t0 = reg[uid]
        return any(t0 + a * 86400 <= t < t0 + b * 86400 for t in enters.get(uid, ()))
    cohorts = {}
    for uid, t0 in reg.items():
        if t0 < now - 12 * 7 * 86400:
            continue
        wk = datetime.fromtimestamp(t0).strftime("%G-W%V")
        c = cohorts.setdefault(wk, {"week": wk, "size": 0, "d1": [0, 0], "d7": [0, 0], "d30": [0, 0]})
        c["size"] += 1
        for key, a, b in (("d1", 1, 2), ("d7", 7, 14), ("d30", 30, 60)):
            if now - t0 >= b * 86400 or (key == "d1" and now - t0 >= 2 * 86400):
                c[key][1] += 1
                if returned(uid, a, b):
                    c[key][0] += 1
    # тепловая карта: среднее одновременно онлайн по (день недели, час) за 4 недели
    since = now - 28 * 86400
    heat = [[0.0] * 24 for _ in range(7)]
    for st, en in sessions:
        st, en = max(st, since), min(en, now)
        t = st
        while t < en:
            dt = datetime.fromtimestamp(t)
            nxt = min(en, t - dt.minute * 60 - dt.second + 3600)
            heat[dt.weekday()][dt.hour] += (nxt - t) / 3600.0
            t = nxt if nxt > t else t + 60
    heat = [[round(v / 4.0, 2) for v in row] for row in heat]
    # отток по уровням
    det = load_user_details(world_dir)
    buckets = {}
    quick = 0
    for uid, u in per.items():
        lv = (det.get(uid) or {}).get("level")
        b = _level_bucket(lv)
        row = buckets.setdefault(b, {"bucket": b, "active": 0, "churned": 0})
        gone = (now - (u.get("last_epoch") or 0)) > churn_days * 86400 and not u.get("online")
        row["churned" if gone else "active"] += 1
        if gone and float((det.get(uid) or {}).get("playtime_h") or 0) < 1:
            quick += 1

    def bkey(b):
        return int(b.split("-")[0])
    lv_rows = sorted(buckets.values(), key=lambda r: bkey(r["bucket"]))
    return {"ok": True, "days": days, "cohorts": sorted(cohorts.values(), key=lambda c: c["week"]),
            "heat": heat, "levels": lv_rows, "churn_days": churn_days, "quit_first_hour": quick,
            "players": len(per)}


# --- лидерборды ---------------------------------------------------------------
_RICH_ITEMS = ("platinum_coin", "gold_coin", "silver_coin", "tech_booster")


def leaderboards(cfg, tt_log, clan_points_path, top=10):
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    names = load_user_list(world_dir)
    items = load_items(world_dir)
    slug2id = {v: k for k, v in items.items()}
    per = _player_items(world_dir)

    def nm(u):
        return names.get(u) or ("id %s" % u)
    rich = []
    for slug in _RICH_ITEMS:
        tid = slug2id.get(slug)
        if tid is None:
            continue
        rows = sorted(((inv.get(tid, 0), u) for u, inv in per.items() if inv.get(tid)), reverse=True)[:top]
        rich.append({"item": item_label(slug), "rows": [{"id": u, "name": nm(u), "v": n} for n, u in rows]})
    # торговцы: продажи терминалов (+ магазины из кэша карт, если уже разобраны)
    sales = collections.Counter()
    tp = os.path.join(world_dir, "Data", "game", "terminals.dt2")
    if mapdt and os.path.exists(tp):
        td = mapdt.parse_terminals(tp, world_dir)
        for t in td.get("terminals") or []:
            sales[t["user_id"]] += t["sales"]
    for _mt, val in list(_MAPSCAN_CACHE.values()):
        for sh in val.get("shops") or []:
            if sh.get("owner"):
                sales[sh["owner"]] += sh.get("sales") or 0
    traders = [{"id": u, "name": nm(u), "v": n} for u, n in sales.most_common(top) if n]
    # исследователи: за 7 дней по журналу трекинга + всего часов исследований
    week = time.time() - 7 * 86400
    gained = collections.Counter()
    for ln in _read_text(tt_log, tail_bytes=3_000_000).splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if e.get("kind") != "tech_gained":
            continue
        try:
            ts = datetime.strptime(e["ts"], "%Y-%m-%d %H:%M:%S").timestamp()
        except (KeyError, ValueError):
            continue
        if ts >= week:
            gained[e["uid"]] += int(e.get("count") or 0)
    research_week = [{"id": u, "name": nm(u), "v": n} for u, n in gained.most_common(top)]
    tcost = _load_ref(world_dir, "tech.json", "id", "cost")
    det = load_user_details(world_dir)
    rh = sorted(((round(sum(tcost.get(t) or 0 for t in d.get("techs") or []) / 60.0, 1), u)
                 for u, d in det.items()), reverse=True)[:top]
    research_total = [{"id": u, "name": nm(u), "v": h} for h, u in rh if h]
    # кланы: рейтинг сейчас и рост за 7 дней (по точкам истории кланов)
    pts = []
    for ln in _read_text(clan_points_path, tail_bytes=4_000_000).splitlines():
        try:
            pts.append(json.loads(ln))
        except ValueError:
            continue
    old = None
    for p in pts:
        if p.get("t", 0) <= time.time() - 7 * 86400 + 3600:
            old = p
    old = old or (pts[0] if pts else None)
    clans = []
    for c in _clans_raw(world_dir):
        cid = str(c.get("id"))
        was = ((old or {}).get("c") or {}).get(cid)
        clans.append({"id": c.get("id"), "name": c.get("name"), "rating": c.get("rating"),
                      "growth": (c.get("rating") or 0) - was[0] if was else None,
                      "since": (old or {}).get("ts")})
    clans.sort(key=lambda x: -(x["growth"] if x["growth"] is not None else -1e18))
    return {"ok": True, "rich": rich, "traders": traders, "research_week": research_week,
            "research_total": research_total, "clans": clans[:top]}


# --- инструменты админа (всё — только оффлайн, с бэкапом; вызывать из-под пароля)
def _resolve_targets(cfg, spec):
    """``spec``: список id / "clan:N" / "active:D" (заходили за D дней) — через
    запятую или пробел. -> (sorted uids, ошибка|None)."""
    world_dir = find_world_dir(cfg)
    out = set()
    for tok in re.split(r"[\s,;]+", str(spec or "").strip()):
        if not tok:
            continue
        if tok.lower() in ("all", "*", "все"):
            out.update(load_user_list(world_dir).keys())
            continue
        if tok.startswith("clan:"):
            try:
                cid = int(tok[5:])
            except ValueError:
                return [], "неверный клан: %s" % tok
            c = next((x for x in _clans_raw(world_dir) if x.get("id") == cid), None)
            if not c:
                return [], "нет клана %s" % cid
            out.update(u.get("userId") for u in c.get("users") or [])
        elif tok.startswith("active:"):
            try:
                days = float(tok[7:])
            except ValueError:
                return [], "неверный период: %s" % tok
            per, _ev = parse_analytics(os.path.join(world_dir, "analytics.txt"))
            lim = time.time() - days * 86400
            out.update(u for u, x in per.items() if (x.get("last_epoch") or 0) >= lim)
        else:
            try:
                out.add(int(tok))
            except ValueError:
                return [], "неверный id: %s" % tok
    return sorted(u for u in out if u is not None), None


def mass_give(cfg, spec, item, count):
    uids, err = _resolve_targets(cfg, spec)
    if err:
        return {"ok": False, "error": err}
    if not uids:
        return {"ok": False, "error": "список игроков пуст"}
    if len(uids) > 5000:
        return {"ok": False, "error": "слишком много игроков (%d > 5000)" % len(uids)}
    names = load_user_list(find_world_dir(cfg))
    res = []
    for u in uids:
        r = give_stash_items(cfg, u, item, count)
        res.append({"id": u, "name": names.get(u) or ("id %s" % u), "ok": bool(r.get("ok")),
                    "error": r.get("error"), "backup": r.get("backup")})
    return {"ok": True, "done": sum(1 for r in res if r["ok"]), "total": len(res), "results": res}


def clan_give_tech(cfg, clan_id, techs):
    """Техи клану: клановые (tech.json isClan) — в список клана ``clans.json → tech`` (только при
    остановленной игре: кланы игра держит в памяти и перезапишет файл); обычные — каждому
    участнику в личный techList (оффлайн-игрокам)."""
    wd = find_world_dir(cfg)
    if not wd:
        return {"ok": False, "error": "каталог мира не найден"}
    tj = {it["id"]: it for it in (_read_json(os.path.join(wd, "Data", "tech.json")) or {}).get("items", []) if "id" in it}
    reqs = [x.strip() for x in re.split(r"[\s,]+", str(techs or "")) if x.strip()]
    if not reqs:
        return {"ok": False, "error": "не указаны техи"}
    bad = [x for x in reqs if x not in tj]
    if bad:
        return {"ok": False, "error": "нет таких техов: %s" % ", ".join(bad[:10])}
    clan_t = [x for x in reqs if tj[x].get("isClan")]
    pers_t = [x for x in reqs if not tj[x].get("isClan")]
    out = {"ok": True, "done": 0, "total": 0, "results": [], "clan_added": [], "clan_error": None}
    if clan_t:
        r = clan_add_tech(cfg, clan_id, clan_t)
        if r.get("ok"):
            out["clan_added"] = r.get("added") or []
            out["backup"] = r.get("backup")
        else:
            out["clan_error"] = r.get("error")
            if not pers_t:
                return {"ok": False, "error": r.get("error")}
    if pers_t:
        uids, err = _resolve_targets(cfg, "clan:%s" % clan_id)
        if err:
            return {"ok": False, "error": err}
        names = load_user_list(wd)
        for u in uids:
            r = player_add_tech(cfg, u, " ".join(pers_t))
            out["results"].append({"id": u, "name": names.get(u) or ("id %s" % u), "ok": bool(r.get("ok")),
                                   "error": r.get("error"), "added": r.get("added")})
        out["done"] = sum(1 for r in out["results"] if r["ok"])
        out["total"] = len(out["results"])
    return out


def clan_add_tech(cfg, clan_id, techs):
    """Добавить клановые технологии в ``Data\\game\\clans.json`` (бэкап перед записью). Только
    когда игра не запущена — иначе она перезапишет файл своим состоянием из памяти."""
    wd = find_world_dir(cfg)
    if not wd:
        return {"ok": False, "error": "каталог мира не найден"}
    if game_running():
        return {"ok": False, "error": "игра запущена — клановые техи можно выдать только при остановленном сервере"}
    path = os.path.join(wd, "Data", "game", "clans.json")
    try:
        cid = int(clan_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "неверный клан"}
    mt = os.path.getmtime(path)
    raw = _read_json(path) or {}
    c = next((x for x in raw.get("clans") or [] if x.get("id") == cid), None)
    if not c:
        return {"ok": False, "error": "нет клана %s" % cid}
    cur = c.setdefault("tech", [])
    added = [t for t in techs if t not in cur]
    cur.extend(added)
    if game_running():
        return {"ok": False, "error": "игра запустилась — запись отменена"}
    bak = _game_edit_backup(cfg, path)
    ok, err = _write_json_compact(path, raw, mt)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True, "added": added, "backup": os.path.basename(os.path.dirname(bak))}


def player_backups(cfg, uid):
    """Бэкапы файлов игрока, которые панель делала перед правками
    (``logs\\game_edits\\<ts>\\user<N>.json`` / ``unit<id>.json``)."""
    world_dir = find_world_dir(cfg)
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return {"ok": False, "error": "неверный id"}
    raw = _read_json(_user_file(world_dir, uid))
    unit_id = raw.get("unitId")
    wanted = {"user%d.json" % uid: "stash"}
    if unit_id is not None:
        wanted["unit%s.json" % unit_id] = "carry"
    base = os.path.join(cfg.get("base_dir", os.path.dirname(os.path.abspath(__file__))), "logs", "game_edits")
    items = load_items(world_dir)
    out = []
    try:
        dirs = sorted(os.listdir(base), reverse=True)
    except OSError:
        dirs = []
    for d in dirs:
        for fn, where in wanted.items():
            fp = os.path.join(base, d, fn)
            if os.path.isfile(fp):
                inv = (_read_json(fp).get("Inventory") or {}).get("items") or []
                out.append({"dir": d, "file": fn, "where": where, "entries": len(inv),
                            "items": _name_inv(inv, items)[:60]})
        if len(out) >= 100:
            break
    return {"ok": True, "id": uid, "backups": out}


def restore_inventory(cfg, uid, backup_dir, file):
    """Вернуть ``Inventory`` игрока из бэкапа панели (только оффлайн; текущее
    состояние перед этим тоже бэкапится — откат обратим)."""
    world_dir = find_world_dir(cfg)
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return {"ok": False, "error": "неверный id"}
    if not re.match(r"^[0-9_]+$", str(backup_dir or "")) or not re.match(r"^(user|unit)\d+\.json$", str(file or "")):
        return {"ok": False, "error": "неверный бэкап"}
    raw = _read_json(_user_file(world_dir, uid))
    if file == "user%d.json" % uid:
        where = "stash"
    elif raw.get("unitId") is not None and file == "unit%s.json" % raw["unitId"]:
        where = "carry"
    else:
        return {"ok": False, "error": "бэкап не от этого игрока"}
    base = os.path.join(cfg.get("base_dir", os.path.dirname(os.path.abspath(__file__))), "logs", "game_edits")
    bak = _read_json(os.path.join(base, backup_dir, file))
    if "Inventory" not in bak:
        return {"ok": False, "error": "в бэкапе нет инвентаря"}
    if not _is_offline(world_dir, uid):
        return {"ok": False, "error": "игрок сейчас онлайн — откат только для оффлайн"}
    path, mt, root, inv = _inv_container(world_dir, uid, where)
    if inv is None:
        return {"ok": False, "error": root}
    root["Inventory"] = bak["Inventory"]
    if not _is_offline(world_dir, uid):
        return {"ok": False, "error": "игрок зашёл в игру — запись отменена"}
    cur_bak = _game_edit_backup(cfg, path)
    ok, err = _write_json_compact(path, root, mt)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True, "id": uid, "where": where, "from": backup_dir,
            "backup": os.path.basename(os.path.dirname(cur_bak))}


# --- флот игроков ---------------------------------------------------------------
def _merge_cargo(cargo, items):
    tot = collections.OrderedDict()
    for c in cargo:
        tot[c["type"]] = tot.get(c["type"], 0) + c["count"]
    return [{"name": item_label(items.get(t)) if items.get(t) else "#%s" % t, "id": items.get(t) or "", "count": n}
            for t, n in tot.items()]


_SPACE_KIND_RU = {"planet": "планета", "satellite": "спутник", "asteroid": "астероид"}


def nearest_space_object(cfg, star_id, x, y, max_dist=50_000):
    """Ближайший именованный объект звёздной системы -> («планета Имя», расстояние) или
    (None, расстояние), если дальше ``max_dist`` (корабль в пути между системами)."""
    try:
        objs = space_objects(cfg, star_id).get("objects") or []
    except Exception:  # noqa: BLE001
        objs = []
    best, dist = None, None
    for o in objs:
        d = math.hypot(o["x"] - x, o["y"] - y)
        if dist is None or d < dist:
            best, dist = o, d
    if best is None:
        return None, None
    label = ("%s %s" % (_SPACE_KIND_RU.get(best.get("kind"), ""), best["name"])).strip()
    return (label if dist <= max_dist else None), round(dist)


def season_rating(cfg):
    """Весь сезонный рейтинг (Data/users/rating.json, orderRating > 0) с местами, разрывами,
    наградой по месту и полученными наградами (Logs/reward_order.txt) — для админки."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    raw = _read_json(os.path.join(world_dir, "Data", "users", "rating.json"), default={}) or {}
    names = load_user_list(world_dir)
    clan_of = {u.get("userId"): c.get("name") for c in _clans_raw(world_dir) for u in c.get("users") or []}
    got, payouts = collections.defaultdict(list), collections.OrderedDict()
    for ln in _read_text(os.path.join(world_dir, "Logs", "reward_order.txt")).splitlines():
        m = _REWARD_RX.match(ln)
        if m:
            got[int(m.group(2))].append(int(m.group(3)))
            payouts[m.group(1)] = payouts.get(m.group(1), 0) + 1
    users = raw.get("users") or []
    rows = sorted((u for u in users if (u.get("orderRating") or 0) > 0), key=lambda u: -(u.get("orderRating") or 0))
    out, prev = [], None
    for i, u in enumerate(rows):
        uid, pts = u.get("userId"), u.get("orderRating") or 0
        g = got.get(uid) or []
        out.append({"place": i + 1, "id": uid, "name": names.get(uid) or ("id %s" % uid), "clan": clan_of.get(uid) or "",
                    "level": u.get("level"), "points": pts, "rating": u.get("rating"),
                    "gap_prev": (prev - pts) if prev is not None else None,
                    "reward": REWARD_TIERS[i] if i < len(REWARD_TIERS) else 0,
                    "rewards_n": len(g), "rewards_sum": sum(g)})
        prev = pts
    return {"ok": True, "rows": out, "total_users": len(users), "with_points": len(rows),
            "tiers": list(REWARD_TIERS), "payouts": [{"ts": k, "n": v} for k, v in list(payouts.items())[-10:]][::-1]}


def space_fleet(cfg):
    """Корабли в космосе по владельцам (из space\\units.dt) + станции игроков
    (Data\\stations\\station*.json)."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    su = space_units(cfg)
    items = load_items(world_dir)
    clan_of = {u.get("userId"): c.get("name") for c in _clans_raw(world_dir) for u in c.get("users") or []}
    names = load_user_list(world_dir)
    owners = {}
    for sh in (su.get("ships") or []) if su.get("ok") else []:
        uid = sh.get("user_id") or 0
        o = owners.setdefault(uid, {"id": uid, "name": sh.get("name") or "—", "clan": clan_of.get(uid) or "",
                                    "ships": []})
        near, dist = nearest_space_object(cfg, sh.get("star_id") or 1, sh["x"], sh["y"])
        sname = star_name(cfg, sh.get("star_id") or 1)
        o["ships"].append({"id": sh["id"], "model": sh["box_name"], "star": sh.get("star_id"),
                           "x": sh["x"], "y": sh["y"], "moving": sh["moving"], "speed": sh.get("speed"),
                           "near": near, "near_dist": dist, "star_name": sname,
                           "health": sh.get("health"), "aboard": sh.get("aboard"),
                           "cargo": _merge_cargo(sh.get("cargo") or [], items)})
    stations = []
    sd = os.path.join(world_dir, "Data", "stations")
    try:
        for f in sorted(os.listdir(sd)):
            if not f.endswith(".json"):
                continue
            st = _read_json(os.path.join(sd, f))
            uid = st.get("userId")
            pos = st.get("position") or {}
            stations.append({"id": st.get("id"), "name": st.get("name") or "", "owner": {"id": uid, "name": names.get(uid) or ("id %s" % uid)},
                             "clan": next((c.get("name") for c in _clans_raw(world_dir) if c.get("id") == st.get("clanId")), "") if st.get("clanId") else "",
                             "star": st.get("starId"), "x": round(pos.get("x") or 0), "y": round(pos.get("y") or 0),
                             "size": "%sx%s" % ((st.get("size") or {}).get("x"), (st.get("size") or {}).get("y"))})
    except OSError:
        pass
    fleet = sorted(owners.values(), key=lambda o: (-len(o["ships"]), o["name"].lower()))
    return {"ok": True, "owners": fleet, "ships": sum(len(o["ships"]) for o in fleet),
            "stations": stations, "note": su.get("note") if su.get("ok") else su.get("error")}


def economy_snapshot_due(hist_path):
    """True, если за сегодня ещё нет снимка экономики (фоновый поток зовёт
    economy_report раз в сутки — чтобы динамика копилась без открытия вкладки)."""
    tail = _read_text(hist_path, tail_bytes=2_000_000).strip().splitlines()
    if not tail:
        return True
    try:
        return json.loads(tail[-1]).get("date") != datetime.now().strftime("%Y-%m-%d")
    except ValueError:
        return True


def economy_item_history(hist_path, item_id):
    try:
        key = str(int(item_id))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad id"}
    series = []
    for ln in _read_text(hist_path, tail_bytes=20_000_000).splitlines():
        try:
            h = json.loads(ln)
        except ValueError:
            continue
        series.append({"t": h.get("t"), "v": (h.get("totals") or {}).get(key, 0)})
    return {"ok": True, "series": series}


# --- мониторинг нарушений: всплески инвентаря, быстрые исследования, цены ----
_SUSP_WATCH_DEFAULT = {"silver_coin": 50000, "gold_coin": 5000, "platinum_coin": 1000, "tech_booster": 5}


def _twink_links(cfg):
    """{uid: set(uid)} — связанные аккаунты (одинаковый пароль или общий IP)."""
    try:
        d = twink_report(cfg, 2)
    except Exception:  # noqa: BLE001
        logging.exception("susp: twink_report")
        return {}
    links = {}
    for g in (d.get("code_groups") or []) + (d.get("groups") or []):
        ids = [a.get("id") for a in g.get("accounts") or [] if a.get("id") is not None]
        for a in ids:
            links.setdefault(a, set()).update(x for x in ids if x != a)
    return links


def inv_track_scan(cfg, state_path, log_path):
    """Сравнить инвентари всех игроков с прошлым проходом. Всплеск = рост
    предмета из «наблюдаемых» (монеты, бустеры; ``players.suspicious.watch``)
    выше порога, либо любого предмета на ≥ max(generic_min, 10× было). Для
    каждого всплеска ищем «доноров» — у кого в тот же интервал этого предмета
    убыло сопоставимо, и помечаем, если донор — связанный аккаунт (твинк)."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return []
    sc = _cfg_pl(cfg).get("suspicious", {}) or {}
    watch = dict(_SUSP_WATCH_DEFAULT)
    watch.update(sc.get("watch") or {})
    gen_min = int(sc.get("generic_min", 2000))
    items = load_items(world_dir)
    slug2id = {v: k for k, v in items.items()}
    watch_id = {slug2id[k]: v for k, v in watch.items() if k in slug2id}
    cur = _player_items(world_dir)
    st = _read_json(state_path) or {}
    prev = {int(k): {int(t): n for t, n in v.items()} for k, v in (st.get("inv") or {}).items()}
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    events = []
    if prev:
        names = load_user_list(world_dir)
        spikes = []
        for uid, inv in cur.items():
            p = prev.get(uid)
            if p is None:
                continue
            for t, n in inv.items():
                gain = n - p.get(t, 0)
                if gain <= 0:
                    continue
                thr = watch_id.get(t)
                if (thr is not None and gain >= thr) or gain >= max(gen_min, 10 * p.get(t, 0)):
                    spikes.append((uid, t, gain, p.get(t, 0), n))
        links = _twink_links(cfg) if spikes else {}
        for uid, t, gain, was, now_n in spikes:
            donors = []
            for ouid, pinv in prev.items():
                if ouid == uid:
                    continue
                loss = pinv.get(t, 0) - (cur.get(ouid) or {}).get(t, 0)
                if loss >= gain * 0.5:
                    donors.append({"id": ouid, "name": names.get(ouid) or ("id %s" % ouid), "loss": loss,
                                   "twink": ouid in links.get(uid, ())})
            slug = items.get(t)
            events.append({"ts": ts, "kind": "inv_spike", "uid": uid, "name": names.get(uid) or ("id %s" % uid),
                           "item": item_label(slug) if slug else "#%s" % t, "item_id": t,
                           "gain": gain, "was": was, "now": now_n, "watched": t in watch_id,
                           "donors": sorted(donors, key=lambda x: -x["loss"])[:5],
                           "twink": any(x["twink"] for x in donors)})
    try:
        tmp = state_path + ".swtmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated": ts, "inv": {str(u): {str(t): n for t, n in v.items()} for u, v in cur.items()}},
                      f, separators=(",", ":"))
        os.replace(tmp, state_path)
    except OSError:
        logging.exception("susp: state")
    _susp_append(log_path, events)
    return events


def research_check(cfg, tt_events, prev_research, elapsed_s, log_path):
    """Быстрые исследования: за интервал ``elapsed_s`` игрок получил техи общей
    стоимостью больше, чем позволяет время + потраченные бустеры (1 бустер =
    1 ч). Тех, который уже шёл на прошлом проходе, не считаем (он мог почти
    закончиться). Выдача теха админом тоже попадёт сюда — это видно в аудите."""
    world_dir = find_world_dir(cfg)
    if not world_dir or not elapsed_s:
        return []
    meta = tech_meta(world_dir)
    boosts = {}
    for e in tt_events:
        if e.get("kind") == "booster_spent":
            boosts[e["uid"]] = boosts.get(e["uid"], 0) + int(e.get("delta") or 0)
    events = []
    for e in tt_events:
        if e.get("kind") != "tech_gained":
            continue
        uid = e["uid"]
        techs = [t for t in (e.get("techs") or []) if t != prev_research.get(str(uid))]
        cost = sum((meta.get(t) or {}).get("cost_min") or 0 for t in techs)
        allowed = elapsed_s / 60.0 + boosts.get(uid, 0) * 60
        if cost > allowed * 1.3 + 20:
            events.append({"ts": e["ts"], "kind": "fast_research", "uid": uid, "name": e.get("name"),
                           "techs": [tech_label(world_dir, t) for t in techs], "cost_min": round(cost),
                           "allowed_min": round(allowed), "boosters": boosts.get(uid, 0)})
    _susp_append(log_path, events)
    return events


def _susp_append(log_path, events):
    if not events:
        return
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with io.open(log_path, "a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        _rotate(log_path, 3_000_000)
    except OSError:
        logging.exception("susp: log")


def suspicious_read(cfg, log_path, trade=None, limit=500):
    """Журнал подозрений + аномальные цены в торговле (курс в 10+ раз от
    медианы той же пары «товар → оплата», если у пары ≥3 предложений)."""
    rows = []
    for ln in _read_text(log_path, tail_bytes=2_000_000).splitlines():
        try:
            rows.append(json.loads(ln))
        except ValueError:
            continue
    anomalies = None
    if trade and trade.get("ok"):
        pairs = {}
        for o in trade.get("offers") or []:
            if o.get("unit"):
                pairs.setdefault((o["give"][0]["id"], o["want"][0]["id"]), []).append(o)
        anomalies = []
        for (_g, _w), lst in pairs.items():
            if len(lst) < 3:
                continue
            med = _median([o["unit"] for o in lst])
            for o in lst:
                if med and (o["unit"] < med / 10.0 or o["unit"] > med * 10.0):
                    anomalies.append({"owner": o["owner"], "src": o["src"], "where": o["where"],
                                      "give": o["give"], "want": o["want"], "unit": o["unit"],
                                      "median": round(med, 4), "x": round(o["unit"] / med, 3)})
        anomalies.sort(key=lambda a: min(a["x"], 1.0 / a["x"] if a["x"] else 0))
    return {"ok": True, "events": rows[-limit:][::-1], "total": len(rows), "trade_anomalies": anomalies}


# --- человеко-читаемые подписи к блокам/абилкам ------------------------------
# blocks.json/ability.json дают только английский slug (id/name). Тексты — из
# клиентской локализации: resources.assets содержит TextAsset "lang" — XML
# <Lang><Section id="blocks|ability|...">...<str id="<slug>"><ru>...</ru></str>
# — собран офлайн тем же приёмом, что и _TECH_NAMES (см. tech_label выше).
_BLOCK_NAMES_RU = {
    'acid_puddle': 'Кислотная лужа', 'amphibian_camel': 'Амфибия "Верблюд"',
    'amphibian_elephant': 'Амфибия "Слон"', 'amphibian_polecat': 'Амфибия "Хорек"',
    'amphibian_turtle': 'Амфибия "Черепаха"', 'apple': 'Яблоня', 'arable': 'Пашня',
    'armored_car1': 'Бронеавтомобиль "Енот"', 'armored_car_chipmunk': 'Броневик "Бурундук"',
    'bananas': 'Банановая пальма', 'baobab': 'Баобаб', 'bed': 'Кровать', 'beet': 'Свекла',
    'bell_pepper': 'Сладкий перец', 'birch': 'Береза', 'boat_crucian': 'Катер "Карась"',
    'boat_pike': 'Катер "Щука"', 'box': 'Ящик', 'box_annihilator': 'Ящик аннигилятор',
    'brick_bridge': 'Кирпичный мост', 'bush': 'Куст', 'cabbage': 'Капуста',
    'car_1': 'Фургон V-1', 'car_2': 'Фургон V-2', 'car_3': 'Фургон V-3', 'car_buggy': 'Багги',
    'car_kangaroo': 'Автомобиль "Кенгуру"', 'carrot': 'Морковь', 'chest_wood': 'Деревянный сундук',
    'coal': 'Уголь', 'cobalt': 'Кобальтовая руда', 'cobalt_crusher': 'Дробилка (кобальт)',
    'cobalt_distiller': 'Дистиллятор (кобальт)', 'cobalt_extractor': 'Экстрактор (кобальт)',
    'cobalt_furnace': 'Печь (кобальт)', 'cobalt_press': 'Пресс (кобальт)', 'coconut': 'Кокосовая пальма',
    'collector_container': 'Контейнер-коллектор', 'collider': 'Коллайдер',
    'collider_block': 'Блок коллайдера', 'comfortable_bed': 'Удобная кровать',
    'container': 'Контейнер', 'copper_ore': 'Медная руда', 'corn': 'Кукуруза',
    'cosmochlor': 'Космохлоровая руда', 'crusher': 'Дробилка', 'cucumber': 'Огурец',
    'culinary_table': 'Кулинарный стол', 'cyber_workbench': 'Кибер-верстак', 'dill': 'Укроп',
    'distiller': 'Дистиллятор', 'door_brick': 'Кирпичная дверь', 'door_iron': 'Железная дверь',
    'door_stone': 'Каменная дверь', 'door_titan': 'Титановая дверь', 'door_wood': 'Деревянная дверь',
    'drawing_table': 'Чертёжный стол', 'electronium': 'Электрониевая руда',
    'epsilon_metal': 'Эпсилон-металл', 'extra_hydroponics': 'Доп. гидропоника',
    'extractor': 'Экстрактор', 'faunitron': 'Фаунитрон', 'feeder': 'Кормушка',
    'feeder_refrigerator': 'Холодильник-кормушка', 'floor_brick': 'Кирпичный пол',
    'floor_iron': 'Железный пол', 'floor_stone': 'Каменный пол', 'floor_titan': 'Титановый пол',
    'floor_wood': 'Деревянный пол', 'furnace': 'Печь', 'garlic': 'Чеснок', 'gold_ore': 'Золотая руда',
    'grape': 'Виноград', 'grass': 'Трава', 'hydroponics_unit': 'Гидропонная установка',
    'hyperdrive': 'Гипердвигатель', 'industrial_mixer': 'Промышленный миксер',
    'industrial_workbench': 'Индустриальный верстак', 'iridium': 'Иридиевая руда',
    'iron': 'Железная руда', 'iron_boat': 'Железная лодка', 'iron_bridge': 'Железный мост',
    'iron_workbench': 'Железный верстак', 'lab_table': 'Лабораторный стол',
    'landing_area': 'Посадочная площадка', 'landscaping_generator': 'Генератор ландшафта',
    'large_container': 'Большой контейнер', 'lead_ore': 'Свинцовая руда', 'lemons': 'Лимонное дерево',
    'meteorite': 'Метеорит', 'mushrooms': 'Грибы', 'nitrocalite': 'Нитрокалит',
    'oil': 'Нефть', 'omicronium': 'Омикрониевая руда', 'omicronium_crusher': 'Дробилка (омикроний)',
    'omicronium_distiller': 'Дистиллятор (омикроний)', 'omicronium_extractor': 'Экстрактор (омикроний)',
    'omicronium_furnace': 'Печь (омикроний)', 'omicronium_press': 'Пресс (омикроний)',
    'onion': 'Лук', 'oranges': 'Апельсиновое дерево', 'oxygen_generator_og1': 'Генератор кислорода OG-1',
    'pepper': 'Перец', 'pineapple': 'Ананас', 'planetary_stabilizer': 'Планетарный стабилизатор',
    'platinum': 'Платиновая руда', 'plutonium': 'Плутониевая руда', 'poisonous_moss': 'Ядовитый мох',
    'portal': 'Портал', 'potatoes': 'Картофель', 'press': 'Пресс', 'protonite': 'Протонитовая руда',
    'pumpkin': 'Тыква', 'quantum_hyperdrive': 'Квантовый гипердвигатель',
    'quantum_workbench': 'Квантовый верстак', 'recycling_workbench': 'Верстак переработки',
    'refrigerator': 'Холодильник', 'rescue_capsule': 'Спасательная капсула', 'rice': 'Рис',
    'rocket_carnotaurus': 'Ракета "Карнотавр"', 'rocket_cargo1': 'Грузовой отсек ракеты 1',
    'rocket_cargo2': 'Грузовой отсек ракеты 2', 'rocket_diplodocus': 'Ракета "Диплодок"',
    'rocket_engine_i1': 'Ракетный двигатель I-1', 'rocket_engine_i2': 'Ракетный двигатель I-2',
    'rocket_engine_n1': 'Ракетный двигатель N-1', 'rocket_engine_n2': 'Ракетный двигатель N-2',
    'rocket_engine_quantum': 'Квантовый ракетный двигатель', 'rocket_engine_sfe3': 'Ракетный двигатель SFE-3',
    'rocket_jalopy': 'Ракета Jalopy', 'rocket_pterodactyl': 'Ракета "Птеродактиль"',
    'rocket_r1': 'Ракета R1', 'rocket_r2': 'Ракета R2', 'rocket_r3': 'Ракета R3',
    'rocket_sauropod': 'Ракета "Завропод"', 'rocket_spinosaurus': 'Ракета "Спинозавр"',
    'rocket_stegosaurus': 'Ракета "Стегозавр"', 'rocket_tyrannosaurus': 'Ракета "Тираннозавр"',
    'rubber_tree': 'Каучуковое дерево', 'salt': 'Соль', 'seaweed': 'Водоросли',
    'sequoia': 'Секвойя', 'silver_ore': 'Серебряная руда', 'space_item': 'Космический предмет',
    'spruce': 'Ель', 'station_control_panel': 'Панель управления станцией',
    'stone': 'Камень', 'stone_bridge': 'Каменный мост', 'strawberry': 'Клубника',
    'sugar_cane': 'Сахарный тростник', 'sulfur': 'Сера', 'tank_bear': 'Танк "Медведь"',
    'tank_crocodile': 'Танк "Крокодил"', 'tank_fox': 'Танк "Лис"', 'tank_muskrat': 'Танк "Ондатра"',
    'tank_rhinoceros': 'Танк "Носорог"', 'tank_wolf': 'Танк "Волк"', 'titan_workbench': 'Титановый верстак',
    'titanium_bed': 'Титановая кровать', 'titanium_ore': 'Титановая руда', 'tomatoes': 'Помидоры',
    'trading_station': 'Торговая станция', 'tropical_tree': 'Тропическое дерево', 'tree': 'Дерево',
    'tungsten': 'Вольфрамовая руда', 'tungsten_bed': 'Вольфрамовая кровать',
    'tungsten_workbench': 'Вольфрамовый верстак', 'uranium_ore': 'Урановая руда',
    'vulcanite': 'Вулканитовая руда', 'wall_brick': 'Кирпичная стена', 'wall_iron': 'Железная стена',
    'wall_stone': 'Каменная стена', 'wall_titan': 'Титановая стена', 'wall_wood': 'Деревянная стена',
    'waterlily': 'Кувшинка', 'watermelon': 'Арбуз', 'wheat': 'Пшеница', 'wooden_boat': 'Деревянная лодка',
    'wooden_bridge': 'Деревянный мост', 'workbench': 'Верстак', 'xirium': 'Ксириевая руда',
}

_ABILITY_NAMES_RU = {
    'attack1': 'Вероятность ускорения атаки в 2 раза (5%)',
    'attack2': 'Вероятность ускорения атаки в 2 раза (10%)',
    'attack3': 'Вероятность ускорения атаки в 2 раза (15%)',
    'autocure': 'Автоматическое использование лекарств из инвентаря',
    'autoeat': 'Автоматическое использование еды из инвентаря',
    'autoenergy': 'Автоматическое использование энергетиков из инвентаря',
    'autooxygen': 'Автоматическая экипировка скафандром при отсутствии кислорода',
    'boost_tech1': 'Вероятность ускорения исследования на 10% (2%)',
    'boost_tech2': 'Вероятность ускорения исследования на 10% (5%)',
    'boost_tech3': 'Вероятность ускорения исследования на 10% (10%)',
    'cargo1': 'Увеличение размера инвентаря на 1 слот', 'cargo2': 'Увеличение размера инвентаря на 2 слота',
    'cargo3': 'Увеличение размера инвентаря на 3 слота', 'cargo4': 'Увеличение размера инвентаря на 4 слота',
    'cargo5': 'Увеличение размера инвентаря на 5 слотов', 'cargo6': 'Увеличение размера инвентаря на 6 слотов',
    'cargo7': 'Увеличение размера инвентаря на 7 слотов', 'cargo8': 'Увеличение размера инвентаря на 8 слотов',
    'cargo9': 'Увеличение размера инвентаря на 9 слотов', 'cargo10': 'Увеличение размера инвентаря на 10 слотов',
    'cargo11': 'Увеличение размера инвентаря на 11 слотов', 'cargo12': 'Увеличение размера инвентаря на 12 слотов',
    'cargo13': 'Увеличение размера инвентаря на 13 слотов', 'cargo14': 'Увеличение размера инвентаря на 14 слотов',
    'cargo15': 'Увеличение размера инвентаря на 15 слотов', 'cargo16': 'Увеличение размера инвентаря на 16 слотов',
    'cargo17': 'Увеличение размера инвентаря на 17 слотов', 'cargo18': 'Увеличение размера инвентаря на 18 слотов',
    'cargo19': 'Увеличение размера инвентаря на 19 слотов', 'cargo20': 'Увеличение размера инвентаря на 20 слотов',
    'cargo21': 'Увеличение размера инвентаря на 21 слот', 'cargo22': 'Увеличение размера инвентаря на 22 слота',
    'cargo23': 'Увеличение размера инвентаря на 23 слота', 'cargo24': 'Увеличение размера инвентаря на 24 слота',
    'cargo25': 'Увеличение размера инвентаря на 25 слотов', 'cargo26': 'Увеличение размера инвентаря на 26 слотов',
    'cargo27': 'Увеличение размера инвентаря на 27 слотов', 'cargo28': 'Увеличение размера инвентаря на 28 слотов',
    'cargo29': 'Увеличение размера инвентаря на 29 слотов',
    # cargo30 — в клиентской локализации нет строки (пропуск в игре), оставлен как #slug
    'craft1': 'Вероятность ускорения крафта в 2 раза (10%)', 'craft2': 'Вероятность ускорения крафта в 2 раза (20%)',
    'craft3': 'Вероятность ускорения крафта в 2 раза (30%)',
    'crit1': 'Критический удар (1%)', 'crit2': 'Критический удар (2%)', 'crit3': 'Критический удар (3%)',
    'crit4': 'Критический удар (4%)', 'crit5': 'Критический удар (5%)',
    'exp1': 'Ускорение получения опыта (5%)', 'exp2': 'Ускорение получения опыта (10%)',
    'exp3': 'Ускорение получения опыта (15%)',
    'hand_mining1': 'Ускорение добычи руками в 2 раза', 'hand_mining2': 'Ускорение добычи руками в 3 раза',
    'hand_mining3': 'Ускорение добычи руками в 4 раза', 'hand_mining4': 'Ускорение добычи руками в 5 раз',
    'item_double1': 'Вероятность создания в два раза больше предметов при крафте (1%)',
    'item_double2': 'Вероятность создания в два раза больше предметов при крафте (2%)',
    'item_double3': 'Вероятность создания в два раза больше предметов при крафте (3%)',
    'item_double4': 'Вероятность создания в два раза больше предметов при крафте (4%)',
    'item_durability1': 'Вероятность создания прочного предмета (5%)',
    'item_durability2': 'Вероятность создания прочного предмета (10%)',
    'item_durability3': 'Вероятность создания прочного предмета (15%)',
    'item_durability4': 'Вероятность создания прочного предмета (20%)',
    'item_durability5': 'Вероятность создания прочного предмета (25%)',
    'mining1': 'Вероятность ускорения добычи в 2 раза (10%)', 'mining2': 'Вероятность ускорения добычи в 2 раза (20%)',
    'mining3': 'Вероятность ускорения добычи в 2 раза (30%)',
    'regeneration1': 'Ускоренная регенерация (1 уровень)', 'regeneration2': 'Ускоренная регенерация (2 уровень)',
    'regeneration3': 'Ускоренная регенерация (3 уровень)',
    'survival1': 'Вероятность выживания при смертельном повреждении (2%)',
    'survival2': 'Вероятность выживания при смертельном повреждении (5%)',
    'survival3': 'Вероятность выживания при смертельном повреждении (10%)',
}


def _block_label(slug):
    return _BLOCK_NAMES_RU.get(slug, slug) if slug else None


def _ability_label(slug):
    return _ABILITY_NAMES_RU.get(slug, slug) if slug else None


def mapdt_find(cfg, map_id, item):
    """Найти предмет(ы) во всём мире или на одной карте.

    ``map_id`` = число или ``"all"``. ``item`` = id / имя / подстрока имени.
    -> ``{ok, query, matched[{id,name}], want, per_map[{map,size,total_count,
    spots,by_where,sec,capped}], hits[{map,x,y,where,type,name,count,durability}],
    total_count, spots, scanned, skipped, elapsed_sec, note}``.
    """
    if mapdt is None:
        return {"ok": False, "error": "модуль mapdt недоступен"}
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    want, matched = _resolve_item_query(world_dir, item)
    if not want:
        return {"ok": False, "error": "предмет не найден: %r" % item}

    md = os.path.join(world_dir, "Data", "maps")
    ids = []
    if str(map_id).lower() in ("all", "*", ""):
        try:
            for f in os.listdir(md):
                mm = re.match(r"map(\d+)\.dt$", f)
                if mm:
                    ids.append(int(mm.group(1)))
        except OSError:
            pass
        ids.sort(key=lambda n: os.path.getsize(os.path.join(md, "map%d.dt" % n)))
    else:
        try:
            ids = [int(map_id)]
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad id"}

    items = load_items(world_dir)
    unames = load_user_list(world_dir)
    per_map, hits = [], []
    by_owner = collections.Counter()
    ospots = collections.Counter()
    total = spots = scanned = 0
    skipped = []
    budget = 150.0            # c: общий бюджет на скан всего мира
    hit_cap = 5000
    t_start = time.time()
    fs = frozenset(want)
    for mid in ids:
        if time.time() - t_start > budget:
            skipped.append(mid)
            continue
        path = os.path.join(md, "map%d.dt" % mid)
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        ck = (path, fs)
        cached = _MAPDT_FIND_CACHE.get(ck)
        if cached and cached[0] == mt:
            res = cached[1]
        else:
            t0 = time.time()
            res = mapdt.find_item(path, want, world_dir=world_dir,
                                  item_names=items, cap=hit_cap, user_names=unames)
            if res.get("ok"):
                res["sec"] = round(time.time() - t0, 2)
                _MAPDT_FIND_CACHE[ck] = (mt, res)
        scanned += 1
        if not res.get("ok"):
            skipped.append(mid)
            continue
        if res["spots"]:
            per_map.append({
                "map": mid, "size": "%dx%d" % (res["w"], res["h"]),
                "total_count": res["total_count"], "spots": res["spots"],
                "by_where": res["by_where"], "by_owner": res.get("by_owner") or [],
                "sec": res.get("sec"), "capped": res["capped"],
            })
            total += res["total_count"]
            spots += res["spots"]
            for row in (res.get("by_owner") or []):
                by_owner[row["owner"]] += row["count"]
                ospots[row["owner"]] += row["spots"]
            for hh in res["hits"]:
                if len(hits) < hit_cap:
                    hh2 = dict(hh)
                    hh2["map"] = mid
                    hits.append(hh2)
    per_map.sort(key=lambda x: -x["total_count"])
    owners = [{"owner": o, "owner_name": (unames.get(o) or ("id %s" % o)) if o else "— ничья —",
               "spots": ospots[o], "count": v} for o, v in by_owner.most_common()]
    return {
        "ok": True,
        "query": str(item),
        "matched": matched,
        "want": sorted(want),
        "per_map": per_map,
        "by_owner": owners,
        "hits": hits,
        "total_count": total,
        "spots": spots,
        "scanned": scanned,
        "skipped": skipped,
        "elapsed_sec": round(time.time() - t_start, 1),
        "note": ("остановлено по лимиту времени (%ss); повторите — уже разобранные "
                 "карты в кэше" % int(budget)) if skipped else None,
    }


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

    # id внесистемной карты (map<N>.dt) == id записи в Data\world\star1.json —
    # ПОДТВЕРЖДЕНО: все 26 внесистемных карт на проде совпали 1:1 по id с
    # осмысленными соседними координатами (см. sigma-steam-stage2-bot.md,
    # коммит после e8aa465). Даёт имя и позицию в звёздной системе для планет.
    star_by_id = {}
    try:
        so = space_objects(cfg, 1)
        if so.get("ok"):
            star_by_id = {o["id"]: o for o in so["objects"]}
    except Exception:  # noqa: BLE001
        pass

    rows = []
    for mp in maps:
        dim = map_dim(world_dir, mp) if mp not in (None, 0) else None
        is_offworld = mp is not None and mp not in (0, 1)
        so_obj = star_by_id.get(mp) if is_offworld else None
        rows.append({"map": mp, "online": gs.get(mp, 0), "avatars": avatars_by_map.get(mp, 0),
                     "territories": terr_by_map.get(mp, 0), "space": mp == 0,
                     "is_offworld": is_offworld,
                     "space_name": so_obj["name"] if so_obj else None,
                     "space_x": so_obj["x"] if so_obj else None,
                     "space_y": so_obj["y"] if so_obj else None,
                     "size": ("%dx%d" % (dim["w"], dim["h"])) if dim else None})
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
            "space_note": "карта 0 = космос: игра считает таких игроков онлайн, фактически могут быть оффлайн",
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
            "current_name": tech_label(world_dir, raw.get("researchTech")),
            "remaining_min": _rem_min(raw.get("timeResearchTech")),
            "done_count": len(raw.get("techList") or []),
            "tech_list": raw.get("techList") or [],
            "tech_named": [{"id": t, "label": tech_label(world_dir, t)}
                           for t in (raw.get("techList") or [])],
            "invested_h": round(sum(_load_ref(world_dir, "tech.json", "id", "cost").get(t) or 0
                                    for t in (raw.get("techList") or [])) / 60.0, 1),
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
            "abilities": [_ability_label(abil_names.get(a)) or ("#%s" % a) for a in (unit.get("ability") or [])],
            "buffs": len(unit.get("buffs") or []),
            "stash_count": len(inv_u),
            "carry_count": len(inv_a),
            "carry_size": (unit.get("Inventory") or {}).get("size"),
            "carry_limited": bool((unit.get("Inventory") or {}).get("isLimit")),
            "stash_size": (raw.get("Inventory") or {}).get("size"),
            "stash_limited": bool((raw.get("Inventory") or {}).get("isLimit")),
            "equip_idx": (unit.get("Inventory") or {}).get("equipItem"),
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
    out = [{"id": i, "name": d.get("name"), "label": item_label(d.get("name")) or d.get("name"), "stack": d.get("stack", 1)}
           for i, d in by_id.items()]
    out.sort(key=lambda x: (x["name"] or ""))
    return {"ok": True, "items": out}


def _resolve_item(world_dir, item):
    by_id, by_name = _items_full(world_dir)
    s = str(item).strip()
    m = re.search(r"#(\d+)\s*$", s)            # «Уголь · coal #12» из списка выбора
    if m and int(m.group(1)) in by_id:
        return int(m.group(1)), by_id[int(m.group(1))]
    if s.isdigit() and int(s) in by_id:
        return int(s), by_id[int(s)]
    if s in by_name:
        return by_name[s]["id"], by_name[s]
    return None, None


def _is_offline(world_dir, uid):
    """Офлайн ли игрок ПРЯМО СЕЙЧАС (см. ``_online_now`` — с поправкой на
    рестарт сервера). Используется как гейт перед правкой инвентаря/модерацией."""
    return not _online_now(world_dir).get(uid, False)


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
               "research": raw.get("researchTech") or "", "map": raw.get("mapId")}
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
        if cur["map"] is not None and p.get("map") is not None and cur["map"] != p["map"]:
            events.append({"ts": ts, "uid": uid, "name": who, "kind": "map_changed",
                           "to": cur["map"], "from": p["map"],
                           "space": cur["map"] == 0 or p["map"] == 0})

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


def buff_notepad_save(path, raw):
    """Сохранить buff_notepad.json, загруженный вручную через веб-панель (вкладка
    «Микстуры») — сам файл лежит на машине игрока, сервер его не видит.
    Формат (как в игре): ``{"items":[{"items":[id,id,id,id],"time":float,
    "isSign":bool,"buff":[{"state":int,"val":float},...]},...]}``."""
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return {"ok": False, "error": "неверный формат: ожидается объект с полем items[]"}
    for r in raw["items"]:
        if not isinstance(r, dict) or not isinstance(r.get("items"), list):
            return {"ok": False, "error": "неверный формат записи в items[]"}
    out = {"items": raw["items"], "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        tmp = path + ".swtmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "count": len(out["items"])}


def buff_lib_as_notepad(cfg):
    """Библиотека рецептов микстур самого сервера (``Data\\product\\buff_lib.json``: все смешивания
    всех игроков, игра ведёт сама) в формате buff_notepad — ручная загрузка файла не нужна."""
    wd = find_world_dir(cfg)
    lib = _read_json(os.path.join(wd, "Data", "product", "buff_lib.json")) if wd else None
    if not lib:
        return None
    return {"items": [{"items": r.get("Materials") or [], "time": r.get("time"), "buff": r.get("Buffs") or []}
                      for r in lib.get("items") or []],
            "saved_at": datetime.fromtimestamp(os.path.getmtime(os.path.join(wd, "Data", "product", "buff_lib.json"))).strftime("%Y-%m-%d %H:%M:%S"),
            "source": "server"}


def buff_notepad_read(cfg, path):
    """Загруженный buff_notepad с именами ингредиентов (из ``Data\\items.json``,
    с RU-подписью через ``_buff_material_label`` где есть) и списком-индексом
    «ингредиент -> в каких комбинациях встречается» — для вкладки «Микстуры».
    Названия эффектов (``buff[].state``) — см. ``_BUFF_TYPE_NAMES_RU``
    (``ZData.BuffType``, расшифрован Ghidra-дампом 2026-09-17/18, см.
    [[sigma-buff-recipe-algorithm]]; это ОТДЕЛЬНЫЙ enum от статов игрока)."""
    d = buff_lib_as_notepad(cfg) or _read_json(path) or {}      # сначала серверная библиотека
    recs = d.get("items") or []
    if not recs:
        return {"ok": True, "count": 0, "records": [], "by_item": [], "saved_at": d.get("saved_at")}
    world_dir = find_world_dir(cfg)
    item_names = load_items(world_dir) if world_dir else {}

    def iname(i):
        raw = item_names.get(i)
        return _buff_material_label(raw) or raw or ("#%s" % i)

    records = []
    by_item = {}
    for idx, r in enumerate(recs):
        ids = r.get("items") or []
        records.append({
            "idx": idx,
            "items": [{"id": i, "name": iname(i)} for i in ids],
            "time": r.get("time"),
            "buff": [{"state": b.get("state"), "name": _BUFF_TYPE_NAMES_RU.get(b.get("state"), "#%s" % b.get("state")),
                      "val": b.get("val")} for b in (r.get("buff") or [])],
        })
        for i in ids:
            slot = by_item.setdefault(i, {"id": i, "name": iname(i), "count": 0})
            slot["count"] += 1
    by_item_list = sorted(by_item.values(), key=lambda x: x["name"].lower())
    return {"ok": True, "source": d.get("source") or "upload", "count": len(records), "records": records,
            "by_item": by_item_list, "saved_at": d.get("saved_at")}


# --- ZData.BuffType / ZData.BuffRecipe.Calc — расшифровано Ghidra-декомпиляцией
# GameAssembly.dll 2026-09-17/18 (см. [[sigma-buff-recipe-algorithm]] для полного
# разбора и метода). Формула проверена на 366/366 реальных рецептах сервера —
# точное совпадение, не эмпирическая аппроксимация.
_BUFF_TYPE_NAMES_RU = {
    0: "Здоровье", 1: "Энергия", 2: "Меткость", 3: "Скорость движения",
    4: "Скорость действия", 5: "Сила ближнего боя", 6: "Сила дальнего боя",
    7: "Щит", 8: "Скорость ближнего боя", 9: "Скорость дальнего боя",
}

_BUFF_MATERIAL_NAMES_RU = {
    "meat": "Мясо", "bone": "Кость", "leather": "Кожа", "meat_fish": "Рыбье мясо",
    "fat_tail": "Жирный хвост", "brain": "Мозг", "kidneys": "Почки", "liver": "Печень",
    "stomach": "Желудок", "lungs": "Лёгкие", "eyes": "Глаза", "spleen": "Селезёнка",
    "ears": "Уши", "cartilage": "Хрящ", "heart": "Сердце", "poultry": "Птица",
    "poisonous_moss": "Ядовитый мох", "milk": "Молоко", "red_caviar": "Красная икра",
    "black_caviar": "Чёрная икра", "egg": "Яйцо", "honey": "Мёд", "butter": "Масло", "curd": "Творог",
}

_BUFF_GETVAL_DIV80 = (3, 5, 6)
_BUFF_GETVAL_DIV160 = (4, 7, 8, 9)
_BUFF_CLAMP_THRESH = {0: 5.0, 1: 5.0, 2: 1.0}
_BUFF_CLAMP_DEFAULT = 0.0999


def _buff_material_label(slug):
    return _BUFF_MATERIAL_NAMES_RU.get(slug, slug) if slug else None


def _buff_get_val(t, v):
    if t in (0, 1):
        return v
    if t == 2:
        return v * 0.0625
    if t in _BUFF_GETVAL_DIV80:
        return v / 80.0
    return v / 160.0


def _buff_clamp_val(t, v):
    thresh = _BUFF_CLAMP_THRESH.get(t, _BUFF_CLAMP_DEFAULT)
    return 0.0 if abs(v) < thresh else v


def _buff_correct_val(t, v):
    if t in (0, 1, 2):
        return v
    if v < 0:
        return 1.0 / (abs(v) + 1.0)
    return v + 1.0


def _buff_calc(materials, mat, slots):
    """Точное воспроизведение ``ZData.BuffRecipe.Calc``. ``materials`` — 4
    id-слага ингредиента В ПОРЯДКЕ СЛОТОВ 0-3 (порядок важен — определяет
    знак!). ``mat`` — из ``buff_balance_read()['materials']``, ``slots`` —
    оттуда же ``['slots']``. -> ``{buff_type: val}`` (только ненулевые)."""
    out = {}
    for t in range(10):
        total = 0.0
        for i in range(4):
            val, op = mat[materials[i]].get(t, (0.0, 0))
            if op not in (0, 1):
                continue
            st = slots[i].get(t, 2)
            if st == 2:
                continue
            sign = 1 if op == 0 else -1
            if st == 1:
                sign = -sign
            total += sign * val
        for i in range(4):
            val, op = mat[materials[i]].get(t, (0.0, 0))
            if op not in (2, 3):
                continue
            st = slots[i].get(t, 2)
            if st == 2:
                continue
            do_sub = (st == 1) != (op == 3)
            total = total - val if do_sub else total * val
        v = _buff_clamp_val(t, _buff_get_val(t, total))
        if abs(v) > 0:
            out[t] = _buff_correct_val(t, v)
    return out


def buff_balance_read(cfg):
    """Таблица состояний ингредиентов для микстур (``Data\\product\\buff_balance.json``,
    ``ZData.BuffBalance``) + фиксированные слоты (``Data\\product\\buff_lib.json``,
    поле ``slots`` — тоже ``ZData``, общее на весь сервер, не per-рецепт).
    -> ``{ok, materials: {slug: {type: (val,op)}}, slots: [{type:state},...×4],
    ingredients: [{id,name}]}``."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    bal = _read_json(os.path.join(world_dir, "Data", "product", "buff_balance.json"))
    if not bal or not bal.get("items"):
        return {"ok": False, "error": "нет buff_balance.json — в этом мире ещё не мешали микстуры"}
    lib = _read_json(os.path.join(world_dir, "Data", "product", "buff_lib.json")) or {}
    slots_raw = lib.get("slots") or []
    if len(slots_raw) != 4:
        return {"ok": False, "error": "нет данных о слотах (buff_lib.json)"}
    mat = {it["id"]: {s["StateType"]: (s["val"], s["operation"]) for s in (it.get("States") or [])}
           for it in bal["items"]}
    slots = [{x["Type"]: x["State"] for x in (s.get("Items") or [])} for s in slots_raw]
    ingredients = sorted(
        ({"id": k, "name": _buff_material_label(k) or k} for k in mat),
        key=lambda x: x["name"].lower())
    return {"ok": True, "materials": mat, "slots": slots, "ingredients": ingredients}


def buff_optimize(cfg, available, target_type, top_n=5):
    """Перебор всех УПОРЯДОЧЕННЫХ четвёрок из ``available`` (id-слаги
    ингредиентов, доступных игроку) в поисках максимума эффекта
    ``target_type`` (``ZData.BuffType``, 0-9). Порядок важен — определяет,
    в какой слот (0-3) попадёт ингредиент, а слот определяет знак вклада.
    -> ``{ok, checked, count, results: [{materials:[{id,name}], buffs:[{state,name,val}]}]}``,
    отсортировано по убыванию ``target_type``."""
    bal = buff_balance_read(cfg)
    if not bal.get("ok"):
        return bal
    mat, slots = bal["materials"], bal["slots"]
    try:
        target_type = int(target_type)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad target_type"}
    if target_type not in _BUFF_TYPE_NAMES_RU:
        return {"ok": False, "error": "bad target_type"}
    avail = [a for a in (available or []) if a in mat]
    if len(avail) < 4:
        return {"ok": False, "error": "нужно минимум 4 доступных ингредиента"}
    n = len(avail)
    checked = n * (n - 1) * (n - 2) * (n - 3)
    results = []
    for combo in itertools.permutations(avail, 4):
        buffs = _buff_calc(combo, mat, slots)
        v = buffs.get(target_type)
        if v is None or v <= 0:
            continue
        results.append((v, combo, buffs))
    results.sort(key=lambda x: -x[0])
    top = results[:max(1, min(50, int(top_n or 5)))]
    return {
        "ok": True, "checked": checked, "count": len(results),
        "results": [{
            "materials": [{"id": m, "name": _buff_material_label(m) or m} for m in combo],
            "buffs": [{"state": t, "name": _BUFF_TYPE_NAMES_RU.get(t, "#%s" % t), "val": round(v2, 4)}
                      for t, v2 in sorted(buffs.items(), key=lambda kv: -kv[1])],
        } for v, combo, buffs in top],
    }


# --- Кулинария: ZServer.Game.Global.ProductLib.GetProductLibItem / ZProductBalance /
# ProductGenes / ZInventoryManager.UseItem — расшифровано Ghidra-декомпиляцией
# GameAssembly.dll 2026-09-24. Формула проверена на 2305/2305 блюдах сервера .106.
#   eat   = B[s0][s1] * B[s2][s3] * B[s0][s2] * B[s1][s3] * 5.0 * 0.0625
#           (B — направленная попарная таблица 0..6 из product_balance.json,
#            слоты — сетка 2×2: 0 1 / 2 3, порядок важен)
#   genes = XOR генов 4 ингредиентов (product_genes.json), только если eat > 0
# Съедание (UseItem): сытость += eat — если превысит максимум, блюдо НЕ съедается;
# каждый ген=1 даёт +eat/5 к genA..genD (UnitParamType 7-10), энергия += eat/5;
# когда все genA..genD ≥ 100 — с каждого снимается 100, «Очки генетики» +1.
_FOOD_EAT_K = 5.0 * 0.0625
_FOOD_GENE_THRESHOLD = 100.0
_FOOD_GENE_NAMES = ("A", "B", "C", "D")

_FOOD_NAMES_RU = {
    'berry': 'Ягода', 'meat': 'Мясо', 'roast': 'Жаренное мясо', 'meat_fish': 'Рыба',
    'potatoes': 'Картофель', 'rice': 'Рис', 'corn': 'Кукуруза', 'tomatoes': 'Помидоры',
    'onion': 'Лук', 'mushrooms': 'Грибы', 'carrot': 'Морковь', 'cabbage': 'Капуста',
    'pumpkin': 'Тыква', 'beet': 'Свекла', 'dill': 'Укроп', 'wheat': 'Пшеница',
    'cucumber': 'Огурец', 'strawberry': 'Клубника', 'apple': 'Яблоко', 'garlic': 'Чеснок',
    'grape': 'Виноград', 'pepper': 'Перец', 'sugar_cane': 'Сахарный тростник',
    'bananas': 'Банан', 'oranges': 'Апельсин', 'lemons': 'Лимон', 'bell_pepper': 'Сладкий перец',
    'pineapple': 'Ананас', 'watermelon': 'Арбуз', 'coconut': 'Кокос', 'flour': 'Мука',
    'sugar': 'Сахар', 'salt': 'Соль', 'fried_fish': 'Жаренная рыба', 'fat_tail': 'Курдючный жир',
    'brain': 'Мозги', 'kidneys': 'Почки', 'liver': 'Печень', 'stomach': 'Желудок',
    'lungs': 'Легкие', 'eyes': 'Глаза', 'spleen': 'Селезенка', 'ears': 'Уши',
    'cartilage': 'Хрящи', 'heart': 'Сердце', 'poultry': 'Мясо птицы', 'milk': 'Молоко',
    'red_caviar': 'Красная икра', 'black_caviar': 'Черная икра', 'egg': 'Яйцо',
    'honey': 'Мед', 'butter': 'Масло', 'curd': 'Творог',
}


def _genes_mask(g):
    """``{"genes":[bool×4]}`` -> битовая маска (бит i = ген A..D)."""
    arr = (g or {}).get("genes") or []
    return sum(1 << i for i, x in enumerate(arr[:4]) if x)


def _genes_list(mask):
    return [_FOOD_GENE_NAMES[i] for i in range(4) if mask >> i & 1]


def food_data_read(cfg):
    """Попарный баланс (``Data\\product\\product_balance.json``) + гены
    ингредиентов (``product_genes.json``) — обе таблицы генерятся сервером
    один раз на мир. -> ``{ok, bal:{id:{id:int}}, genes:{id:mask},
    names:{id:name}, ingredients:[{id,name,genes}]}``."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    pdir = os.path.join(world_dir, "Data", "product")
    bal_raw = _read_json(os.path.join(pdir, "product_balance.json")) or {}
    gen_raw = _read_json(os.path.join(pdir, "product_genes.json")) or {}
    if not bal_raw.get("items") or not gen_raw.get("items"):
        return {"ok": False, "error": "нет product_balance.json/product_genes.json — в этом мире ещё не готовили"}
    bal = {it["id"]: {x["id"]: x["balance"] for x in (it.get("items") or [])} for it in bal_raw["items"]}
    genes = {it["id"]: _genes_mask(it.get("genes")) for it in gen_raw["items"]}
    item_names = load_items(world_dir)
    names = {}
    for i in bal:
        slug = item_names.get(i)
        names[i] = _FOOD_NAMES_RU.get(slug) or slug or ("#%s" % i)
    ingredients = sorted(({"id": i, "name": names[i], "genes": _genes_list(genes.get(i, 0))}
                          for i in bal if i in genes), key=lambda x: x["name"].lower())
    return {"ok": True, "bal": bal, "genes": genes, "names": names, "ingredients": ingredients}


def _food_calc(combo, bal, genes):
    """Точное воспроизведение ``ProductLib.GetProductLibItem``. -> (eat, genes_mask)."""
    a, b, c, d = combo
    p = bal[a].get(b, 0) * bal[c].get(d, 0) * bal[a].get(c, 0) * bal[b].get(d, 0)
    if not p:
        return 0.0, 0
    return p * _FOOD_EAT_K, genes[a] ^ genes[b] ^ genes[c] ^ genes[d]


def _food_dish(combo, eat, mask, names):
    ng = bin(mask).count("1")
    per_gene = eat / 5.0
    return {
        "items": [{"id": i, "name": names.get(i, "#%s" % i)} for i in combo],
        "eat": round(eat, 4), "genes": _genes_list(mask),
        "gene_gain": round(per_gene, 4) if ng else 0,
        # сколько таких блюд нужно на 1 очко генетики (только если все 4 гена)
        "per_point": (int(-(-_FOOD_GENE_THRESHOLD // per_gene)) if ng == 4 and per_gene > 0 else None),
    }


def food_lib_read(cfg):
    """Все блюда, которые когда-либо готовили на сервере (``product_lib.json``,
    кэш ``ProductLib``) — с именами ингредиентов. Пустые (eat=0) тоже, чтобы было
    видно, что с чем НЕ сочетается."""
    fd = food_data_read(cfg)
    if not fd.get("ok"):
        return fd
    world_dir = find_world_dir(cfg)
    lib = _read_json(os.path.join(world_dir, "Data", "product", "product_lib.json")) or {}
    names = fd["names"]
    out, by_item = [], {}
    for r in lib.get("items") or []:
        ids = r.get("items") or []
        if len(ids) != 4:
            continue
        dish = _food_dish(ids, float(r.get("eat") or 0), _genes_mask(r.get("genes")), names)
        dish["id"] = r.get("id")
        out.append(dish)
        for i in ids:
            slot = by_item.setdefault(i, {"id": i, "name": names.get(i, "#%s" % i), "count": 0})
            slot["count"] += 1
    out.sort(key=lambda x: -(x["id"] or 0))
    return {"ok": True, "count": len(out), "dishes": out,
            "by_item": sorted(by_item.values(), key=lambda x: x["name"].lower())}


def food_optimize(cfg, available, need_genes=None, max_eat=None, top_n=10):
    """Полный перебор упорядоченных четвёрок из ``available`` (id ингредиентов).
    ``need_genes`` — список из "A".."D", которые ОБЯЗАТЕЛЬНО должны быть в блюде;
    ``max_eat`` — максимум сытости игрока (блюдо сытнее просто не съедается).
    Для каждого набора ингредиентов оставляем лучший порядок; сортировка по eat.
    -> ``{ok, checked, count, results:[dish]}``."""
    fd = food_data_read(cfg)
    if not fd.get("ok"):
        return fd
    bal, genes, names = fd["bal"], fd["genes"], fd["names"]
    avail = []
    for a in available or []:
        try:
            a = int(a)
        except (TypeError, ValueError):
            continue
        if a in bal and a in genes and a not in avail:
            avail.append(a)
    if len(avail) < 4:
        return {"ok": False, "error": "нужно минимум 4 доступных ингредиента"}
    need = 0
    for g in need_genes or []:
        if g in _FOOD_GENE_NAMES:
            need |= 1 << _FOOD_GENE_NAMES.index(g)
    try:
        max_eat = float(max_eat) if max_eat not in (None, "") else None
    except (TypeError, ValueError):
        max_eat = None
    # eat кратен _FOOD_EAT_K — сравниваем целые произведения балансов
    max_p = int(max_eat / _FOOD_EAT_K + 1e-9) if max_eat is not None else None
    n = len(avail)
    best = {}  # frozenset -> (p, combo, mask)
    for a in avail:
        Ba = bal[a]
        for b in avail:
            if b == a:
                continue
            ab = Ba.get(b, 0)
            if not ab:
                continue
            Bb = bal[b]
            for c in avail:
                if c == a or c == b:
                    continue
                ac = Ba.get(c, 0)
                if not ac:
                    continue
                Bc = bal[c]
                p3 = ab * ac
                g3 = genes[a] ^ genes[b] ^ genes[c]
                for d in avail:
                    if d == a or d == b or d == c:
                        continue
                    p = p3 * Bc.get(d, 0) * Bb.get(d, 0)
                    if not p or (max_p is not None and p > max_p):
                        continue
                    m = g3 ^ genes[d]
                    if m & need != need:
                        continue
                    key = frozenset((a, b, c, d))
                    cur = best.get(key)
                    if cur is None or p > cur[0]:
                        best[key] = (p, (a, b, c, d), m)
    ranked = sorted(best.values(), key=lambda x: (-x[0], -bin(x[2]).count("1")))
    top = ranked[:max(1, min(50, int(top_n or 10)))]
    return {"ok": True, "checked": n * (n - 1) * (n - 2) * (n - 3), "count": len(best),
            "results": [_food_dish(combo, p * _FOOD_EAT_K, m, names) for p, combo, m in top]}


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


_RESTART_CACHE = {}  # world_dir -> (mtime_of_world_performance.txt, epoch|None)


def _last_restart_epoch(world_dir):
    """Эпоха последнего 'Server ready' в Logs\\world_performance.txt — момент,
    когда текущий процесс сервера поднялся. None, если файла/строки нет.

    Нужна, чтобы отличить реально онлайн-игрока от «зависшего»: после
    падения/принудительного рестарта сервер не успевает дописать ``exit`` в
    ``analytics.txt``, и последний ``enter`` такого игрока иначе остался бы
    «онлайн» до его следующего живого входа."""
    p = os.path.join(world_dir, "Logs", "world_performance.txt")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    cached = _RESTART_CACHE.get(world_dir)
    if cached and cached[0] == mt:
        return cached[1]
    epoch = None
    for ln in _read_text(p, tail_bytes=300_000).splitlines():
        if "Server ready" in ln and len(ln) >= 19:
            ep = _to_epoch(ln[:19])
            if ep:
                epoch = ep  # берём последнюю строку с "Server ready" в файле
    _RESTART_CACHE[world_dir] = (mt, epoch)
    return epoch


def parse_analytics(path):
    """-> (per_user: {id: {...}}, events: [ {ts, epoch, kind, id, secs} ] в порядке файла).

    ``per[uid]["online"]`` учитывает поправку на последний рестарт сервера
    (см. ``_last_restart_epoch``): если последний ``enter`` игрока случился ДО
    того, как текущий процесс сервера поднялся, это точно оборванная старым
    процессом сессия — считаем офлайн (и ставим ``stale_online=True``, чтобы
    было видно, что поправка сработала), а не «висим онлайн» до следующего
    захода игрока."""
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

    cutoff = _last_restart_epoch(os.path.dirname(path))
    if cutoff:
        for u in per.values():
            if u["online"] and u["last_epoch"] and u["last_epoch"] < cutoff:
                u["online"] = False
                u["stale_online"] = True
    if not game_running():                   # игра остановлена — никого нет в сети
        for u in per.values():
            if u.get("online"):
                u["online"] = False
                u["server_off"] = True
    return per, events


def _online_now(world_dir):
    """{uid: bool} — кто сейчас online по ``analytics.txt`` (с поправкой на
    последний рестарт сервера, см. ``parse_analytics``)."""
    per, _ = parse_analytics(os.path.join(world_dir, "analytics.txt"))
    return {uid: u["online"] for uid, u in per.items()}


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
            "techs": raw.get("techList") or [],
            "research": raw.get("researchTech") or "",
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


def game_state_space_units(world_dir):
    """Строка 'space unit count = N' из game_state.txt (всего космо-юнитов на сервере)."""
    m = re.search(r"space unit count\s*=\s*(\d+)",
                  _read_text(os.path.join(world_dir, "Logs", "game_state.txt")))
    return int(m.group(1)) if m else None


_SPACEUNITS_CACHE = {}   # (path, star_id) -> (mtime, result)
_CLUSTERS_CACHE = {}     # world_dir -> (ts, [{cluster_id,x,y,star_count,stars}])
_CLUSTERS_TTL = 3600     # статичная генерация галактики — файлы не меняются


def galaxy_clusters(cfg):
    """Кластеры звёздных систем из ``Data\\world\\cluster<N>.json`` (обычный
    JSON, не бинарь: ``{clusterId, starPos{x,y}, starSystems[id…]}``).
    starPos — позиция кластера на карте галактики (др. масштаб, чем позиции
    внутри системы в star<N>.json). Кэш 1 ч.
    -> ``{ok, clusters[{cluster_id,x,y,star_count,stars}], star_count}``."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    now = time.time()
    hit = _CLUSTERS_CACHE.get(world_dir)
    if hit and now - hit[0] < _CLUSTERS_TTL:
        clusters = hit[1]
    else:
        d = os.path.join(world_dir, "Data", "world")
        clusters = []
        try:
            files = os.listdir(d)
        except OSError:
            files = []
        for fn in files:
            m = re.match(r"cluster(\d+)\.json$", fn)
            if not m:
                continue
            try:
                obj = json.loads(_read_text(os.path.join(d, fn)) or "{}")
            except ValueError:
                continue
            stars = obj.get("starSystems") or []
            pos = obj.get("starPos") or {}
            clusters.append({"cluster_id": obj.get("clusterId", int(m.group(1))),
                             "x": pos.get("x"), "y": pos.get("y"),
                             "star_count": len(stars), "stars": stars})
        clusters.sort(key=lambda c: c["cluster_id"])
        _CLUSTERS_CACHE[world_dir] = (now, clusters)
    return {"ok": True, "clusters": clusters,
            "star_count": sum(c["star_count"] for c in clusters)}


def find_cluster_of_star(cfg, star_id):
    """Кластер, которому принадлежит звёздная система ``star_id`` (или None)."""
    g = galaxy_clusters(cfg)
    if not g.get("ok"):
        return None
    try:
        star_id = int(star_id)
    except (TypeError, ValueError):
        return None
    for c in g["clusters"]:
        if star_id in c["stars"]:
            return c
    return None


_SPACEUNITS_RAW_CACHE = {}   # path -> (mtime, parse_space_units result)


def _space_units_raw(path, world_dir):
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None, None
    hit = _SPACEUNITS_RAW_CACHE.get(path)
    if hit and hit[0] == mt:
        return mt, hit[1]
    d = mapdt.parse_space_units(path, world_dir=world_dir)
    _SPACEUNITS_RAW_CACHE[path] = (mt, d)
    return mt, d


def space_units(cfg, star_id=None):
    """Позиции кораблей игроков в космосе из ``Data\\space\\units.dt``
    (снимок на момент последнего сохранения сервера; порт ``SpaceUnit.Read``).
    ``star_id`` (если задан) — оставить только объекты этой звёздной системы
    (в игре несколько систем/кластеров — см. ``galaxy_clusters``); ``None`` —
    все системы разом (как раньше).

    -> ``{ok, star_count, ships[{id,user_id,name,x,y,vx,vy,speed,rotate,health,
    aboard,cargo_items,moving,star_id}], debris_count, total, note}``.
    ``ships`` = не-мусор (``dead_time == 0``); ``debris`` = дрейфующие ящики/
    обломки (``dead_time > 0`` — despawn по serverTime)."""
    if mapdt is None:
        return {"ok": False, "error": "модуль mapdt недоступен"}
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    path = os.path.join(world_dir, "Data", "space", "units.dt")
    sid = None
    if star_id is not None:
        try:
            sid = int(star_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad star_id"}
    ck = (path, sid)
    mt, d = _space_units_raw(path, world_dir)
    if mt is None:
        return {"ok": False, "error": "нет файла space\\units.dt", "ships": [],
                "debris_count": 0, "total": 0}
    hit = _SPACEUNITS_CACHE.get(ck)
    if hit and hit[0] == mt:
        return hit[1]
    if not d.get("ok"):
        return d
    if sid is not None:
        d = dict(d, units=[u for u in d["units"] if u.get("star_id") == sid])
    names = load_user_list(world_dir)
    # владелец космо-юнита: у кого user.spaceUnitId == unit.id
    su_owner = {}
    ud = os.path.join(world_dir, "Data", "users")
    try:
        for nm in os.listdir(ud):
            if not _USER_FILE_RX.match(nm):
                continue
            raw = _read_json(os.path.join(ud, nm)) or {}
            suid = raw.get("spaceUnitId") or 0
            if suid:
                try:
                    su_owner[int(suid)] = int(raw.get("id"))
                except (TypeError, ValueError):
                    pass
    except OSError:
        pass

    blocks = _load_blocks(world_dir)
    ships, meteorites, pods = [], [], []
    other = 0
    stars = set()
    xs, ys = [], []
    for u in d["units"]:
        stars.add(u.get("star_id"))
        xs.append(u["x"]); ys.append(u["y"])
        slug = blocks.get(u["box_type"]) or ""
        bn = _block_label(slug) or ("#%s" % u["box_type"])
        base = {"id": u["id"], "x": round(u["x"], 1), "y": round(u["y"], 1),
                "vx": round(u["vx"], 2), "vy": round(u["vy"], 2),
                "box_type": u["box_type"], "box_name": bn,
                "cargo_items": u.get("inv_items", 0),
                "cargo": u.get("cargo") or [],
                "moving": (abs(u["vx"]) + abs(u["vy"])) > 0.01,
                "star_id": u.get("star_id")}
        # Классификация — по СЫРОМУ англ. слагу из blocks.json, не по переведённому
        # bn (после RU-названий 2026-09-16 bn стал русским, и rocket*/meteorite/
        # space_item больше никогда не совпадали — все объекты молча уходили в
        # "other", корабли/метеориты/предметы пропадали с карты).
        if slug.startswith("rocket"):
            uid = u["user_id"] or su_owner.get(u["id"]) or 0
            base.update(user_id=uid, name=names.get(uid) or ("id %s" % uid if uid else "—"),
                        speed=u["speed"], rotate=u["rotate"], health=u["box_health"],
                        aboard=len(u["aboard"]))
            ships.append(base)
        elif slug == "meteorite":
            meteorites.append(base)
        elif slug == "space_item":
            pods.append(base)
        else:
            other += 1
    ships.sort(key=lambda s: (s["name"] == "—", s["name"].lower()))
    meteorites.sort(key=lambda m: -m["cargo_items"])
    res = {
        "ok": True, "total": len(d["units"]), "star_count": len(stars), "star_id": sid,
        "ships": ships,
        "meteorite_count": len(meteorites), "meteorites": meteorites[:800],
        "pod_count": len(pods), "pods": pods[:400],
        "other_count": other,
        "bounds": {"minx": round(min(xs, default=0)), "maxx": round(max(xs, default=0)),
                   "miny": round(min(ys, default=0)), "maxy": round(max(ys, default=0))},
        "note": ("снимок из space\\units.dt на момент автосохранения сервера "
                 "(≈раз в 12 ч). Это содержимое звёздной системы: корабли-ракеты, "
                 "метеориты (руда) и космо-предметы с координатами и скоростью. "
                 "Планеты (SpaceObject) в файле НЕ хранятся — только по протоколу :45879."),
    }
    _SPACEUNITS_CACHE[ck] = (mt, res)
    if len(_SPACEUNITS_CACHE) > 16:
        _SPACEUNITS_CACHE.pop(next(iter(_SPACEUNITS_CACHE)))
    return res


_SPACEIMG_CACHE = {}   # path -> (mtime, png)
_SPACE_STAR_COL = (255, 225, 140)
_SPACE_SHIP_COL = (90, 200, 255)
_SPACE_MET_COL = (150, 140, 128)
_SPACE_POD_COL = (230, 195, 60)
_SPACE_SATELLITE_COL = (140, 205, 235)
_SPACE_ASTEROID_COL = (170, 125, 80)
_SPACE_PAD_FRAC = 0.05


def _points_bounds(*groups):
    """Границы по фактически ПОКАЗЫВАЕМЫМ точкам (не по сырым ``space_units.bounds``,
    которые включают и скрытых «улетевших» кораблей) — гарантированно включает
    звезду (0,0). ``groups`` — списки dict'ов с ключами x,y (или (x,y) пары)."""
    minx = maxx = miny = maxy = 0
    for g in groups:
        for it in g:
            x, y = (it["x"], it["y"]) if isinstance(it, dict) else it
            minx, maxx = min(minx, x), max(maxx, x)
            miny, maxy = min(miny, y), max(maxy, y)
    return minx, maxx, miny, maxy


_SHIP_LONER_RADIUS = 10000  # корабль без тела (планета/метеорит) ближе этого — считаем «улетел», прячем


def _drop_lone_ships(ships, bodies, radius=_SHIP_LONER_RADIUS):
    """Отфильтровать корабли, у которых нет ни одного тела (планеты/метеорита)
    ближе ``radius`` — такие «улетевшие в никуда» только засоряют схему.
    Если ``bodies`` пуст (нет данных) — ничего не прячем (осторожность)."""
    if not bodies:
        return ships
    r2 = radius * radius
    out = []
    for s in ships:
        sx, sy = s["x"], s["y"]
        if any((sx - bx) ** 2 + (sy - by) ** 2 <= r2 for bx, by in bodies):
            out.append(s)
    return out


# --- Data\world\star<N>.json — именованные объекты системы (планеты/астероиды) ---
# Формат реверс-инжинерен без исходника (сервер-генератор мира не в _src), по
# статистике байт: массив записей переменной длины, для каждой:
#   uint32 <хвост предыдущей записи, НЕ id — пропускаем>
#   int32 nameLen; utf8 name (nameLen байт)
#   int32 type — КАТЕГОРИЯ ОБЪЕКТА, подтверждено 2026-09-17 по геометрии на
#     star1.json: 0=планета (12 шт., 10-38k ед. от звезды), 1=спутник (32 шт.,
#     21-195 ед. от ближайшей планеты — тесная орбита), 2=астероид (389 шт.,
#     267-27234 ед. от ближайшей планеты — разбросаны по системе)
#   7 байт тегов/флагов (не расшифрованы)
#   float64 x; float64 y            <- позиция (ПРОВЕРЕНО: 433/433 валидны на
#                                       star1.json, диапазон разумный ±40k;
#                                       кросс-совпадение с позицией кораблей
#                                       в units.dt на той же планете)
#   … (растровый «хвост» ~314 байт после имени: доп. поля + список ресурсов
#     переменной длины — не распакован, не нужен для имени/позиции)
# Границы записи находятся по соседним строкам: gap между position(name_i) и
# position(name_{i+1}) лежит в 300..340 (= 322+len(name)); это отсекает
# случайные "похожие на строку" байты внутри числовых хвостов.
# ВАЖНО: имена НЕ уникальны — один и тот же пул имён переиспользуется в разных
# звёздных системах с разными координатами (Ryk Xive есть минимум в star1,
# star1029, star1481— с разными x,y). Поиск должен указывать star_id.
_STAROBJ_CACHE = {}   # path -> (mtime, [{"name","x","y"}])


def _is_star_obj_name(s):
    if not (1 <= len(s) <= 40):
        return False
    try:
        s2 = s.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(32 <= ord(c) < 127 for c in s2) and any(c.isalpha() for c in s2)


_STAR_REC_RX = re.compile(rb"\x12(?=.{12})", re.S)   # с перекрытием: 0x12 бывает и внутри чисел


def _parse_star_records(b):
    """Точный разбор по структуре записи SpaceObject: 0x12, uint32 id, int32 −(len+1),
    int32 len, имя (ASCII), int32 type, …, double x (+11 от конца имени), double y (+19)."""
    out, n = [], len(b)
    for m in _STAR_REC_RX.finditer(b):
        i = m.start()
        rid = struct.unpack_from("<I", b, i + 1)[0]
        neg, ln = struct.unpack_from("<i", b, i + 5)[0], struct.unpack_from("<i", b, i + 9)[0]
        if not (1 <= ln <= 40 and neg == -(ln + 1) and i + 13 + ln + 27 <= n):
            continue
        chunk = b[i + 13:i + 13 + ln]
        if not _is_star_obj_name(chunk):
            continue
        end = i + 13 + ln
        typ = struct.unpack_from("<i", b, end)[0]
        x, y = struct.unpack_from("<d", b, end + 11)[0], struct.unpack_from("<d", b, end + 19)[0]
        if typ not in (0, 1, 2) or not (math.isfinite(x) and math.isfinite(y) and abs(x) < 10_000_000 and abs(y) < 10_000_000):
            continue
        out.append({"name": chunk.decode("utf-8"), "type": typ, "kind": {0: "planet", 1: "satellite", 2: "asteroid"}[typ],
                    "x": round(x, 1), "y": round(y, 1), "id": rid})
    return out


_STAR_NAME_CACHE = {}   # путь -> (mtime, имя)


def star_header(b):
    """Заголовок star<N>.json: int32 размер, 0x07, int32 −(len+1), int32 len, имя, int32 кластер,
    байт, double x, double y (позиция системы на карте галактики), double радиус, int32 тип, байт,
    int32 число объектов. -> dict | None"""
    try:
        if len(b) < 13 or b[4] != 0x07:
            return None
        neg, ln = struct.unpack_from("<i", b, 5)[0], struct.unpack_from("<i", b, 9)[0]
        if ln < 0 or ln > 40 or neg != -(ln + 1):
            return None
        p = 13 + ln
        name = b[13:p].decode("utf-8", "replace").strip() or None
        x, y, rad = struct.unpack_from("<d", b, p + 5)[0], struct.unpack_from("<d", b, p + 13)[0], struct.unpack_from("<d", b, p + 21)[0]
        if not all(math.isfinite(v) and abs(v) < 1e9 for v in (x, y, rad)):
            return None
        return {"name": name, "cluster": struct.unpack_from("<i", b, p)[0], "x": round(x, 1), "y": round(y, 1), "radius": round(rad)}
    except struct.error:
        return None


def build_space_index(cfg, path, pause=0.02, stop=None):
    """Индекс всех звёздных систем на диске (``path``, JSON): {star: {mt, size, name, cluster, x, y,
    radius, n, min, max}} — диапазон сквозных id объектов системы (= id карт мира). Инкрементально:
    разбираются только новые/изменённые star*.json, пропавшие — удаляются. ``pause`` — сон между
    файлами (не мешать игре: полный первый проход — минуты). -> {ok, stars, parsed, removed}"""
    wd = find_world_dir(cfg)
    if not wd:
        return {"ok": False, "error": "каталог мира не найден"}
    W = os.path.join(wd, "Data", "world")
    idx = _read_json(path) or {}
    if idx.get("world") != os.path.basename(wd):
        idx = {}
    stars = idx.get("stars") or {}
    try:
        files = {int(m.group(1)): f for f in os.listdir(W) for m in [re.match(r"star(\d+)\.json$", f)] if m}
    except OSError:
        return {"ok": False, "error": "нет Data\\world"}
    parsed, removed = 0, [k for k in list(stars) if int(k) not in files]
    for k in removed:
        stars.pop(k, None)
    for n, (sid, f) in enumerate(sorted(files.items())):
        if stop is not None and stop.is_set():
            break
        fp = os.path.join(W, f)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        old = stars.get(str(sid))
        if old and old.get("mt") == st.st_mtime and old.get("size") == st.st_size:
            continue
        try:
            with open(fp, "rb") as fh:
                b = fh.read()
        except OSError:
            continue
        objs = _parse_star_records(b)
        ids = [o["id"] for o in objs]
        rec = {"mt": st.st_mtime, "size": st.st_size, "n": len(ids),
               "min": min(ids) if ids else None, "max": max(ids) if ids else None}
        rec.update(star_header(b[:128]) or {})
        stars[str(sid)] = rec
        parsed += 1
        if parsed % 200 == 0:
            _save_json(path, {"world": os.path.basename(wd), "updated": time.time(), "stars": stars})
        if pause:
            time.sleep(pause)
    if parsed or removed or not os.path.exists(path):
        _save_json(path, {"world": os.path.basename(wd), "updated": time.time(), "stars": stars})
    return {"ok": True, "stars": len(stars), "parsed": parsed, "removed": len(removed)}


def map_labels(cfg, index_path=None):
    """{id карты: {name, kind, star, star_name}} для всех map*.dt: id карты = сквозной id объекта
    космоса; система ищется по индексу space_index.json (панель игроков строит его раз в час), без
    индекса — система 1 и последняя добавленная. 0 = космос."""
    wd = find_world_dir(cfg)
    if not wd:
        return {}
    try:
        ids = sorted(int(m.group(1)) for f in os.listdir(os.path.join(wd, "Data", "maps"))
                     for m in [re.match(r"map(\d+)\.dt$", f)] if m)
    except OSError:
        ids = []
    rng = []
    idx = _read_json(index_path) if index_path and os.path.exists(index_path) else None
    if idx and idx.get("stars"):
        rng = sorted((v["min"], v["max"], int(k), v.get("name")) for k, v in idx["stars"].items() if v.get("min") is not None)
    else:
        sg = _read_json(os.path.join(wd, "Data", "world", "space_game.json")) or {}
        for st in {1, int(sg.get("curStarId") or 1)}:
            ob = space_objects(cfg, st).get("objects") or []
            if ob:
                rng.append((min(o["id"] for o in ob), max(o["id"] for o in ob), st, star_name(cfg, st)))
        rng.sort()
    import bisect
    objs_by_star, out = {}, {0: {"name": "Космос", "kind": "space"}}
    for mid in ids:
        if mid == 0:
            continue
        i = bisect.bisect_right(rng, (mid, float("inf"), 0, "")) - 1
        if i < 0 or not (rng[i][0] <= mid <= rng[i][1]):
            continue
        st = rng[i][2]
        if st not in objs_by_star:
            objs_by_star[st] = {o["id"]: o for o in space_objects(cfg, st).get("objects") or []}
        o = objs_by_star[st].get(mid)
        if o:
            out[mid] = {"name": o["name"], "kind": _SPACE_KIND_RU.get(o["kind"], o["kind"]), "star": st,
                        "star_name": rng[i][3] or ("#%s" % st)}
    return out


_CLAIMS_CACHE = {}


def claims_by_map(cfg, ttl=120):
    """{id карты: {uid: число участков}} по userTerritories всех игроков (кэш ``ttl`` с)."""
    wd = find_world_dir(cfg)
    if not wd:
        return {}
    hit = _CLAIMS_CACHE.get(wd)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    out = {}
    d = os.path.join(wd, "Data", "users")
    try:
        files = os.listdir(d)
    except OSError:
        files = []
    for f in files:
        m = re.match(r"user(\d+)\.json$", f)
        if not m:
            continue
        raw = _read_json(os.path.join(d, f)) or {}
        uid = int(m.group(1))
        for t in raw.get("userTerritories") or []:
            mp = t.get("mapId")
            if mp:
                cur = out.setdefault(mp, {})
                cur[uid] = cur.get(uid, 0) + 1
    _CLAIMS_CACHE[wd] = (time.time(), out)
    return out


def _index_ranges(index_path):
    idx = _read_json(index_path) if index_path and os.path.exists(index_path) else None
    stars = {int(k): v for k, v in ((idx or {}).get("stars") or {}).items()}
    rng = sorted((v["min"], v["max"], k) for k, v in stars.items() if v.get("min") is not None)
    return stars, rng


def _star_of_id(rng, obj_id):
    import bisect
    i = bisect.bisect_right(rng, (obj_id, float("inf"), 0)) - 1
    return rng[i][2] if i >= 0 and rng[i][0] <= obj_id <= rng[i][1] else None


def admin_space_galaxy(cfg, index_path):
    """Галактика для админки: все системы (позиция, имя, кластер) + по каждой — участков, владельцев,
    кораблей. Нужен индекс space_index.json (строит панель игроков раз в час)."""
    stars, rng = _index_ranges(index_path)
    if not stars:
        return {"ok": False, "error": "индекс систем ещё не построен (панель игроков строит его в фоне раз в час)"}
    names = load_user_list(find_world_dir(cfg))
    per = {}
    for mp, owners in claims_by_map(cfg).items():
        st = _star_of_id(rng, mp)
        if st is None:
            continue
        e = per.setdefault(st, {"claims": 0, "owners": set(), "ships": 0})
        e["claims"] += sum(owners.values())
        e["owners"].update(owners)
    su = space_units(cfg)
    for sh in su.get("ships") or [] if su.get("ok") else []:
        per.setdefault(sh.get("star_id") or 1, {"claims": 0, "owners": set(), "ships": 0})["ships"] += 1
    rows = []
    for k, v in sorted(stars.items()):
        if v.get("x") is None:
            continue
        e = per.get(k) or {}
        rows.append([k, v.get("x"), v.get("y"), v.get("name") or "", v.get("cluster"), e.get("claims", 0),
                     len(e.get("owners") or ()), e.get("ships", 0), v.get("n", 0)])
    top = sorted(((k, e) for k, e in per.items()), key=lambda x: -x[1]["claims"])[:30]
    return {"ok": True, "stars": rows, "columns": ["id", "x", "y", "name", "cluster", "claims", "owners", "ships", "objects"],
            "busiest": [{"star": k, "name": (stars.get(k) or {}).get("name") or "#%s" % k, "claims": e["claims"],
                         "owners": [names.get(u) or "id %s" % u for u in list(e["owners"])[:8]], "ships": e["ships"]} for k, e in top],
            "index_updated": (_read_json(index_path) or {}).get("updated")}


def admin_space_system(cfg, index_path, star_id):
    """Одна система для админки: все объекты (id, имя, тип, x, y) с участками и владельцами, все
    корабли с владельцами и статусом (на земле / у объекта / в полёте / в открытом космосе),
    метеориты, станции."""
    wd = find_world_dir(cfg)
    if not wd:
        return {"ok": False, "error": "каталог мира не найден"}
    try:
        star_id = int(star_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad star"}
    so = space_objects(cfg, star_id)
    if not so.get("ok"):
        return so
    names = load_user_list(wd)
    claims = claims_by_map(cfg)
    stars, _rng = _index_ranges(index_path)
    meta = stars.get(star_id) or {}
    objs = []
    for o in so.get("objects") or []:
        own = claims.get(o["id"]) or {}
        objs.append({"id": o["id"], "name": o["name"], "kind": o["kind"], "x": o["x"], "y": o["y"],
                     "claims": sum(own.values()),
                     "owners": sorted(({"id": u, "name": names.get(u) or "id %s" % u, "n": n} for u, n in own.items()),
                                      key=lambda z: -z["n"])})
    su = space_units(cfg, star_id)
    ships, meteorites = [], []
    if su.get("ok"):
        for sh in su.get("ships") or []:
            near, dist = nearest_space_object(cfg, star_id, sh["x"], sh["y"])
            st = "flight" if sh.get("moving") else ("landed" if near and dist is not None and dist <= 300 else
                                                    ("parked" if near and dist is not None and dist <= 2000 else "open"))
            ships.append({"id": sh["id"], "owner": sh.get("user_id"), "owner_name": sh.get("name"), "model": sh.get("box_name"),
                          "x": sh["x"], "y": sh["y"], "status": st, "near": near, "health": sh.get("health"),
                          "aboard": sh.get("aboard"), "cargo_items": sh.get("cargo_items")})
        meteorites = [[m["x"], m["y"], m.get("cargo_items", 0)] for m in su.get("meteorites") or []]
    stations = [st for st in (space_fleet(cfg).get("stations") or []) if (st.get("star") or 1) == star_id]
    return {"ok": True, "star": star_id, "name": meta.get("name") or star_name(cfg, star_id), "cluster": meta.get("cluster"),
            "radius": meta.get("radius"), "objects": objs, "ships": ships, "meteorites": meteorites,
            "meteorite_count": su.get("meteorite_count", 0) if su.get("ok") else 0, "stations": stations}


def _save_json(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def star_name(cfg, star_id):
    """Имя звёздной системы из заголовка star<N>.json: int32 размер, 0x07, int32 −(len+1),
    int32 len, имя. У системы, добавленной обновлением игры (3129 на .106), имя пустое -> None."""
    wd = find_world_dir(cfg)
    if not wd:
        return None
    path = os.path.join(wd, "Data", "world", "star%d.json" % int(star_id))
    try:
        mt = os.path.getmtime(path)
        hit = _STAR_NAME_CACHE.get(path)
        if hit and hit[0] == mt:
            return hit[1]
        with open(path, "rb") as f:
            b = f.read(64)
    except (OSError, ValueError, TypeError):
        return None
    name = None
    if len(b) >= 13 and b[4] == 0x07:
        neg, ln = struct.unpack_from("<i", b, 5)[0], struct.unpack_from("<i", b, 9)[0]
        if 0 < ln <= 40 and neg == -(ln + 1):
            name = b[13:13 + ln].decode("utf-8", "replace").strip() or None
    _STAR_NAME_CACHE[path] = (mt, name)
    return name


def _parse_star_file(path):
    with open(path, "rb") as f:
        b = f.read()
    exact = _parse_star_records(b)
    if exact:
        return exact
    n = len(b)
    found = []
    i = 0
    while i < n - 4:
        ln = struct.unpack_from("<i", b, i)[0]
        if 1 <= ln <= 40 and i + 4 + ln <= n:
            chunk = b[i + 4:i + 4 + ln]
            if _is_star_obj_name(chunk):
                found.append((i, ln, chunk))
        i += 1
    out = []
    for k in range(len(found) - 1):
        namepos, ln, chunk = found[k]
        if not (300 <= found[k + 1][0] - namepos <= 340):
            continue          # не настоящая граница записи — соседняя случайная "строка"
        end = namepos + 4 + ln
        try:
            typ = struct.unpack_from("<i", b, end)[0]
            x, y = struct.unpack_from("<d", b, end + 11)[0], struct.unpack_from("<d", b, end + 19)[0]
        except struct.error:
            continue
        if not (math.isfinite(x) and math.isfinite(y) and abs(x) < 1_000_000 and abs(y) < 1_000_000):
            continue
        # type 0|1|2 — ПОДТВЕРЖДЕНО по геометрии на star1.json: type1 всегда в
        # 21-195 ед. от ближайшего type0 (орбита спутника), type2 — 267-27234 ед.
        # (разбросаны по системе, астероидный пояс); type0 — 10-38 тыс. ед. от
        # звезды (сами планеты). 0=планета, 1=спутник, 2=астероид.
        kind = {0: "planet", 1: "satellite", 2: "asteroid"}.get(typ, "planet")
        # настоящий id объекта (SpaceObject.id, сквозной по всем системам = id карты мира):
        # запись «… 0x12, uint32 id, int32 −(len+1), int32 len, имя» — сверено на star1
        # (Pojy Bavb=26, Erevai=29) и star3129 (Teqad=751750 = startMapId)
        rid = None
        if namepos >= 9 and b[namepos - 9] == 0x12 and struct.unpack_from("<i", b, namepos - 4)[0] == -(ln + 1):
            rid = struct.unpack_from("<I", b, namepos - 8)[0]
        out.append({"name": chunk.decode("utf-8"), "type": typ, "kind": kind,
                    "x": round(x, 1), "y": round(y, 1), "rid": rid})
    # Запасной разбор (если точный _parse_star_records ничего не нашёл): id — из записи,
    # если распознан, иначе порядковый номер. Порядковый номер НЕНАДЁЖЕН: 25.09 на .106
    # эвристика «соседняя строка через 300–340 байт» теряла объекты 25/33, и номера после них
    # съезжали (спутники Pojy Bavb/Erevai показывались как Nayz/Emavod).
    for idx, o in enumerate(out, 1):
        o["id"] = o.pop("rid") or idx     # без распознанного id — старый способ, по порядку
    return out


def space_objects(cfg, star_id=1):
    """Именованные объекты (планеты/астероиды) звёздной системы ``star_id`` из
    ``Data\\world\\star<star_id>.json`` — реверс-инженерный разбор (нет в
    исходнике), см. комментарий выше. -> ``{ok, star_id, count, objects[{name,x,y}]}``."""
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    try:
        star_id = int(star_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad star_id"}
    path = os.path.join(world_dir, "Data", "world", "star%d.json" % star_id)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {"ok": False, "error": "нет файла world\\star%d.json" % star_id}
    hit = _STAROBJ_CACHE.get(path)
    if hit and hit[0] == mt:
        objs = hit[1]
    else:
        objs = _parse_star_file(path)
        _STAROBJ_CACHE[path] = (mt, objs)
        if len(_STAROBJ_CACHE) > 8:
            _STAROBJ_CACHE.pop(next(iter(_STAROBJ_CACHE)))
    return {"ok": True, "star_id": star_id, "name": star_name(cfg, star_id), "count": len(objs), "objects": objs,
            "note": "разбор бинарного формата без исходника — координаты x,y проверены "
                    "(валидны на 100% образцов, совпадают с позицией кораблей на той же "
                    "точке); имена НЕ уникальны между звёздными системами"}


def space_object_search(cfg, query, star_id=1):
    """Поиск объекта по (под)имени в звёздной системе ``star_id``.
    -> ``{ok, query, star_id, matches[{id,name,x,y}], total_in_star, note}``."""
    d = space_objects(cfg, star_id)
    if not d.get("ok"):
        return d
    q = str(query or "").strip().lower()
    if not q:
        return {"ok": False, "error": "пустой запрос"}
    matches = [o for o in d["objects"] if q in o["name"].lower()]
    matches.sort(key=lambda o: (o["name"].lower() != q, len(o["name"]), o["name"]))
    return {"ok": True, "query": query, "star_id": star_id, "matches": matches[:100],
            "total_in_star": d["count"], "note": d["note"]}


def space_map_points(cfg, star_id=1):
    """Данные для интерактивной схемы системы (id/координаты/детали каждой
    точки) — картинка рисуется отдельно (``space_map_image``), тут только
    метаданные для наведения. Клиент считает пиксель сам: ``pad = size*pad_frac``,
    ``px = pad + (x-minx)/(maxx-minx)*(size-2*pad)``,
    ``py = pad + (maxy-y)/(maxy-miny)*(size-2*pad)``.
    -> ``{ok, bounds{minx,maxx,miny,maxy}, pad_frac, points[{kind,id,x,y,...}]}``."""
    su = space_units(cfg, star_id=star_id)
    if not su.get("ok"):
        return su
    so = space_objects(cfg, star_id)
    objs = so.get("objects") or [] if so.get("ok") else []
    bodies = [(o["x"], o["y"]) for o in objs] + [(m["x"], m["y"]) for m in su["meteorites"]]
    ships = _drop_lone_ships(su["ships"], bodies)
    minx, maxx, miny, maxy = _points_bounds(objs, su["meteorites"], su["pods"], ships)
    points = [{"kind": "star", "id": 0, "x": 0, "y": 0, "name": "★"}]
    for o in objs:
        points.append({"kind": o.get("kind", "planet"), "id": o["id"], "x": o["x"], "y": o["y"], "name": o["name"]})
    for s in ships:
        points.append({"kind": "ship", "id": s["id"], "x": s["x"], "y": s["y"],
                       "name": s["name"], "user_id": s["user_id"], "health": s["health"],
                       "speed": s.get("speed"), "vx": s["vx"], "vy": s["vy"],
                       "cargo_items": s["cargo_items"], "aboard": s.get("aboard"),
                       "moving": s["moving"], "box_name": s["box_name"]})
    for m in su["meteorites"]:
        points.append({"kind": "meteorite", "id": m["id"], "x": m["x"], "y": m["y"],
                       "vx": m["vx"], "vy": m["vy"], "cargo_items": m["cargo_items"],
                       "moving": m["moving"]})
    for p in su["pods"]:
        points.append({"kind": "pod", "id": p["id"], "x": p["x"], "y": p["y"],
                       "vx": p["vx"], "vy": p["vy"], "cargo_items": p["cargo_items"],
                       "moving": p["moving"]})
    return {"ok": True, "bounds": {"minx": minx, "maxx": maxx, "miny": miny, "maxy": maxy},
            "pad_frac": _SPACE_PAD_FRAC, "points": points, "total": len(points),
            "planet_count": sum(1 for o in objs if o.get("kind") == "planet"),
            "satellite_count": sum(1 for o in objs if o.get("kind") == "satellite"),
            "asteroid_count": sum(1 for o in objs if o.get("kind") == "asteroid"),
            "object_count": len(objs),
            "ships_hidden": len(su["ships"]) - len(ships)}


_SPACE_PLANET_COL = (190, 175, 230)


_SPACE_ALL_KINDS = frozenset({"planet", "satellite", "asteroid", "ship", "meteorite", "pod"})


def space_map_image(cfg, size=760, star_id=1, show=None):
    """Рассеянная диаграмма звёздной системы (PNG): звезда в (0,0), именованные
    объекты из ``Data\\world\\star<N>.json`` — планеты лавандовым, спутники
    голубым, астероиды коричневым (по полю ``type``, см. комментарий у
    ``_parse_star_file``), метеориты/поды/корабли (из ``space_units``) по их
    координатам. Не карта местности — просто визуализация того, что реально
    известно. ``show`` — множество видов для отрисовки (подмножество
    ``_SPACE_ALL_KINDS``); ``None`` = все. Звезда рисуется всегда (точка
    отсчёта). Масштаб/границы картинки не зависят от фильтра — считаются по
    ПОЛНОМУ набору объектов, чтобы включение/выключение слоя не «прыгало».
    -> ``(png_bytes, fname, meta)``."""
    if mapdt is None:
        return {"ok": False, "error": "модуль mapdt недоступен"}, None, None
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}, None, None
    path = os.path.join(world_dir, "Data", "space", "units.dt")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {"ok": False, "error": "нет файла space\\units.dt"}, None, None
    size = max(200, min(2000, int(size)))
    try:
        star_id = int(star_id)
    except (TypeError, ValueError):
        star_id = 1
    star_path = os.path.join(world_dir, "Data", "world", "star%d.json" % star_id)
    try:
        star_mt = os.path.getmtime(star_path)
    except OSError:
        star_mt = 0
    show = _SPACE_ALL_KINDS if show is None else (set(show) & _SPACE_ALL_KINDS)
    ck = (path, size, star_id, frozenset(show))
    hit = _SPACEIMG_CACHE.get(ck)
    if hit and hit[0] == (mt, star_mt):
        return hit[1], "space_map.png", {"cached": True}

    su = space_units(cfg, star_id=star_id)
    if not su.get("ok"):
        return su, None, None
    so = space_objects(cfg, star_id)
    objs = so.get("objects") or [] if so.get("ok") else []
    bodies = [(o["x"], o["y"]) for o in objs] + [(m["x"], m["y"]) for m in su["meteorites"]]
    ships = _drop_lone_ships(su["ships"], bodies)
    minx, maxx, miny, maxy = _points_bounds(objs, su["meteorites"], su["pods"], ships)
    spanx = max(maxx - minx, 1)
    spany = max(maxy - miny, 1)
    w = h = size
    pad = int(size * _SPACE_PAD_FRAC)

    def to_px(x, y):
        px = pad + (x - minx) / spanx * (w - 2 * pad)
        py = pad + (maxy - y) / spany * (h - 2 * pad)   # y вниз на экране
        return int(px), int(py)

    rgb = bytearray((8, 10, 22) * (w * h))

    def dot(cx, cy, color, r):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx * dx + dy * dy > r * r + 1:
                    continue
                x, y = cx + dx, cy + dy
                if 0 <= x < w and 0 <= y < h:
                    i = (y * w + x) * 3
                    rgb[i], rgb[i + 1], rgb[i + 2] = color

    _obj_col = {"planet": _SPACE_PLANET_COL, "satellite": _SPACE_SATELLITE_COL,
                "asteroid": _SPACE_ASTEROID_COL}
    _obj_r = {"planet": 2, "satellite": 1, "asteroid": 1}
    for o in objs:
        k = o.get("kind", "planet")
        if k not in show:
            continue
        x, y = to_px(o["x"], o["y"])
        dot(x, y, _obj_col.get(k, _SPACE_PLANET_COL), _obj_r.get(k, 1))
    if "meteorite" in show:
        for m in su["meteorites"]:
            x, y = to_px(m["x"], m["y"])
            dot(x, y, _SPACE_MET_COL, 1 if m["cargo_items"] < 12 else 2)
    if "pod" in show:
        for p in su["pods"]:
            x, y = to_px(p["x"], p["y"])
            dot(x, y, _SPACE_POD_COL, 1)
    if "ship" in show:
        for s in ships:
            x, y = to_px(s["x"], s["y"])
            dot(x, y, _SPACE_SHIP_COL, 3)
    sx, sy = to_px(0, 0)
    dot(sx, sy, _SPACE_STAR_COL, 5)

    png = mapdt.png_bytes(w, h, bytes(rgb), 1)
    _SPACEIMG_CACHE[ck] = ((mt, star_mt), png)
    if len(_SPACEIMG_CACHE) > 32:  # разные ?show= комбинации — иначе кэш растёт без предела
        _SPACEIMG_CACHE.pop(next(iter(_SPACEIMG_CACHE)))
    return png, "space_map.png", {
        "w": w, "h": h, "bounds": {"minx": minx, "maxx": maxx, "miny": miny, "maxy": maxy},
        "planets": len(objs), "ships": len(ships), "ships_hidden": len(su["ships"]) - len(ships),
        "meteorites": su["meteorite_count"], "pods": su["pod_count"], "show": sorted(show)}


def space_report(cfg):
    """Аналитика по космосу (карта 0). Из user<N>.json (mapId, spaceUnitId,
    userTerritories) + analytics (реальный онлайн) + game_state (space unit count).
    Детали кораблей (топливо/груз/HP) в JSON недоступны — они в бинарных картах.
    """
    world_dir = find_world_dir(cfg)
    if not world_dir:
        return {"ok": False, "error": "каталог мира не найден"}
    names = load_user_list(world_dir)
    online_now = _online_now(world_dir)

    d = os.path.join(world_dir, "Data", "users")
    try:
        listing = os.listdir(d)
    except OSError:
        listing = []
    in_space, has_ship = [], 0
    planet_terr = {}          # map -> {owner_id: plots}
    for nm in listing:
        mm = _USER_FILE_RX.match(nm)
        if not mm:
            continue
        raw = _read_json(os.path.join(d, nm))
        try:
            uid = int(raw.get("id") if raw.get("id") is not None else mm.group(1))
        except (TypeError, ValueError):
            continue
        if raw.get("spaceUnitId"):
            has_ship += 1
        if raw.get("mapId") == 0:
            online = online_now.get(uid, False)
            in_space.append({"id": uid, "name": names.get(uid) or ("id %d" % uid),
                             "level": raw.get("unitLevel"), "online": online,
                             "space_unit": raw.get("spaceUnitId"),
                             "stuck": not online})
        for tt in (raw.get("userTerritories") or []):
            mp = tt.get("mapId")
            if mp not in (0, 1, None):
                planet_terr.setdefault(mp, {})
                planet_terr[mp][uid] = planet_terr[mp].get(uid, 0) + 1

    in_space.sort(key=lambda x: (x["online"], -(x["level"] or 0)), reverse=True)
    planets = []
    for mp, owners in sorted(planet_terr.items(), key=lambda kv: -sum(kv[1].values())):
        plist = sorted(({"id": o, "name": names.get(o) or ("id %d" % o), "plots": n}
                        for o, n in owners.items()), key=lambda x: -x["plots"])
        planets.append({"map": mp, "plots": sum(owners.values()),
                        "owner_count": len(owners), "owners": plist[:20]})

    return {
        "ok": True,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "in_space": in_space,
        "in_space_count": len(in_space),
        "stuck_offline": sum(1 for x in in_space if x["stuck"]),
        "space_units_total": game_state_space_units(world_dir),
        "has_ship": has_ship,
        "registered": len(names),
        "planets": planets,
        "note": "карта 0 = звёздная карта (космос); детали кораблей в JSON недоступны (бинарные map*.dt)",
    }


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
            "stale_online": bool(a.get("stale_online")),
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
    stale_n = sum(1 for u in users if u["stale_online"])
    recent = []
    for e in events[-recent_limit:][::-1]:
        recent.append({"ts": e["ts"], "kind": e["kind"], "id": e["id"],
                       "name": names.get(e["id"], "id %s" % e["id"]), "secs": e["secs"]})

    restart_ep = _last_restart_epoch(world_dir)
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
            "online_space": next((x["count"] for x in by_map if x["map"] == 0), 0),
            "stale_online": stale_n,
            "last_restart": (datetime.fromtimestamp(restart_ep).strftime("%Y-%m-%d %H:%M:%S")
                              if restart_ep else None),
        },
        "by_map": by_map,
        "users": users,
        "recent": recent,
    }


# ------------------------------------------------------ анализ генерации космоса
_GEN_GAP = 1800          # разрыв между созданиями звёзд больше 30 мин = генерация мира закончилась
_BACKUP_SG_CACHE = {}    # путь zip -> (mtime, состояние space_game + макс. номер звезды)


def _dt(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else None


def _ctime(path):
    try:
        return os.stat(path).st_ctime
    except OSError:
        return None


def _steam_update(cfg):
    """Когда Steam последний раз обновлял игру (appmanifest) -> {time, build} | None."""
    inst = cfg.get("game_install_dir") or ""
    appid = cfg.get("game_appid") or 1690980
    cand = [os.path.join(inst, "..", "..", "appmanifest_%s.acf" % appid)] if inst else []
    cand.append(r"C:\Program Files (x86)\Steam\steamapps\appmanifest_%s.acf" % appid)
    for p in cand:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                t = f.read()
        except OSError:
            continue
        lu = re.search(r'"LastUpdated"\s+"(\d+)"', t)
        bid = re.search(r'"buildid"\s+"(\d+)"', t)
        return {"t": int(lu.group(1)) if lu else None, "time": _dt(int(lu.group(1))) if lu else None,
                "build": bid.group(1) if bid else None}
    return None


def _backup_space_state(zpath):
    """space_game.json и наибольший номер звезды внутри бэкапа мира (читается только
    оглавление zip и один маленький файл)."""
    mt = os.path.getmtime(zpath)
    hit = _BACKUP_SG_CACHE.get(zpath)
    if hit and hit[0] == mt:
        return hit[1]
    st = None
    try:
        with zipfile.ZipFile(zpath) as zf:
            names = zf.namelist()
            sg = sorted((n for n in names if n.replace("\\", "/").endswith("world/space_game.json")), key=len)
            stars = [int(m.group(1)) for n in names for m in [re.search(r"world[/\\]star(\d+)\.json$", n)] if m]
            clusters = sum(1 for n in names if re.search(r"world[/\\]cluster\d+\.json$", n))
            d = json.loads(zf.read(sg[0]).decode("utf-8", "replace")) if sg else {}
            st = {"curStarId": d.get("curStarId"), "startMapId": d.get("startMapId"),
                  "curObjectId": d.get("curObjectId"), "countClusters": d.get("countClusters"),
                  "startClusterId": d.get("startClusterId"), "updateMapId": d.get("updateMapId"),
                  "stars": len(stars), "maxStar": max(stars) if stars else None, "clusterFiles": clusters}
    except (OSError, zipfile.BadZipFile, ValueError, KeyError, IndexError):
        st = None
    _BACKUP_SG_CACHE[zpath] = (mt, st)
    return st


def space_generation(cfg):
    """Что в космосе (Data\\world: звёзды, кластеры, space_game.json) создано или
    изменено ПОСЛЕ первоначальной генерации мира, и когда. Окно генерации — от
    первой звезды, пока следующая создана не позже чем через 30 мин после предыдущей.
    История — по бэкапам мира (backup\\arhN.zip) и обновлениям игры в Steam."""
    wd = find_world_dir(cfg)
    if not wd:
        return {"ok": False, "error": "каталог мира не найден"}
    W = os.path.join(wd, "Data", "world")
    stars = []
    try:
        listing = os.listdir(W)
    except OSError:
        return {"ok": False, "error": "нет каталога Data\\world"}
    for f in listing:
        m = re.match(r"star(\d+)\.json$", f)
        if m:
            try:
                s = os.stat(os.path.join(W, f))
            except OSError:
                continue
            stars.append({"id": int(m.group(1)), "ct": s.st_ctime, "mt": s.st_mtime, "size": s.st_size})
    if not stars:
        return {"ok": False, "error": "в Data\\world нет star*.json"}
    by_ct = sorted(stars, key=lambda s: s["ct"])
    gen_start = gen_end = by_ct[0]["ct"]
    for s in by_ct[1:]:
        if s["ct"] - gen_end > _GEN_GAP:
            break
        gen_end = s["ct"]
    edge = gen_end + 600

    def s_after(r):
        """Секунд от конца генерации до изменения системы."""
        return datetime.strptime(r["modified"], "%Y-%m-%d %H:%M:%S").timestamp() - gen_end

    clusters, star_cluster, cl_lists = [], {}, {}
    for f in listing:
        m = re.match(r"cluster(\d+)\.json$", f)
        if not m:
            continue
        p = os.path.join(W, f)
        c = _read_json(p) or {}
        try:
            s = os.stat(p)
        except OSError:
            continue
        cl_lists[int(m.group(1))] = list(c.get("starSystems") or [])
        for sid in c.get("starSystems") or []:
            star_cluster[sid] = int(m.group(1))
        clusters.append({"id": int(m.group(1)), "ct": s.st_ctime, "mt": s.st_mtime,
                         "stars": len(c.get("starSystems") or [])})
    sg = _read_json(os.path.join(W, "space_game.json")) or {}
    sg_bak = _read_json(os.path.join(W, "space_game.json.bak")) or {}
    steam = _steam_update(cfg)

    def star_info(s, why):
        try:
            objs = _parse_star_file(os.path.join(W, "star%d.json" % s["id"]))
        except OSError:
            objs = []
        kinds = collections.Counter(o["kind"] for o in objs)
        cl = star_cluster.get(s["id"])
        r = {"id": s["id"], "name": star_name(cfg, s["id"]), "why": why, "created": _dt(s["ct"]), "modified": _dt(s["mt"]), "size": s["size"],
             "cluster": cl, "start_cluster": cl is not None and cl == sg.get("startClusterId"),
             "objects": len(objs), "planets": kinds.get("planet", 0), "satellites": kinds.get("satellite", 0),
             "asteroids": kinds.get("asteroid", 0),
             "planet_names": [o["name"] for o in objs if o["kind"] == "planet"][:20]}
        t = s["ct"] if why == "created" else s["mt"]
        if steam and steam.get("t") and 0 <= t - steam["t"] <= 6 * 3600:
            r["after_update"] = "через %d мин после обновления игры (сборка %s)" % ((t - steam["t"]) // 60, steam.get("build"))
        return r

    # Независимо от дат файлов (мир могли скопировать/восстановить): при генерации кластеры
    # получают звёзды подряд — 1..21, 22..35, … Звезда вне этой последовательности
    # (например, 3129 в конце списка кластера 1) добавлена позже.
    out_of_order, nxt = [], 1
    for cid in sorted(cl_lists):
        for sid in cl_lists[cid]:
            if sid == nxt:
                nxt += 1
            else:
                out_of_order.append({"cluster": cid, "star": sid})
    ooo_ids = {x["star"] for x in out_of_order}
    late = [star_info(s, "created") for s in sorted(stars, key=lambda s: s["id"]) if s["ct"] > edge or s["id"] in ooo_ids]
    for r in late:
        r["out_of_order"] = r["id"] in ooo_ids
    changed = [star_info(s, "modified") for s in sorted(stars, key=lambda s: s["id"])
               if s["ct"] <= edge and s["mt"] > edge and s["id"] not in ooo_ids]
    ids = {s["id"] for s in stars}
    missing = sorted(set(range(1, max(ids) + 1)) - ids)
    cl_late = [{"id": c["id"], "created": _dt(c["ct"]), "modified": _dt(c["mt"]), "stars": c["stars"],
                "new": c["ct"] > edge} for c in sorted(clusters, key=lambda c: c["id"]) if c["mt"] > edge]

    # стартовая карта новичков: когда впервые созданы её файлы
    start = None
    smid = sg.get("startMapId")
    if smid is not None:
        D = os.path.join(wd, "Data")
        start = {"map": smid, "was": sg_bak.get("startMapId"),
                 "files": [{"file": rel, "created": _dt(_ctime(os.path.join(D, rel)))}
                           for rel in ("maps\\map%s.dt" % smid, "map_info\\map%s.json" % smid, "units\\bots%s" % smid)
                           if _ctime(os.path.join(D, rel))]}

    # история по бэкапам: только моменты, когда состояние космоса менялось
    hist, prev = [], None
    zips = sorted(glob.glob(os.path.join(wd, "backup", "*.zip")), key=os.path.getmtime)
    keys = ("curStarId", "maxStar", "stars", "startMapId", "curObjectId", "countClusters", "startClusterId", "clusterFiles")
    for z in zips:
        st = _backup_space_state(z)
        if not st:
            continue
        diff = {k: [prev.get(k), st.get(k)] for k in keys if prev and prev.get(k) != st.get(k)}
        if prev is None or diff:
            hist.append({"backup": os.path.basename(z), "time": _dt(os.path.getmtime(z)), "state": st, "diff": diff})
        prev = st
    now_state = {"curStarId": sg.get("curStarId"), "maxStar": max(ids), "stars": len(ids),
                 "startMapId": sg.get("startMapId"), "curObjectId": sg.get("curObjectId"),
                 "countClusters": sg.get("countClusters"), "startClusterId": sg.get("startClusterId"),
                 "clusterFiles": len(clusters)}
    diff = {k: [prev.get(k), now_state.get(k)] for k in keys if prev and prev.get(k) != now_state.get(k)}
    if prev is None or diff:
        hist.append({"backup": "сейчас", "time": _dt(time.time()), "state": now_state, "diff": diff})

    # выводы простым текстом
    concl = ["Генерация мира: %s — %s, звёздных систем %d, кластеров %d."
             % (_dt(gen_start), _dt(gen_end), sum(1 for s in stars if s["ct"] <= edge and s["id"] not in ooo_ids),
                len(clusters))]
    if not late and not changed:
        concl.append("После генерации ни одна звёздная система не создавалась и не менялась.")
    for r in late:
        concl.append("Создана система #%d%s: %d объектов (планет %d, спутников %d, астероидов %d) — %s%s."
                     % (r["id"], " в стартовом кластере %s" % r["cluster"] if r["start_cluster"]
                        else (" в кластере %s" % r["cluster"] if r["cluster"] else " (ни в одном кластере!)"),
                        r["objects"], r["planets"], r["satellites"], r["asteroids"], r["created"],
                        ", " + r["after_update"] if r.get("after_update") else ""))
    for r in late:
        if r.get("out_of_order"):
            concl.append("Система #%d стоит в кластере %s вне порядка первоначальной генерации — добавлена позже "
                         "(этот признак не зависит от дат файлов)." % (r["id"], r["cluster"]))
    # изменения подряд (в пределах минуты) — одной строкой: игра переписывает сразу группу систем
    grp = []
    for r in sorted(changed, key=lambda r: (r["modified"], r["id"])):
        if grp and r["modified"][:16] == grp[-1][-1]["modified"][:16]:
            grp[-1].append(r)
        else:
            grp.append([r])
    for g in grp:
        ids_s = ("#%d" % g[0]["id"]) if len(g) == 1 else "#%d–#%d (%d шт.)" % (g[0]["id"], g[-1]["id"], len(g))
        startcl = all(r["start_cluster"] for r in g)
        concl.append("Изменены системы %s%s, созданные при генерации — %s%s%s." % (
            ids_s, " стартового кластера" if startcl else "", g[0]["modified"],
            ", " + g[0]["after_update"] if g[0].get("after_update") else "",
            " (вскоре после генерации — похоже на первый запуск мира, игра обновляет стартовую зону)"
            if s_after(g[0]) < 3600 else ""))
    if missing:
        concl.append("Нет файлов звёзд с номерами: %s%s." % (", ".join(map(str, missing[:20])), "…" if len(missing) > 20 else ""))
    if start and start["was"] is not None and start["was"] != start["map"]:
        concl.append("Стартовая карта новичков сменилась: %s → %s%s." % (
            start["was"], start["map"], (" (впервые создана %s)" % start["files"][0]["created"]) if start["files"] else ""))
    for h in hist[1:]:
        if h["diff"]:
            concl.append("%s (%s) по сравнению с предыдущим бэкапом: %s." % (
                "Сейчас" if h["backup"] == "сейчас" else "Бэкап " + h["backup"], h["time"], "; ".join(
                "%s %s → %s" % (k, v[0], v[1]) for k, v in h["diff"].items())))
    if steam and steam.get("time"):
        concl.append("Последнее обновление игры в Steam: %s (сборка %s)." % (steam["time"], steam.get("build")))
    return {"ok": True, "world": os.path.basename(wd), "generation": {"start": _dt(gen_start), "end": _dt(gen_end)},
            "stars_total": len(stars), "max_star": max(ids), "missing": missing[:200], "clusters_total": len(clusters),
            "late_stars": late, "changed_stars": changed, "clusters_changed": cl_late, "out_of_order": out_of_order,
            "space_game": sg, "space_game_bak": sg_bak, "start_map": start, "steam": steam,
            "history": hist, "backups": len(zips), "conclusions": concl}
