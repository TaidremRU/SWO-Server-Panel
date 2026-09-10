# -*- coding: utf-8 -*-
"""Парсер бинарных карт Sigma World Online (``Data\\maps\\map<N>.dt``).

Точный порт сериализации из исходника игры (Unity-проект ``SigmaWorldClient``,
``Assets/Plugins/ZData/``): ``Map.Load`` / ``MapCell.Read`` / ``Block.Read`` /
``Inventory.Read`` / ``Item.Read`` / ``Machine.Read`` / ``Transport.Read`` /
``Shop.Read`` / ``Unit.Read``. Формат — обычный .NET ``BinaryReader`` (LE), без
сжатия.

Раскладка файла (``mapVersion`` = 7)::

    int32   version                         (если исходный version > 6)
    double  mapServerTime
    int16   width, int16 height
    width*height * MapCell
    int32 n; n * {int16 x, int16 y, int32 type}      # stonePos (руда/камень)
    int32 n; n * int32                                # biomCount
    (width/8)*(height/8) * uint32                     # userMap — владелец блока 8x8
    [если карта без кислорода] width*height * float32 # oxygenMap

Животных в файле НЕТ (спавнятся в рантайме). Есть: террейн/блоки/постройки, руда,
контейнеры с предметами, машины, газ/заражение, сетка владения землёй.

``summary(path, cfg=None)`` -> агрегаты для панели (без сырой сетки).
"""
import io
import json
import logging
import os
import struct
from collections import Counter

MAP_VERSION = 7


class _R:
    """Читалка поверх bytes, семантика .NET BinaryReader (little-endian)."""

    __slots__ = ("b", "p", "n")

    def __init__(self, data):
        self.b = data
        self.p = 0
        self.n = len(data)

    def _take(self, k):
        if self.p + k > self.n:
            raise EOFError("нужно %d байт на позиции %d, осталось %d" % (k, self.p, self.n - self.p))
        v = self.b[self.p:self.p + k]
        self.p += k
        return v

    def i8(self):
        return self._take(1)[0]

    def boolean(self):
        return self._take(1)[0] != 0

    def i16(self):
        return struct.unpack_from("<h", self._take(2))[0]

    def i32(self):
        return struct.unpack_from("<i", self._take(4))[0]

    def u32(self):
        return struct.unpack_from("<I", self._take(4))[0]

    def i64(self):
        return struct.unpack_from("<q", self._take(8))[0]

    def u64(self):
        return struct.unpack_from("<Q", self._take(8))[0]

    def f32(self):
        return struct.unpack_from("<f", self._take(4))[0]

    def f64(self):
        return struct.unpack_from("<d", self._take(8))[0]

    def vec2w(self):
        return (self.i16(), self.i16())

    def rest(self):
        return self.n - self.p


# --------------------------------------------------------------- отрисовка карты
# базовые цвета (RGB) по категории тайла
_COL = {
    "water": (38, 88, 150), "land": (176, 160, 126), "grass": (86, 148, 66),
    "plant": (44, 104, 48), "mtn": (128, 128, 134), "ore": (122, 108, 92),
    "wall": (228, 202, 72), "floor": (206, 186, 118), "built": (222, 138, 46),
    "unknown": (150, 120, 120),
}
_CLAIM_HL = (90, 235, 110)       # выделение конкретного владельца
# палитра для клаймов (по владельцу) — насыщенные различимые оттенки
_CLAIM_PAL = [
    (231, 76, 60), (46, 134, 193), (241, 196, 15), (155, 89, 182),
    (26, 188, 156), (230, 126, 34), (52, 152, 219), (243, 156, 18),
    (142, 68, 173), (39, 174, 96), (211, 84, 0), (41, 128, 185),
    (192, 57, 43), (22, 160, 133), (127, 140, 141), (243, 104, 224),
    (109, 76, 65), (33, 97, 140), (125, 206, 160), (203, 67, 53),
    (247, 220, 111), (169, 50, 38), (84, 153, 199), (240, 178, 122),
]


