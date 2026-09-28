# -*- coding: utf-8 -*-
"""Ассортиментная ведомость из дендроплана.

Дендроплан раскладывает насаждения по слоям, где порода зашита в имя:

    05_ДП_ЛИПА_Сущ.            существующие липы
    06_ДП_БересклетЕВР_план    проектируемые бересклеты

Каждый круг или контур — одно растение. Модуль собирает из этого ведомость:
порода, существующие, проектируемые, средний диаметр условного знака.

    python src/dendro.py "data/ДЕНДРОПлан.dxf" -o out/vedomost.csv
"""
import argparse
import csv
import math
import os
import re
import sys
from collections import defaultdict

import ezdxf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Слои дендроплана: 05_ДП_<порода>_Сущ. и 06_ДП_<порода>_план
LAYER_RE = re.compile(r"^0?(\d)\s*_*ДП_+(.+?)_+(Сущ\.?|план)\s*$", re.I)

# Расшифровка сокращений в именах слоёв
EXPAND = [
    (r"\bЛИПА\s*Мелк\b", "Липа мелколистная"),
    (r"\bЛИПА\b", "Липа"),
    (r"\bКЛЕН\s*Гин?нала\b", "Клён Гиннала"),
    (r"\bКЛЕН\s*РОЯЛРЕД\b", "Клён 'Royal Red'"),
    (r"\bКЛЕН\b", "Клён"),
    (r"\bБересклетЕВР\b", "Бересклет европейский"),
    (r"\bПузыреплодЛЮТЕУС\b", "Пузыреплодник 'Luteus'"),
    (r"\bПузыр\.?\s*РедБарон\b", "Пузыреплодник 'Red Baron'"),
    (r"\bПузыр\.?\s*ЭМБЕР\s*Джубили\b", "Пузыреплодник 'Amber Jubilee'"),
    (r"\bСпирВАНГУТА\b", "Спирея Вангутта"),
    (r"\bСпирГОЛДФРЕЙМ\b", "Спирея 'Goldflame'"),
    (r"\bСпирБЕРЕЗ\b", "Спирея берёзолистная"),
    (r"\bСпирМАКРОФИЛА\b", "Спирея 'Macrophylla'"),
    (r"\bДеренЭЛЕГАНТ\b", "Дерен 'Elegantissima'"),
    (r"\bГорт\.?\s*МЕТЕЛЬЧ\b", "Гортензия метельчатая"),
    (r"Горт\.?\s*_?\s*ПинкАнабель", "Гортензия 'Pink Annabelle'"),
    (r"Бузина\s*_?\s*Черн", "Бузина чёрная"),
    (r"Горт\.?\s*АН\s*_?\s*БЕЛАЯ", "Гортензия 'Annabelle'"),
    (r"\bГорт\.?\s*АН[_\s]*БЕЛАЯ\b", "Гортензия 'Annabelle'"),
    (r"\bЧЕРЕМ\s*Колората\b", "Черёмуха 'Colorata'"),
    (r"\bЯБЛОНЯ\s*Дек\b", "Яблоня декоративная"),
    (r"\bСОСНА\s*ГОРН\b", "Сосна горная"),
    (r"\bСОСНА\s*ОБЫКН\b", "Сосна обыкновенная"),
    (r"\bСОСНА\s*Кедр\b", "Сосна кедровая"),
    (r"\bЕЛЬ\s*КОЛЮЧ\b", "Ель колючая"),
    (r"\bЕЛЬ\s*ОБЫКН\b", "Ель обыкновенная"),
    (r"\bСиреньВЕНГ\.?\b", "Сирень венгерская"),
    (r"\bСирень\s*обыкн\b", "Сирень обыкновенная"),
    (r"\bРябинникРябин\b", "Рябинник рябинолистный"),
    (r"\bВейникКоротковолос\b", "Вейник коротковолосистый"),
    (r"\bТОПОЛЬ\s*Симана\b", "Тополь Симона"),
    (r"\bПСЕВДОТЦУГА\b", "Псевдотсуга Мензиса"),
    (r"\bАКАЦИЯ\s*ЖЕЛТ\.?\b", "Акация жёлтая (карагана)"),
]


