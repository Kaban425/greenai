# -*- coding: utf-8 -*-
"""Генерация плана посадок."""
import math
import random
import time

import numpy as np
from shapely.geometry import Point
from shapely.prepared import prep

# Веса критериев по стратегии размещения.
# street — рядовая посадка вдоль улицы (защита от пыли и шума).
# grove  — равномерное заполнение всей допустимой зоны (сквер, массив).
# mixed  — компромисс.
WEIGHTS = {
    "street": {"clearance": 0.30, "walk": 0.20, "road": 0.40, "exist": 0.10},
    "grove":  {"clearance": 0.60, "walk": 0.10, "road": 0.00, "exist": 0.30},
    "mixed":  {"clearance": 0.45, "walk": 0.25, "road": 0.15, "exist": 0.15},
}

MAX_CANDIDATES = 60000


def _candidate_grid(zone, step, jitter=0.35, seed=42, verbose=True):
    """Точки-кандидаты внутри допустимой зоны, в шахматном порядке
    со случайным смещением, чтобы посадки не выглядели машинной решёткой."""
    rnd = random.Random(seed)
    minx, miny, maxx, maxy = zone.bounds
    # защита от чертежей во весь лист: не даём сетке разрастись
    est = ((maxx - minx) / step) * ((maxy - miny) / (step * math.sqrt(3) / 2))
    if est > MAX_CANDIDATES:
        step *= math.sqrt(est / MAX_CANDIDATES)
        if verbose:
            print(f"      шаг сетки увеличен до {step:.1f} м "
                  f"(иначе {int(est):,} кандидатов)", flush=True)

    pz = prep(zone)
    pts = []
    y, row = miny, 0
    while y <= maxy:
        offset = (step / 2.0) if row % 2 else 0.0
        x = minx + offset
        while x <= maxx:
            px = x + rnd.uniform(-jitter, jitter) * step
            py = y + rnd.uniform(-jitter, jitter) * step
            if pz.contains(Point(px, py)):
                pts.append((px, py))
            x += step
        y += step * math.sqrt(3) / 2.0
        row += 1
    return pts


def _score(pt, boundary_dist, cons, w):
    """Оценка точки: чем выше, тем лучше место для дерева."""
    x, y = pt
    score = w["clearance"] * min(boundary_dist / 3.0, 1.0)

    if w["walk"] and "walkway" in cons.parts:
        d = cons.distance("walkway", x, y)
        score += w["walk"] * (1.0 if 1.5 <= d <= 5.0
                              else max(0.0, 1.0 - abs(d - 3.0) / 8.0))
    if w["road"] and "road" in cons.parts:
        d = cons.distance("road", x, y)
        score += w["road"] * max(0.0, 1.0 - d / 15.0)
    if w["exist"] and "existing_tree" in cons.parts:
        d = cons.distance("existing_tree", x, y)
        score += w["exist"] * min(d / 10.0, 1.0)
    return score


def place_trees(zone, constraints, species, params, max_count=None, seed=42,
                verbose=True):
    """Жадная расстановка деревьев с гарантией минимального интервала."""
    if zone.is_empty:
        return []

    spacing = max(params["tree_spacing_m"], species["crown_m"] * 0.75)
    # шаг сетки: половина интервала между деревьями, но не мельче настройки
    step = max(params["grid_step_m"], spacing / 2.0)

    t0 = time.time()
    cands = _candidate_grid(zone, step, seed=seed, verbose=verbose)
    if verbose:
        print(f"      кандидатов: {len(cands):,} ({time.time()-t0:.1f} с)",
              flush=True)
    if not cands:
        return []

    t0 = time.time()
    boundary = zone.boundary
    w = WEIGHTS.get(params.get("mode", "street"), WEIGHTS["street"])
    scored = []
    for c in cands:
        bd = Point(c).distance(boundary)
        scored.append((_score(c, bd, constraints, w), c))
    scored.sort(key=lambda t: -t[0])
    if verbose:
        print(f"      оценка точек ({time.time()-t0:.1f} с)", flush=True)

    t0 = time.time()
    chosen = []
    chosen_xy = np.empty((0, 2))
    for sc, (x, y) in scored:
        if max_count and len(chosen) >= max_count:
            break
        if len(chosen_xy):
            d = np.hypot(chosen_xy[:, 0] - x, chosen_xy[:, 1] - y)
            if d.min() < spacing:
                continue
        checks = constraints.explain_point(x, y, "tree")
        if any(not c["ok"] for c in checks):
            continue                      # страховка: норма не нарушается
        chosen.append({"x": x, "y": y, "score": round(sc, 3), "checks": checks})
        chosen_xy = np.vstack([chosen_xy, [x, y]])
    if verbose:
        print(f"      отбор ({time.time()-t0:.1f} с)", flush=True)
    return chosen


def place_hedges(zone, constraints, species, params, max_count=None, verbose=True):
    """Живая изгородь: линии вдоль проезжей части со смещением на норматив."""
    if zone.is_empty or "road" not in constraints.parts:
        return []

    rule = constraints.rules["road"]
    offset = rule["shrub"] + params["boundary_margin_m"] + 0.2
    interval = species.get("hedge_interval_m", params["shrub_spacing_m"])

    t0 = time.time()
    road = constraints.merged("road")
    lines = []
    for geom in getattr(road, "geoms", [road]):
        if geom.geom_type not in ("LineString", "LinearRing"):
            continue
        if geom.length < interval * 5:
            continue                      # обрывки пунктира пропускаем
        for d in (offset, -offset):
            try:
                off = geom.offset_curve(d)
            except Exception:
                continue
            if off.is_empty:
                continue
            clipped = off.intersection(zone)
            for part in getattr(clipped, "geoms", [clipped]):
                if part.geom_type == "LineString" and part.length >= interval * 3:
                    lines.append(part)

    points = []
    for ln in lines:
        n = int(ln.length // interval)
        for i in range(n + 1):
            if max_count and len(points) >= max_count:
                break
            p = ln.interpolate(i * interval)
            checks = constraints.explain_point(p.x, p.y, "shrub")
            if any(not c["ok"] for c in checks):
                continue
            points.append({"x": p.x, "y": p.y, "checks": checks})
    if verbose:
        print(f"      изгородь: {len(points):,} шт ({time.time()-t0:.1f} с)",
              flush=True)
    return points
