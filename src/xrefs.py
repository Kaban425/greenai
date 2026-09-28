# -*- coding: utf-8 -*-
"""Показывает все внешние ссылки (xref) чертежа и пути к их файлам.

    python src/xrefs.py "data/Посадочный план.dxf"

Внешняя ссылка — это отдельный DWG-файл, подключённый к чертежу.
Геоподоснова Мосгоргеотреста почти всегда подключается именно так.
"""
import os
import sys
from collections import Counter

import ezdxf


def main(path):
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()

    # сколько раз каждый блок вставлен в модель
    used = Counter(e.dxf.name for e in msp if e.dxftype() == "INSERT")

    rows = []
    for block in doc.blocks:
        blk = getattr(block, "block", None)
        if blk is None:
            continue
        xp = getattr(blk.dxf, "xref_path", "") or ""
        if xp:
            rows.append((block.name, xp, used.get(block.name, 0)))

    if not rows:
        print("Внешних ссылок не найдено — вся геометрия внутри файла.")
        return

    print(f"Файл: {path}")
    print(f"Внешних ссылок: {len(rows)}\n")

    dirs = Counter()
    print("%-46s %6s  %s" % ("ИМЯ ССЫЛКИ", "ВСТАВОК", "ПУТЬ К ФАЙЛУ"))
    print("-" * 120)
    for name, xp, n in sorted(rows, key=lambda r: -r[2]):
        print("%-46.46s %6d  %s" % (name, n, xp))
        dirs[os.path.dirname(xp.replace("\\", "/"))] += 1

    print("\nПАПКИ, В КОТОРЫХ ЛЕЖАТ ФАЙЛЫ ПОДОСНОВЫ:")
    for d, n in dirs.most_common():
        print(f"  {n:>3} файл(ов)  {d or '(та же папка, что и чертёж)'}")

    print("\nИМЕНА ФАЙЛОВ ДЛЯ ПОИСКА В НАБОРЕ ОРГАНИЗАТОРОВ:")
    names = sorted({os.path.basename(xp.replace("\\", "/")) for _, xp, _ in rows})
    for n in names:
        print("  ", n)

    out = os.path.splitext(path)[0] + "_xrefs.txt"
    with open(out, "w", encoding="utf-8") as f:
        for name, xp, n in sorted(rows):
            f.write(f"{n}\t{name}\t{xp}\n")
    print(f"\nСписок сохранён: {out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Укажите путь: python src/xrefs.py "data/файл.dxf"')
        sys.exit(1)
    main(sys.argv[1])