SKIP_RE = re.compile(r"выноск|размер|подпис|текст|сетка|рамк|штрихов", re.I)


def pretty(name):
    s = name.replace("_", " ").strip()
    for pat, repl in EXPAND:
        if re.search(pat, s, re.I):
            return repl
    return s.capitalize()


def radius(e):
    """Радиус условного знака, м."""
    t = e.dxftype()
    if t == "CIRCLE":
        return float(e.dxf.radius)
    try:
        pts = [(p[0], p[1]) for p in e.get_points("xy")] \
            if t == "LWPOLYLINE" else None
        if pts and len(pts) >= 3:
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            return sum(math.hypot(p[0] - cx, p[1] - cy) for p in pts) / len(pts)
    except Exception:
        pass
    return 0.0


def main():
    ap = argparse.ArgumentParser(description="Ведомость из дендроплана")
    ap.add_argument("dxf")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    doc = ezdxf.readfile(args.dxf)
    msp = doc.modelspace()

    # Дерево рисуется кругом: один круг — одно растение.
    # Кустарники рисуются контуром массива: один контур — группа растений.
    # Смешивать нельзя, поэтому считаем раздельно.
    data = defaultdict(lambda: {"Сущ_шт": 0, "план_шт": 0,
                                "Сущ_гр": 0, "план_гр": 0, "r": []})
    for e in msp:
        lay = e.dxf.layer.strip()
        m = LAYER_RE.match(lay)
        if not m or SKIP_RE.search(lay):
            continue
        t = e.dxftype()
        if t not in ("CIRCLE", "LWPOLYLINE", "POLYLINE", "ELLIPSE", "INSERT"):
            continue
        species = pretty(m.group(2))
        kind = "Сущ" if m.group(3).lower().startswith("сущ") else "план"
        single = t in ("CIRCLE", "INSERT")
        data[species][f"{kind}_{'шт' if single else 'гр'}"] += 1
        if single:
            r = radius(e)
            if r > 0:
                data[species]["r"].append(r)

    if not data:
        print("Слои дендроплана вида 05_ДП_..._Сущ / 06_ДП_..._план не найдены.")
        return

    rows = []
    for sp, d in sorted(data.items(),
                        key=lambda kv: -(kv[1]["Сущ_шт"] + kv[1]["план_шт"]
                                         + kv[1]["Сущ_гр"] + kv[1]["план_гр"])):
        avg = round(2 * sum(d["r"]) / len(d["r"]), 1) if d["r"] else ""
        rows.append((sp, d["Сущ_шт"], d["план_шт"],
                     d["Сущ_гр"], d["план_гр"], avg))

    hdr = ("ПОРОДА", "СУЩ, шт", "ПРОЕКТ, шт", "СУЩ, гр", "ПРОЕКТ, гр", "Ø КРОНЫ")
    print("%-34s %9s %11s %9s %11s %9s" % hdr)
    print("-" * 88)
    for sp, es, ps, eg, pg, avg in rows:
        print("%-34.34s %9s %11s %9s %11s %9s" % (
            sp, es or "", ps or "", eg or "", pg or "", avg))
    print("-" * 88)
    print("%-34s %9d %11d %9d %11d" % (
        "ИТОГО", sum(r[1] for r in rows), sum(r[2] for r in rows),
        sum(r[3] for r in rows), sum(r[4] for r in rows)))
    print("\nшт — одиночные посадки (круг = растение); "
          "гр — контуры массивов (один контур = группа)")

    out = args.out or (os.path.splitext(args.dxf)[0] + "_vedomost.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Порода", "Существующие одиночные, шт",
                    "Проектируемые одиночные, шт", "Существующие массивы, контуров",
                    "Проектируемые массивы, контуров", "Диаметр кроны, м"])
        w.writerows(rows)
    print(f"\nВедомость сохранена: {out}")


if __name__ == "__main__":
    main()