def _classify(c, bc):
    """категория тайла для карты-картинки. bc = {block_type: 'mtn'|'ore'|'wall'|
    'floor'|'built'|'plant'|'aqua'|''}. 'aqua' (водоросли/кувшинки) — не растение."""
    b = c.get("block")
    if b:
        k = bc.get(b["type"], "")
        if k == "aqua":
            return "water" if not c.get("ground") else "land"
        if k in ("mtn", "ore", "wall", "floor", "built", "plant"):
            return k
        return "built"                     # неизвестный блок — скорее постройка
    if c.get("machine"):
        return "built"
    g = c.get("grass")
    if g:
        k = bc.get(g["type"], "")
        if k in ("wall", "floor"):
            return k
        if k == "plant":
            return "plant"
        if k == "aqua":
            return "water" if not c.get("ground") else "land"
        return "grass"
    if c.get("box"):
        return "built"
    if not c.get("ground"):
        return "water"
    return "land"


def _blend(a, b, t):
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def _png_bytes(w, h, rgb, scale=1):
    """Минимальный кодировщик PNG (RGB8, фильтр 0), только stdlib. ``rgb`` —
    bytes длиной w*h*3, строки по y. ``scale`` — целочисленный nearest-upscale."""
    import zlib
    s = max(1, int(scale))
    ow, oh = w * s, h * s
    stride = w * 3
    raw = bytearray()
    for y in range(h):
        line = rgb[y * stride:(y + 1) * stride]
        if s > 1:
            wide = bytearray(ow * 3)
            for x in range(w):
                px = line[x * 3:x * 3 + 3]
                base = x * s * 3
                for k in range(s):
                    wide[base + k * 3:base + k * 3 + 3] = px
            line = bytes(wide)
        for _ in range(s):
            raw.append(0)
            raw += line
    comp = zlib.compress(bytes(raw), 6)

    def _ch(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + _ch(b"IHDR", struct.pack(">IIBBBBB", ow, oh, 8, 2, 0, 0, 0))
            + _ch(b"IDAT", comp) + _ch(b"IEND", b""))


# ------------------------------------------------------------ вложенные структуры
# ctx (мутируемый, один на разбор): {"want": set|None, "hits": list,
#   "x": int, "y": int, "where": str, "cap": int} — сбор координат предметов.
def _read_item(r, version, item_ext, ctx):
    it = {"type": r.i32(), "count": r.i32()}
    it["durability"] = r.i32() if version == 0 else r.f32()
    it["extData"] = r.u64()
    it["life"] = r.f64()
    if r.boolean():  # ext
        ext_id = r.u64()
        if r.boolean():  # есть доп-данные
            is_dish, is_mix = item_ext.get(it["type"], (False, False))
            if is_dish:
                r.f32()                       # eat
                r._take(4)                    # 4 * bool genes
            if is_mix:
                r.f32()                       # time
                for _ in range(r.i32()):      # buffs
                    r.i32()
                    r.f32()
    w = ctx["want"]
    if w is not None and it["type"] in w and len(ctx["hits"]) < ctx["cap"]:
        ctx["hits"].append({"x": ctx["x"], "y": ctx["y"], "where": ctx["where"],
                            "type": it["type"], "count": it["count"],
                            "durability": round(it["durability"], 1) if it.get("durability") else 0})
    return it


def _read_inventory(r, version, item_ext, ctx):
    n = r.i32()
    items = [_read_item(r, version, item_ext, ctx) for _ in range(n)]
    size = r.i32()
    is_limit = r.boolean()
    is_limit_stack = r.boolean()
    return {"items": items, "size": size, "is_limit": is_limit,
            "is_limit_stack": is_limit_stack}


def _read_machine(r, version, item_ext, ctx):
    m = {"type": r.i32()}
    _w = ctx["where"]
    ctx["where"] = _w + "/machine.material"
    m["material"] = _read_item(r, version, item_ext, ctx) if r.boolean() else None
    ctx["where"] = _w + "/machine.product"
    m["product"] = _read_item(r, version, item_ext, ctx) if r.boolean() else None
    ctx["where"] = _w + "/machine.fuel"
    m["fuel"] = _read_item(r, version, item_ext, ctx) if r.boolean() else None
    ctx["where"] = _w
    m["energy"] = r.f32()
    return m


