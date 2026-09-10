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
        "clan_members": clan["members"] if clan else [],
    }


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
