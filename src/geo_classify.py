# -*- coding: utf-8 -*-
"""Распознавание слоёв по форме объектов, когда имя ничего не говорит.

Имя слоя «Слой_17» или «ГП_изм_3» не подскажет ни правилам, ни языковой
модели. Но то, что на нём нарисовано, узнаётся по форме: колодец и люк —
это маленькие окружности, существующее дерево — окружность кроны в
несколько метров, опора освещения — совсем мелкий кружок. Такие знаки
стандартны на топопланах, и форма у них устойчивее, чем имена слоёв.

Классификатор осторожный: срабатывает, только когда подавляющее
большинство объектов слоя — однотипные знаки одного размера. Смешанные
слои и линии не трогает: лучше оставить слой нераспознанным, чем
присвоить неверный класс.
"""
import numpy as np

# (класс, радиус от, радиус до) — диапазоны типовых условных знаков
SYMBOLS = [
    ("lighting_pole", 0.10, 0.30),     # опора освещения, столбик
    ("well", 0.30, 0.90),              # колодец, люк, дождеприёмник
    ("existing_tree", 1.20, 6.50),     # проекция кроны дерева
]
MIN_OBJECTS = 3        # по одному-двум объектам класс не выводим
MIN_SHARE = 0.8        # такая доля объектов слоя должна быть знаками


def _ring_stats(g):
    """Окружность ли это и какого радиуса. None, если не похоже на знак."""
    try:
        pts = np.asarray(g.coords, dtype=float)
    except Exception:
        return None
    if len(pts) < 8:
        return None
    closed = np.hypot(*(pts[0] - pts[-1])) < 1e-3 * max(np.ptp(pts[:, 0]), 1e-6) + 1e-6
    if not closed:
        return None
    c = pts.mean(axis=0)
    d = np.hypot(pts[:, 0] - c[0], pts[:, 1] - c[1])
    r = float(d.mean())
    if r <= 0:
        return None
    # окружность: расстояние от центра почти одинаковое
    if float(d.std()) / r > 0.15:
        return None
    return r


def classify_layer_geometry(geoms):
    """Класс по форме объектов слоя или None."""
    if len(geoms) < MIN_OBJECTS:
        return None, 0.0
    radii = [r for r in (_ring_stats(g) for g in geoms) if r is not None]
    if len(radii) < MIN_OBJECTS or len(radii) / len(geoms) < MIN_SHARE:
        return None, 0.0
    med = float(np.median(radii))
    spread = float(np.std(radii) / med) if med > 0 else 1.0
    for cls, lo, hi in SYMBOLS:
        if lo <= med <= hi:
            inside = sum(1 for r in radii if lo <= r <= hi) / len(radii)
            if inside >= MIN_SHARE:
                conf = min(1.0, inside * (1.0 - min(spread, 0.5)))
                return cls, round(conf, 2)
    return None, 0.0


def classify_unknown(by_layer, layer_class):
    """Для слоёв без класса — класс по форме. Возвращает {слой: класс}."""
    out = {}
    for layer, cls in layer_class.items():
        if cls != "unknown":
            continue
        got, conf = classify_layer_geometry(by_layer.get(layer, []))
        if got and conf >= 0.6:
            out[layer] = got
    return out