def _read_unit(r, version, bot_version, item_ext, ctx):
    u = {"id": r.u64(), "user_id": r.u32(), "species": r.u32(), "gender": r.i32()}
    r.f64()                                   # timeGrowing
    u["map"] = r.u32()
    u["pos"] = r.vec2w()
    _w = ctx["where"]
    ctx["where"] = _w + "/unit"
    u["inventory"] = _read_inventory(r, version, item_ext, ctx)
    ctx["where"] = _w
    for _ in range(r.i32()):                  # paramList
        r.i32(); r.f32(); r.f32()
    for _ in range(r.i32()):                  # paramLongList
        r.i32(); r.i64(); r.i64()
    for _ in range(r.i32()):                  # skillLevels
        r.i32(); r.i32()
    for _ in range(r.i32()):                  # orderAddSkills
        r.i32(); r.i64()
    if r.boolean():                           # childInfo
        for _ in range(r.i32()):
            r.u32(); r.i32()
    r.f64(); r.f64()                          # timePostpartumRecovery, timeCreateChilds
    u["is_grown"] = r.boolean()
    r.f32()                                   # timePrepareAttack
    if r.boolean():                           # box
        _read_block(r, version, item_ext, ctx)
    r.i32(); r.i32(); r.i32()                 # viewInfo: view(Pair int,int), color
    if r.boolean():                           # respawnPoint
        r.u32(); r.vec2w()
    r.f64()                                   # life
    for _ in range(r.i32()):                  # ability
        r.i32()
    if bot_version > 0:
        r.u32()                               # flockId
    if bot_version > 1 and r.boolean():       # robot
        r.u32(); r.i32(); r.f32(); r.boolean()          # userId, robotType, energy, isActivate
        ctx["where"] = _w + "/robot"
        for _ in range(r.i32()):
            _read_inventory(r, MAP_VERSION, item_ext, ctx)   # Equipment
        ctx["where"] = _w
        r.i32(); r.i32(); r.i32()                        # RobotProgram
        if bot_version > 2:
            r.vec2w()                                    # parking
            r.f64()                                      # timeTimer
            for _ in range(r.i32()):
                r.f32(); r.boolean()                     # timers
    if bot_version > 3:
        u["domesticated"] = r.boolean()
        r.f64()                                          # timeDomestication
    return u


def _read_transport(r, version, item_ext, ctx):
    t = {"energy": r.f32(), "health": r.f32(), "user_id": r.u32()}
    r.f64()                                   # timeFree
    _w = ctx["where"]
    ctx["where"] = _w + "/vehicle"
    t["inventory"] = _read_inventory(r, version, item_ext, ctx)
    ctx["where"] = _w + "/vehicle.equip"
    for _ in range(r.i32()):                  # Equipment
        _read_inventory(r, version, item_ext, ctx)
    ctx["where"] = _w
    t["units"] = []
    if version > 1:
        for _ in range(r.i32()):             # transport (вложенные Block)
            _read_block(r, version, item_ext, ctx)
    if version > 4:
        bot_version = 4 if version > 6 else (3 if version > 5 else 2)
        for _ in range(r.i32()):
            t["units"].append(_read_unit(r, version, bot_version, item_ext, ctx))
    return t


def _read_shop(r, version, item_ext, ctx):
    _w = ctx["where"]
    ctx["where"] = _w + "/shop"
    for _ in range(r.i32()):                  # inventory (List<Item>)
        _read_item(r, version, item_ext, ctx)
    if r.boolean():                           # storage
        _read_inventory(r, version, item_ext, ctx)
    ctx["where"] = _w
    for _ in range(r.i32()):                  # price
        r.i32(); r.i32()
    r.i32()                                   # countUse


def _read_block(r, version, item_ext, ctx):
    blk = {"type": r.i32(), "level": r.i32(), "health": r.f32(), "res": []}
    for _ in range(r.i32()):
        blk["res"].append({"type": r.i32(), "count": r.i32()})
    blk["transport"] = _read_transport(r, version, item_ext, ctx) if r.boolean() else None
    if version > 0 and r.boolean():
        _read_shop(r, version, item_ext, ctx)
    return blk


