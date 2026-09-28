# -*- coding: utf-8 -*-
"""Разбор слоёв чертежа: что внутри и как это понял классификатор.

    python src/inspect_dxf.py data/Посадочный_план.dxf
"""
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ezdxf                                    # noqa: E402
from dxf_io import _open                        # noqa: E402
from layers import classify_layer, normalize    # noqa: E402


def main(path):
    doc = _open(path)
    msp = doc.modelspace()

    counts, types = Counter(), {}
    texts = {}          # слой -> примеры подписей
    blocks_in = Counter()
    for e in msp:
        lay = e.dxf.layer
        counts[lay] += 1
        types.setdefault(lay, Counter())[e.dxftype()] += 1
        if e.dxftype() in ("TEXT", "MTEXT", "ATTRIB"):
            try:
                t = e.plain_text() if hasattr(e, "plain_text") else e.dxf.text
            except Exception:
                t = ""
            t = (t or "").strip()
            if t:
                texts.setdefault(lay, []).append(t)
        if e.dxftype() == "INSERT":
            blocks_in[e.dxf.name] += 1

    declared = [l.dxf.name for l in doc.layers]
    print(f"Файл: {path}")
    print(f"Версия DXF: {doc.dxfversion}")
    print(f"Слоёв объявлено: {len(declared)}, со сдержимым: {len(counts)}")
    print(f"Объектов в модели: {sum(counts.values())}")

    xrefs = [b.name for b in doc.blocks if b.name.startswith("*") is False
             and getattr(b.block, "dxf", None) is not None
             and getattr(b.block.dxf, "xref_path", "")]
    if xrefs:
        print(f"\nВНИМАНИЕ: внешние ссылки не связаны ({len(xrefs)} шт): "
              + ", ".join(xrefs[:10]))
        print("Переконвертируйте DWG с включённой опцией Bind xrefs.")

    by_class = Counter()
    rows = []
    for lay, n in counts.most_common():
        cls = classify_layer(lay)
        by_class[cls] += n
        top = ", ".join(f"{t}:{c}" for t, c in types[lay].most_common(3))
        rows.append((lay, cls, n, top))

    print("\n%-46s %-20s %7s  %s" % ("СЛОЙ", "КЛАСС", "ОБЪЕКТОВ", "ТИПЫ"))
    print("-" * 110)
    for lay, cls, n, top in rows:
        mark = "  <-- ?" if cls == "unknown" else ""
        print("%-46.46s %-20s %7d  %s%s" % (lay, cls, n, top, mark))

    # Подписи сетей: диаметры, число кабелей, глубина заложения.
    # Если сети лежат одним слоем, тип определяется по ближайшей подписи.
    net_re = re.compile(r"(d\s*=|\d+\s*[кx×]\s*\d|\bгл\.|в\.тр|лот|н\.тр|"
                        r"\d+\s*тр\b|не\s*прослуш)", re.I)
    hits = [(lay, t) for lay, lst in texts.items() for t in lst if net_re.search(t)]
    if hits:
        print(f"\nПОДПИСИ СЕТЕЙ: найдено {len(hits)} на "
              f"{len({l for l, _ in hits})} слоях")
        seen = Counter(l for l, _ in hits)
        for lay, n in seen.most_common(10):
            sample = [t for l, t in hits if l == lay][:6]
            print(f"  {n:>6}  {lay}")
            print(f"          примеры: {' | '.join(sample)}")

    if blocks_in:
        print(f"\nВСТАВЛЕННЫЕ БЛОКИ: {len(blocks_in)} разных, "
              f"{sum(blocks_in.values())} вставок")
        for name, n in blocks_in.most_common(10):
            print(f"  {n:>6}  {name}")

    print("\nИТОГО ПО КЛАССАМ:")
    for cls, n in by_class.most_common():
        print(f"  {cls:<22} {n:>8} объектов")

    # Полный список ОБЪЯВЛЕННЫХ слоёв, включая пустые: в них имена слоёв
    # подосновы, пришедшие через внешние ссылки.
    dump = os.path.splitext(path)[0] + "_layers.txt"
    with open(dump, "w", encoding="utf-8") as f:
        f.write(f"# {path}\n# объявлено слоёв: {len(declared)}\n")
        f.write("# формат: <объектов>\t<класс>\t<имя слоя>\n\n")
        for name in sorted(declared):
            f.write(f"{counts.get(name, 0)}\t{classify_layer(name)}\t{name}\n")
    print(f"\nПолный список слоёв выгружен: {dump}")
    empty = [n for n in declared if counts.get(n, 0) == 0]
    print(f"Объявлено, но пусто: {len(empty)} слоёв "
          f"(вероятно, подоснова из несвязанных внешних ссылок)")

    unk = [(l, n) for l, c, n, _ in rows if c == "unknown"]
    if unk:
        print(f"\nНЕРАСПОЗНАНО слоёв: {len(unk)}, "
              f"объектов: {sum(n for _, n in unk)}")
        print("Крупнейшие (их стоит разобрать в первую очередь):")
        for lay, n in sorted(unk, key=lambda t: -t[1])[:25]:
            print(f"  {n:>7}  {lay}   ->  нормализовано: {normalize(lay)!r}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Укажите путь к DXF: python src/inspect_dxf.py data/файл.dxf")
        sys.exit(1)
    main(sys.argv[1])
