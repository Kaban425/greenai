# -*- coding: utf-8 -*-
"""Стенд замера качества: тестовая улица с заранее известной геометрией.

Нужен, чтобы улучшения измерялись цифрами, а не впечатлением. Улица
собрана так, чтобы в ней встретились все трудные случаи из реальных
чертежей: широкий газон и узкая полоса, кабель под газоном, колодцы и
существующие деревья на слоях с невнятными именами, газон без подсказки
в имени слоя.

    python src/benchmark.py

Метрики:
  recognized_objects_pct — доля объектов неизвестных слоёв, получивших класс;
  trees                  — сколько деревьев посажено;
  tree_coverage_pct      — доля допустимой зоны деревьев под кронами;
  shrub_zone_cover_pct   — доля зоны «только кустарники», занятая кустами;
  violations             — нарушения нормативов (обязано быть 0);
  outside_zone           — посадки вне допустимой зоны (обязано быть 0);
  vector_violations      — нарушения по исходной геометрии, без растра
                           (обязано быть 0: расчёт такие посадки убирает);
  min_spacing_m          — минимальный интервал между стволами.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class _Line:
    geom_type = "LineString"
    is_empty = False

    def __init__(self, coords):
        self.coords = coords


def _rect(x0, y0, x1, y1):
    return [_Line([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])]


def _circle(cx, cy, r, n=20):
    a = np.linspace(0, 2 * np.pi, n)
    return _Line([(cx + r * np.cos(t), cy + r * np.sin(t)) for t in a])


def build_street():
    """Улица 400 x 70 м со всеми трудными случаями.

    Известные ответы для слоёв с невнятными именами хранятся в truth:
    по ним считается, правильно ли распознана геометрия.
    """
    rng = np.random.default_rng(7)
    layers = {
        "Граница работ": _rect(0, 0, 400, 70),
        # широкий газон 25 м (северная сторона) и узкая полоса 5 м (южная)
        "ГП_4.5_Устраиваемый газон": _rect(0, 40, 400, 65) + _rect(0, 5, 400, 10),
        "ДВ_ГП_П_Борт_БР100.30.15": [_Line([(0, 20), (400, 20)]),
                                     _Line([(0, 30), (400, 30)])],
        "Тротуар": [_Line([(0, 13), (400, 13)]), _Line([(0, 37), (400, 37)])],
        # кабель под широким газоном
        "output|Кабель электрический": [_Line([(0, 52), (400, 52)])],
    }
    truth = {}
    # колодцы на слое с невнятным именем
    layers["Слой_17"] = [_circle(x, 38.0, 0.45) for x in range(20, 400, 40)]
    truth["Слой_17"] = "well"
    # существующие деревья на слое с невнятным именем
    xs = rng.uniform(15, 385, 14)
    layers["ГП_изм_3"] = [_circle(x, 60.0, 3.0) for x in xs]
    truth["ГП_изм_3"] = "existing_tree"
    # подложка PDF, разобранная на примитивы: дублирует бортовой камень
    layers["PDF _Проект"] = [_Line([(x, 20.0), (x + 9.0, 20.0)])
                             for x in range(0, 400, 10)]
    truth["PDF _Проект"] = "road"
    # соглашение об именах проектировщика: «ПР7» у него значит газон
    for k in range(1, 4):
        layers[f"Газон ПР7_{k}"] = _rect(10 + 30 * k, 42, 30 + 30 * k, 44)
    layers["ПР7_9"] = _rect(250, 42, 280, 44)
    truth["ПР7_9"] = "lawn"
    # линия канализации на безымянном слое, подписанная «К1»
    layers["Слой_44"] = [_Line([(0, 67.0), (400, 67.0)])]
    truth["Слой_44"] = "sewer"
    truth["_texts"] = [(x, 67.8, "К1") for x in range(15, 400, 45)]

    truth["_trunks"] = np.column_stack([xs, np.full(len(xs), 60.0)])
    truth["_wells"] = np.column_stack([np.arange(20, 400, 40, dtype=float),
                                       np.full(len(range(20, 400, 40)), 38.0)])
    return layers, truth


def measure(verbose=True):
    import yaml
    import raster_engine as R
    from layers import classify_with_source, NON_OBSTACLE

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "config", "norms.yaml"), encoding="utf-8") as f:
        norms = yaml.safe_load(f)

    by_layer, truth = build_street()
    lc, src = classify_with_source(by_layer.keys())
    # Та же политика, что в расчёте (main.accept_weak): слабый способ не
    # назначает газон и границу работ — их назначает человек.
    from layers import PERMISSIVE

    def weak(got):
        return {k: v for k, v in got.items() if v not in PERMISSIVE}

    # распознавание по геометрии, если модуль есть
    try:
        import geo_classify
        extra = weak(geo_classify.classify_unknown(
            by_layer, {k: v for k, v in lc.items()}))
        for k, v in extra.items():
            lc[k], src[k] = v, "geometry"
    except ImportError:
        pass
    # распознавание по контексту, если модуль есть
    try:
        import context_classify as CC
        for fn, tag in ((lambda: CC.colocate(by_layer, lc), "colocate"),
                        (lambda: CC.tokens(lc), "tokens"),
                        (lambda: CC.labels(by_layer, lc, truth["_texts"]),
                         "labels")):
            for k, v in weak(fn()).items():
                lc[k], src[k] = v, tag
    except ImportError:
        pass

    # слои, чей верный класс разрешающий, слабым способам не по силе по
    # замыслу: их оставляют человеку, в долю распознанного они не входят
    names = [k for k in truth if not k.startswith("_")
             and truth[k] not in PERMISSIVE]
    wrong = [k for k in names if lc.get(k) not in (truth[k], "unknown")]
    unk_obj = sum(len(by_layer[k]) for k in names)
    ok_obj = sum(len(by_layer[k]) for k in names if lc.get(k) == truth[k])

    cons = R.RasterConstraints(by_layer, norms, lc, non_obstacle=NON_OBSTACLE,
                               cell=0.25, verbose=False)
    cons.verbose = False
    cons.tree_crown_m = 6.0
    tm, _ = cons.allowed_mask("tree")
    sm, _ = cons.allowed_mask("shrub")

    spacing = 10.0
    env = R.env_maps(cons, verbose=False)
    q = R.env_score(cons, tm, env, verbose=False)
    kw = {}
    if "narrow_spacing" in R.place_rows.__code__.co_varnames:
        kw["narrow_spacing"] = 6.0
    trees = R.place_rows(cons, tm, spacing, None, verbose=False, quality=q, **kw)
    trees = list(trees) + list(R.fill_remaining(cons, tm, trees, spacing,
                                                quality=q, verbose=False))
    shrubs = R.hedge_points(cons, sm, 0.8, 100000, verbose=False)
    if hasattr(R, "shrub_massifs"):
        shrubs = list(shrubs) + list(R.shrub_massifs(cons, sm & ~tm, shrubs,
                                                     verbose=False))

    def inside(pts, mk):
        bad = 0
        for p in pts:
            ix = int((p[0] - cons.x0) / cons.cell)
            iy = int((p[1] - cons.y0) / cons.cell)
            if not (0 <= iy < cons.ny and 0 <= ix < cons.nx and mk[iy, ix]):
                bad += 1
        return bad

    viol = 0
    for p in trees:
        viol += any(not c["ok"] for c in cons.explain_xy(p[0], p[1], "tree"))
    for p in shrubs:
        viol += any(not c["ok"] for c in cons.explain_xy(p[0], p[1], "shrub"))

    from scipy.spatial import cKDTree
    xy = np.array([[p[0], p[1]] for p in trees])
    min_sp = float(cKDTree(xy).query(xy, k=2)[0][:, 1].min()) if len(xy) > 1 else 0

    sh_zone = sm & ~tm
    sh_cov = R.coverage(cons, sh_zone, shrubs, 1.0) if sh_zone.any() else 0.0

    # Конфликты по истинным координатам — независимо от того, распознан ли
    # слой. Показывает, сколько посадок встало на существующее дерево или
    # на колодец, которых алгоритм не увидел.
    def conflicts(pts, targets, dist):
        if not len(pts) or not len(targets):
            return 0
        a = np.array([[p[0], p[1]] for p in pts])
        d = cKDTree(targets).query(a, k=1)[0]
        return int((d < dist).sum())

    res = {
        "recognized_objects_pct": round(100.0 * ok_obj / max(unk_obj, 1), 1),
        "wrong_class_layers": len(wrong),
        "trees_on_existing": conflicts(trees, truth["_trunks"], 4.0),
        "plants_on_wells": conflicts(list(trees) + list(shrubs),
                                     truth["_wells"], 0.7),
        "trees": len(trees),
        "tree_coverage_pct": round(100 * R.coverage(cons, tm, trees, 8.0), 1),
        "shrubs": len(shrubs),
        "shrub_zone_cover_pct": round(100 * sh_cov, 1),
        "violations": int(viol),
        "outside_zone": inside(trees, tm) + inside(shrubs, sm),
        "vector_violations": len({v["id"] for v in cons.vector_audit(
            [{"id": i, "type": "дерево", "x": p[0], "y": p[1]}
             for i, p in enumerate(trees)] +
            [{"id": -1 - i, "type": "кустарник", "x": p[0], "y": p[1]}
             for i, p in enumerate(shrubs)])}),
        "min_spacing_m": round(min_sp, 2),
    }
    if verbose:
        for k, v in res.items():
            print(f"  {k:<24} {v}")
    return res


if __name__ == "__main__":
    measure()