def _read_cell(r, version, item_ext, ctx):
    c = {"ground": r.i8(), "block": None, "grass": None, "box": None,
         "machine": None, "containers": [], "gas": None}
    if r.boolean():
        ctx["where"] = "block"
        c["block"] = _read_block(r, version, item_ext, ctx)
    if r.boolean():
        ctx["where"] = "grass"
        c["grass"] = _read_block(r, version, item_ext, ctx)
    if r.boolean():
        ctx["where"] = "box"
        c["box"] = _read_block(r, version, item_ext, ctx)
    for slot in ("underground", "ground_inv", "container"):
        if r.boolean():
            ctx["where"] = "container:" + slot
            c["containers"].append((slot, _read_inventory(r, version, item_ext, ctx)))
    if r.boolean():
        ctx["where"] = "machine"
        c["machine"] = _read_machine(r, version, item_ext, ctx)
    ctx["where"] = ""
    if version > 2:
        c["energy"] = r.f32()
    if version > 3 and r.boolean():
        c["gas"] = {"type": r.i32(), "time": r.f64()}
    return c


# --------------------------------------------------------------------- item_ext
_ITEM_EXT_CACHE = {}


def _load_item_ext(world_dir):
    """{item_id: (is_dish, is_mixture)} из Data\\items.json (для Item.ext)."""
    if not world_dir:
        return {}
    p = os.path.join(world_dir, "Data", "items.json")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return {}
    hit = _ITEM_EXT_CACHE.get(p)
    if hit and hit[0] == mt:
        return hit[1]
    out = {}
    try:
        with io.open(p, "r", encoding="utf-8-sig") as f:
            for it in json.load(f).get("items", []):
                e = it.get("ext")
                if isinstance(e, dict) and "id" in it:
                    out[it["id"]] = (bool(e.get("isDish")), bool(e.get("isMixture")))
    except (OSError, ValueError):
        pass
    _ITEM_EXT_CACHE[p] = (mt, out)
    return out


