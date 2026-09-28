# -*- coding: utf-8 -*-
"""Предпросмотр чертежа до расчёта: как распознаны слои.

Раньше увидеть, что проезжая часть прочитана как газон или граница работ
взята из чертежа профиля, можно было только после расчёта. Здесь чертёж
рисуется сразу после загрузки, раскрашенный по классам слоёв.

Разбор DXF кешируется в cache/: чертёж на 200 МБ читается минуту, а
страницы слоёв и разметки открывают его снова и снова.
"""
import hashlib
import os
import pickle

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "cache")
CACHE_VERSION = 2          # меняется, когда меняется разбор в dxf_io


def load(path, convert_dir=None):
    """(by_layer, fills) — из кеша, если файл не менялся, иначе разбор.

    convert_dir — куда класть DXF после конвертации DWG (по умолчанию рядом)."""
    st = os.stat(path)
    key = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}|"
                       f"{CACHE_VERSION}".encode("utf-8")).hexdigest()
    cp = os.path.join(CACHE, key + ".pkl")
    try:
        with open(cp, "rb") as f:
            return pickle.load(f)
    except Exception:
        pass
    from dxf_io import read_dxf
    _, by_layer, fills = read_dxf(path, convert_dir=convert_dir)
    os.makedirs(CACHE, exist_ok=True)
    try:
        with open(cp, "wb") as f:
            pickle.dump((by_layer, fills), f, protocol=5)
    except Exception:
        pass
    return by_layer, fills


def layer_lengths(by_layer):
    """Длина линий каждого слоя, м: вес слоя честнее числа объектов."""
    import shapely
    out = {}
    for lay, geoms in by_layer.items():
        try:
            out[lay] = float(shapely.length(np.asarray(geoms, dtype=object)).sum())
        except Exception:
            out[lay] = 0.0
    return out


def classify(by_layer, overrides):
    """Классы слоёв так же, как в расчёте, без модели и LLM."""
    from layers import classify_with_source
    import context_classify as CC
    cls, src = classify_with_source(by_layer.keys(), overrides)
    for k, v in CC.type_codes(cls, src).items():
        cls[k], src[k] = v, "types"
    return cls, src


def _extent(by_layer, cls, bins=120):
    """Габарит основной части чертежа.

    Вес объекта — длина его линий: легенда и штамп состоят из множества
    мелких знаков и по числу объектов перетягивали схему, а по длине они
    ничто. Геометрия раскладывается по сетке, соседние занятые клетки
    склеиваются, и берётся связная часть с наибольшей длиной линий: план,
    а не листы профилей и копии в стороне.
    """
    from scipy import ndimage
    pts, wts = [], []
    for lay, geoms in by_layer.items():
        if cls.get(lay) in ("unknown", "annotation", "ignore"):
            continue
        for g in geoms:
            b = g.bounds
            pts.append(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
            wts.append(max(g.length, 0.1))
    if len(pts) < 10:
        for geoms in by_layer.values():
            for g in geoms[:2000]:
                b = g.bounds
                pts.append(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
                wts.append(max(g.length, 0.1))
    p, w = np.asarray(pts), np.asarray(wts)
    lo, hi = np.percentile(p, 0.5, axis=0), np.percentile(p, 99.5, axis=0)
    step = max((hi - lo).max() / bins, 1.0)
    ij = np.floor((p - lo) / step).astype(int)
    ok = (ij >= 0).all(1) & (ij[:, 0] <= bins) & (ij[:, 1] <= bins)
    grid = np.zeros((bins + 1, bins + 1))
    np.add.at(grid, (ij[ok, 0], ij[ok, 1]), w[ok])
    lab, n = ndimage.label(ndimage.binary_dilation(grid > 0, iterations=2))
    if n:
        mass = ndimage.sum(grid, lab, index=np.arange(1, n + 1))
        main = lab == 1 + int(np.argmax(mass))
        sel = ok.copy()
        sel[ok] = main[ij[ok, 0], ij[ok, 1]]
        if sel.sum() >= 10:
            p = p[sel]
    x0, y0 = p.min(0)
    x1, y1 = p.max(0)
    pad = 0.03 * max(x1 - x0, y1 - y0, 1.0)
    return x0 - pad, y0 - pad, x1 + pad, y1 + pad


def svg(by_layer, cls, style, max_points=350_000):
    """Схема: по группе <g> на класс, координаты упрощены под масштаб.

    Возвращает (svg-разметка, сводка по классам). Нераспознанные слои —
    красным поверх всего: их и нужно видеть в первую очередь.
    """
    import shapely
    x0, y0, x1, y1 = _extent(by_layer, cls)
    span = max(x1 - x0, y1 - y0)
    tol = span / 2500.0                       # меньше пикселя на экране
    order = [c for c in style] + ["unknown"]
    groups = {c: [] for c in order}
    stats = {}
    budget = max_points
    for lay, geoms in by_layer.items():
        c = cls.get(lay, "unknown")
        if c in ("annotation", "ignore", "reference_planting", "removed_tree"):
            continue
        c = c if c in groups else "unknown"
        s = stats.setdefault(c, {"layers": 0, "len": 0.0})
        s["layers"] += 1
        for g in geoms:
            b = g.bounds
            if b[2] < x0 or b[0] > x1 or b[3] < y0 or b[1] > y1:
                continue
            s["len"] += g.length
            if budget <= 0:
                continue
            gs = shapely.simplify(g, tol) if tol > 0 else g
            for part in getattr(gs, "geoms", [gs]):
                rings = ([part.exterior, *part.interiors]
                         if part.geom_type == "Polygon" else [part])
                for r in rings:
                    xy = np.asarray(r.coords)
                    if len(xy) < 2:
                        if len(xy) == 1:          # точка — крошечный крестик
                            x, y = xy[0]
                            xy = np.array([[x - tol, y], [x + tol, y]])
                        else:
                            continue
                    budget -= len(xy)
                    groups[c].append("M" + " ".join(
                        f"{x - x0:.2f},{y1 - y:.2f}" for x, y in xy))
    w, h = x1 - x0, y1 - y0
    body = []
    for c in order:
        if not groups[c]:
            continue
        color = "#e53935" if c == "unknown" else style[c][1]
        lw = 2.2 if c == "unknown" else style[c][2]
        body.append(f'<g id="pv_{c}" data-cls="{c}"><path d="{" ".join(groups[c])}" '
                    f'fill="none" stroke="{color}" stroke-width="{lw}" '
                    f'vector-effect="non-scaling-stroke" '
                    f'stroke-linecap="round"/></g>')
    out = (f'<svg id="pv" viewBox="0 0 {w:.1f} {h:.1f}" '
           f'preserveAspectRatio="xMidYMid meet">{"".join(body)}</svg>')
    return out, stats, budget <= 0
