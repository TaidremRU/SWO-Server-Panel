# -*- coding: utf-8 -*-
"""Собрать иконки предметов игры в один атлас для панели игроков.

Картинки берутся из распакованного проекта игры (``Assets/Art/Items/*.png|*.tga``,
имя файла = поле ``sprite`` из ``Data/items.json``). Сама графика — собственность
игры, в репозиторий она не кладётся: атлас собирается локально и копируется в
``base_dir`` панели. Нет атласа — панель просто работает без иконок.

    python build_item_icons.py <Assets/Art/Items> <items.json> <каталог_вывода>

-> ``item_icons.png`` (сетка CELL×CELL, предмет по центру без пустых полей) + ``item_icons.json``
{cell, cols, idx: {slug предмета: номер клетки}}.
"""
import json
import math
import os
import sys

from PIL import Image

CELL = 64
PAD = 4      # поля внутри клетки


def main(src, items_path, out_dir):
    items = json.load(open(items_path, encoding="utf-8-sig")).get("items") or []
    files = {}
    for f in os.listdir(src):
        base, ext = os.path.splitext(f)
        if ext.lower() in (".png", ".tga"):
            files.setdefault(base, os.path.join(src, f))
    sprites = sorted({it.get("sprite") for it in items if it.get("sprite") in files})
    cols = 32
    rows = math.ceil(len(sprites) / cols)
    sheet = Image.new("RGBA", (cols * CELL, rows * CELL), (0, 0, 0, 0))
    cell_of = {}
    for i, sp in enumerate(sprites):
        im = Image.open(files[sp]).convert("RGBA")
        # в игре предмет занимает ~18 px в центре картинки 32×32 — срезать прозрачные
        # поля и увеличить в целое число раз (пиксели остаются чёткими), чтобы все
        # иконки были одного видимого размера
        bb = im.getbbox()
        if bb:
            im = im.crop(bb)
        room = CELL - 2 * PAD
        side = max(im.size)
        if side <= room:
            k = max(1, room // side)
            im = im.resize((im.width * k, im.height * k), Image.NEAREST)
        else:
            f = room / float(side)
            im = im.resize((max(1, round(im.width * f)), max(1, round(im.height * f))), Image.LANCZOS)
        x0, y0 = (i % cols) * CELL, (i // cols) * CELL
        sheet.paste(im, (x0 + (CELL - im.width) // 2, y0 + (CELL - im.height) // 2), im)
        cell_of[sp] = i
    idx = {it["name"]: cell_of[it["sprite"]] for it in items if it.get("name") and it.get("sprite") in cell_of}
    os.makedirs(out_dir, exist_ok=True)
    sheet.save(os.path.join(out_dir, "item_icons.png"), optimize=True)
    with open(os.path.join(out_dir, "item_icons.json"), "w", encoding="utf-8") as f:
        json.dump({"cell": CELL, "cols": cols, "idx": idx}, f, separators=(",", ":"))
    missing = [it.get("name") for it in items if it.get("name") not in idx]
    print("иконок: %d, предметов с иконкой: %d/%d%s" % (len(sprites), len(idx), len(items),
                                                         ("; без иконки: " + ", ".join(missing[:20])) if missing else ""))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