# --------------------------------------------------------------------- публичное
def parse(path, world_dir=None, keep_grid=False, want=None, cap=20000,
          paint=False, block_class=None, claims=True, only_owner=None, owners=False):
    """Полный разбор map<N>.dt. -> dict. Не бросает — при ошибке ``ok=False``.

    ``want`` — множество id предметов для поиска; тогда в ответе есть ``hits`` =
    ``[{x, y, where, type, count, durability}]`` (не более ``cap`` записей).

    ``paint`` — вернуть ``pixels`` (bytearray w*h*3, строки по y) с цветовой
    картой (вода/суша/горы/природа/постройки), ``block_class`` = {type: cat}.
    ``claims`` — тонировать застолблённую землю; ``only_owner`` — выделить одного.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return {"ok": False, "error": "не открыть файл: %s" % e}

    item_ext = _load_item_ext(world_dir)
    want = set(want) if want else None
    ctx = {"want": want, "hits": [], "x": 0, "y": 0, "where": "", "cap": cap}
    bc = block_class or {}
    r = _R(data)
    try:
        version = MAP_VERSION
        # первый int32 всегда версия у mapVersion>6 (в файле лежит 7)
        version = r.i32()
        server_time = r.f64()
        w, h = r.i16(), r.i16()
        if not (0 < w <= 8192 and 0 < h <= 8192):
            return {"ok": False, "error": "неправдоподобный размер %dx%d" % (w, h)}

        blocks = Counter()
        grass = Counter()
        machines = Counter()
        res_in_blocks = Counter()      # ресурсы внутри блоков (руда в жиле и т.п.)
        container_items = Counter()    # предметы в наземных/подземных контейнерах
        water = land = 0
        gas_tiles = infection_tiles = 0
        container_count = 0
        vehicles = 0
        vehicle_units = 0
        grid = [] if keep_grid else None
        px = bytearray(w * h * 3) if paint else None

        for _x in range(w):
            row = [] if keep_grid else None
            for _y in range(h):
                ctx["x"], ctx["y"] = _x, _y
                c = _read_cell(r, version, item_ext, ctx)
                if c["ground"]:
                    land += 1
                else:
                    water += 1
                if paint:
                    col = _COL.get(_classify(c, bc), _COL["unknown"])
                    pi = (_y * w + _x) * 3
                    px[pi] = col[0]; px[pi + 1] = col[1]; px[pi + 2] = col[2]
                if c["block"]:
                    blocks[c["block"]["type"]] += 1
                    for rr in c["block"]["res"]:
                        res_in_blocks[rr["type"]] += rr["count"]
                        if want and rr["type"] in want and len(ctx["hits"]) < cap:
                            ctx["hits"].append({"x": _x, "y": _y, "where": "block.res",
                                                "type": rr["type"], "count": rr["count"],
                                                "durability": 0})
                    if c["block"].get("transport"):
                        vehicles += 1
                        vehicle_units += len(c["block"]["transport"].get("units") or [])
                if c["grass"]:
                    grass[c["grass"]["type"]] += 1
                    if c["grass"]["type"] == 147:
                        infection_tiles += 1
                if c["machine"]:
                    machines[c["machine"]["type"]] += 1
                for _slot, inv in c["containers"]:
                    container_count += 1
                    for it in inv["items"]:
                        container_items[it["type"]] += it["count"]
                if c["gas"]:
                    gas_tiles += 1
                if keep_grid:
                    row.append(c)
            if keep_grid:
                grid.append(row)

        stone = []
        for _ in range(r.i32()):
            x, y = r.i16(), r.i16()
            stone.append({"x": x, "y": y, "type": r.i32()})
        biom = [r.i32() for _ in range(r.i32())]

        owner = Counter()
        um_w, um_h = w // 8, h // 8
        um_flat = [] if (want or paint or owners) else None
        # userMap[x, y] = ReadUInt32(), x внешний цикл (0..w/8), y внутренний (0..h/8)
        for _ in range(um_w * um_h):
            uid = r.u32()
            if um_flat is not None:
                um_flat.append(uid)
            if uid:
                owner[uid] += 1

        # проставить владельца земли (блок 8×8) каждому найденному предмету
        if um_flat is not None and want:
            for hh in ctx["hits"]:
                bx, by = hh["x"] // 8, hh["y"] // 8
                oi = bx * um_h + by
                hh["owner"] = um_flat[oi] if (bx < um_w and by < um_h and 0 <= oi < len(um_flat)) else 0

        # наложить клаймы на картинку
        if paint and um_flat and (claims or only_owner):
            only_owner = int(only_owner) if only_owner else 0
            nflat = len(um_flat)
            npal = len(_CLAIM_PAL)
            # стабильный индекс палитры по id владельца (в порядке появления)
            pal_of = {}
            for _y in range(h):
                byi = _y // 8
                for _x in range(w):
                    oi = (_x // 8) * um_h + byi     # userMap[x,y], x-мажор (Map.cs)
                    o = um_flat[oi] if oi < nflat else 0
                    if not o:
                        continue
                    pi = (_y * w + _x) * 3
                    cur = (px[pi], px[pi + 1], px[pi + 2])
                    if only_owner:
                        nc = _blend(cur, _CLAIM_HL, 0.6) if o == only_owner else _blend(cur, (0, 0, 0), 0.35)
                    else:
                        k = pal_of.get(o)
                        if k is None:
                            k = pal_of[o] = _CLAIM_PAL[len(pal_of) % npal]
                        nc = _blend(cur, k, 0.5)
                    px[pi] = nc[0]; px[pi + 1] = nc[1]; px[pi + 2] = nc[2]

        oxygen_map = None
        remain = r.rest()
        if remain >= w * h * 4:
            oxygen_map = True
            r.p += w * h * 4
        trailing = r.rest()

        stone_types = Counter(s["type"] for s in stone)
        return {
            "ok": True,
            "version": version,
            "server_time": server_time,
            "w": w, "h": h,
            "cells": w * h,
            "ground": {"land": land, "water": water},
            "blocks_total": sum(blocks.values()),
            "blocks_by_type": [{"type": t, "n": n} for t, n in blocks.most_common(40)],
            "grass_total": sum(grass.values()),
            "machines": [{"type": t, "n": n} for t, n in machines.most_common()],
            "machines_total": sum(machines.values()),
            "res_in_blocks": [{"type": t, "n": n} for t, n in res_in_blocks.most_common(30)],
            "containers": container_count,
            "container_items": [{"type": t, "n": n} for t, n in container_items.most_common(30)],
            "stone_points": len(stone),
            "stone_types": [{"type": t, "n": n} for t, n in stone_types.most_common()],
            "biom_count": biom,
            "gas_tiles": gas_tiles,
            "infection_tiles": infection_tiles,
            "vehicles": vehicles,
            "vehicle_units": vehicle_units,
            "land_owners": [{"owner": o, "blocks8": n} for o, n in owner.most_common(50)],
            "land_owned_blocks8": sum(owner.values()),
            "land_total_blocks8": um_w * um_h,
            "oxygen_map": bool(oxygen_map),
            "trailing_bytes": trailing,
            "grid": grid,
            "hits": ctx["hits"] if want else None,
            "hits_capped": bool(want) and len(ctx["hits"]) >= cap,
            "pixels": px,
            "owner_grid": um_flat if (owners or paint) else None,
            "um_w": um_w, "um_h": um_h,
        }
    except (EOFError, struct.error) as e:
        return {"ok": False, "error": "разбор оборвался: %s" % e, "at_byte": r.p,
                "file_size": len(data)}
    except Exception as e:  # noqa: BLE001
        logging.exception("mapdt.parse %s", path)
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "at_byte": r.p}


def render_png(path, world_dir=None, block_class=None, scale=None,
               claims=True, only_owner=None):
    """Картинка карты (PNG bytes) + мета. ``block_class`` = {block_type: cat}
    (`mtn|ore|wall|floor|built|plant`). ``scale`` = None -> авто (крупная сторона
    ~640, макс x8). -> ``{ok, w, h, scale, png, legend, owners}``."""
    d = parse(path, world_dir=world_dir, keep_grid=False, paint=True,
              block_class=block_class, claims=claims, only_owner=only_owner)
    if not d.get("ok"):
        return d
    w, h = d["w"], d["h"]
    if scale is None:
        scale = max(1, min(8, 640 // max(w, h) or 1))
    scale = max(1, min(16, int(scale)))
    png = _png_bytes(w, h, bytes(d["pixels"]), scale)
    return {
        "ok": True, "w": w, "h": h, "scale": scale, "png": png,
        "legend": [{"cat": k, "rgb": list(v)} for k, v in _COL.items()
                   if k != "unknown"],
        "owners": d.get("land_owners") or [],
        "land_total_blocks8": d.get("land_total_blocks8"),
        "owner_grid": d.get("owner_grid"), "um_w": d.get("um_w"), "um_h": d.get("um_h"),
    }


def summary(path, world_dir=None, item_names=None):
    """Как ``parse`` без ``grid``; ``type`` заменяются именами (если дан
    ``item_names`` = {id: name}, напр. из ``players.load_items``)."""
    d = parse(path, world_dir=world_dir, keep_grid=False)
    if not d.get("ok") or not item_names:
        return d
    for key in ("blocks_by_type", "res_in_blocks", "container_items",
                "machines", "stone_types"):
        for row in d.get(key, []):
            row["name"] = item_names.get(row["type"]) or ("#%s" % row["type"])
    return d


def find_item(path, want, world_dir=None, item_names=None, cap=20000, user_names=None):
    """Найти все предметы с id из ``want`` на карте ``path``.

    -> ``{ok, w, h, want, total_count, spots, by_where[{where,spots,count}],
    by_owner[{owner,owner_name,spots,count}],
    hits[{x,y,where,type,name,count,durability,owner,owner_name}], capped}``.
    ``owner`` = id владельца земли (блок 8×8) в точке предмета, 0 = ничья.
    """
    want = set(want) if not isinstance(want, set) else want
    user_names = user_names or {}
    d = parse(path, world_dir=world_dir, keep_grid=False, want=want, cap=cap)
    if not d.get("ok"):
        return d
    hits = d.get("hits") or []
    by_where = Counter()
    cnt_where = Counter()
    by_owner = Counter()
    cnt_owner = Counter()
    total = 0
    for hh in hits:
        total += hh["count"]
        by_where[hh["where"]] += hh["count"]
        cnt_where[hh["where"]] += 1
        if item_names:
            hh["name"] = item_names.get(hh["type"]) or ("#%s" % hh["type"])
        o = hh.get("owner") or 0
        hh["owner"] = o
        hh["owner_name"] = (user_names.get(o) or ("id %s" % o)) if o else ""
        by_owner[o] += hh["count"]
        cnt_owner[o] += 1
    return {
        "ok": True,
        "w": d["w"], "h": d["h"],
        "want": sorted(want),
        "total_count": total,
        "spots": len(hits),
        "by_where": [{"where": k, "spots": cnt_where[k], "count": v}
                     for k, v in by_where.most_common()],
        "by_owner": [{"owner": o, "owner_name": (user_names.get(o) or ("id %s" % o)) if o else "",
                      "spots": cnt_owner[o], "count": v}
                     for o, v in by_owner.most_common()],
        "hits": hits,
        "capped": d.get("hits_capped", False),
    }
