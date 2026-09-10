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


# ------------------------------------------------------------ вложенные структуры
def _read_item(r, version, item_ext):
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
    return it


def _read_inventory(r, version, item_ext):
    n = r.i32()
    items = [_read_item(r, version, item_ext) for _ in range(n)]
    size = r.i32()
    is_limit = r.boolean()
    is_limit_stack = r.boolean()
    return {"items": items, "size": size, "is_limit": is_limit,
            "is_limit_stack": is_limit_stack}


def _read_machine(r, version, item_ext):
    m = {"type": r.i32()}
    m["material"] = _read_item(r, version, item_ext) if r.boolean() else None
    m["product"] = _read_item(r, version, item_ext) if r.boolean() else None
    m["fuel"] = _read_item(r, version, item_ext) if r.boolean() else None
    m["energy"] = r.f32()
    return m


def _read_unit(r, version, bot_version, item_ext):
    u = {"id": r.u64(), "user_id": r.u32(), "species": r.u32(), "gender": r.i32()}
    r.f64()                                   # timeGrowing
    u["map"] = r.u32()
    u["pos"] = r.vec2w()
    u["inventory"] = _read_inventory(r, version, item_ext)
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
        _read_block(r, version, item_ext)
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
        for _ in range(r.i32()):
            _read_inventory(r, MAP_VERSION, item_ext)    # Equipment
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


def _read_transport(r, version, item_ext):
    t = {"energy": r.f32(), "health": r.f32(), "user_id": r.u32()}
    r.f64()                                   # timeFree
    t["inventory"] = _read_inventory(r, version, item_ext)
    for _ in range(r.i32()):                  # Equipment
        _read_inventory(r, version, item_ext)
    t["units"] = []
    if version > 1:
        for _ in range(r.i32()):             # transport (вложенные Block)
            _read_block(r, version, item_ext)
    if version > 4:
        bot_version = 4 if version > 6 else (3 if version > 5 else 2)
        for _ in range(r.i32()):
            t["units"].append(_read_unit(r, version, bot_version, item_ext))
    return t


def _read_shop(r, version, item_ext):
    for _ in range(r.i32()):                  # inventory (List<Item>)
        _read_item(r, version, item_ext)
    if r.boolean():                           # storage
        _read_inventory(r, version, item_ext)
    for _ in range(r.i32()):                  # price
        r.i32(); r.i32()
    r.i32()                                   # countUse


def _read_block(r, version, item_ext):
    blk = {"type": r.i32(), "level": r.i32(), "health": r.f32(), "res": []}
    for _ in range(r.i32()):
        blk["res"].append({"type": r.i32(), "count": r.i32()})
    blk["transport"] = _read_transport(r, version, item_ext) if r.boolean() else None
    if version > 0 and r.boolean():
        _read_shop(r, version, item_ext)
    return blk


def _read_cell(r, version, item_ext):
    c = {"ground": r.i8(), "block": None, "grass": None, "box": None,
         "machine": None, "containers": [], "gas": None}
    if r.boolean():
        c["block"] = _read_block(r, version, item_ext)
    if r.boolean():
        c["grass"] = _read_block(r, version, item_ext)
    if r.boolean():
        c["box"] = _read_block(r, version, item_ext)
    for slot in ("underground", "ground_inv", "container"):
        if r.boolean():
            c["containers"].append((slot, _read_inventory(r, version, item_ext)))
    if r.boolean():
        c["machine"] = _read_machine(r, version, item_ext)
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
def parse(path, world_dir=None, keep_grid=False):
    """Полный разбор map<N>.dt. -> dict. Не бросает — при ошибке ``ok=False``."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return {"ok": False, "error": "не открыть файл: %s" % e}

    item_ext = _load_item_ext(world_dir)
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

        for _x in range(w):
            row = [] if keep_grid else None
            for _y in range(h):
                c = _read_cell(r, version, item_ext)
                if c["ground"]:
                    land += 1
                else:
                    water += 1
                if c["block"]:
                    blocks[c["block"]["type"]] += 1
                    for rr in c["block"]["res"]:
                        res_in_blocks[rr["type"]] += rr["count"]
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
        for _ in range(um_w * um_h):
            uid = r.u32()
            if uid:
                owner[uid] += 1

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
        }
    except (EOFError, struct.error) as e:
        return {"ok": False, "error": "разбор оборвался: %s" % e, "at_byte": r.p,
                "file_size": len(data)}
    except Exception as e:  # noqa: BLE001
        logging.exception("mapdt.parse %s", path)
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "at_byte": r.p}


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
