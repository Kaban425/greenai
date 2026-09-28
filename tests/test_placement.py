# -*- coding: utf-8 -*-
"""Расстановка: сетка в широкой зоне держит шаг и вмещает не меньше россыпи.

    python tests/test_placement.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from raster_engine import _fill_zone, _fill_zone_grid  # noqa: E402


def _zone(w=40.0, h=25.0, cell=0.25, angle=0.4):
    xs, ys = np.meshgrid(np.arange(0, w, cell) + cell / 2,
                         np.arange(0, h, cell) + cell / 2)
    p = np.column_stack([xs.ravel(), ys.ravel()])
    c, s = np.cos(angle), np.sin(angle)
    rot = p @ np.array([[c, s], [-s, c]])
    # к сетке ячеек: зона повёрнута, но ячейки остаются по осям
    rot = (np.round(rot / cell - 0.5) + 0.5) * cell
    rot = np.unique(rot, axis=0)
    return rot, np.ones(len(rot)), np.array([c, s]), cell


def test_grid_keeps_spacing_and_count():
    pts, q, axis, cell = _zone()
    grid = _fill_zone_grid(pts, q, 8.0, axis, cell)
    free = _fill_zone(pts, q, 8.0)
    a = np.array([(x, y) for x, y, _ in grid])
    d = np.hypot(a[:, None, 0] - a[None, :, 0], a[:, None, 1] - a[None, :, 1])
    d[np.arange(len(a)), np.arange(len(a))] = 1e9
    assert d.min() >= 8.0 - 2 * cell, d.min()
    assert len(grid) >= 0.8 * len(free), (len(grid), len(free))


def test_generic_plant_layers_split_by_symbol_size():
    # «Растения фр1» из PDF: круг 4 м — дерево, 2,5 м — кустарник,
    # метка ствола 0,3 м — не растение; круг приходит замкнутой линией
    from shapely.geometry import Point, LineString
    import validate

    def ring(x, d):
        return LineString(Point(x, 0).buffer(d / 2).exterior.coords)
    geoms = [ring(0, 4.0), ring(20, 4.0), ring(40, 2.5),
             Point(0, 0).buffer(0.15), ring(60, 6.0)]
    ref, used, _ = validate.collect_reference(
        {"Новый_Растения фр1_Ном._пера__99": geoms},
        {"Новый_Растения фр1_Ном._пера__99": "reference_planting"})
    assert len(ref["tree"]) == 3 and len(ref["shrub"]) == 1


def test_alley_chain_follows_curved_band_with_spacing():
    # полоса-дуга радиусом 40 м: ряд должен идти вдоль неё с шагом 8 м
    from raster_engine import _alley_chain
    a = np.linspace(0, np.pi / 2, 800)
    P = np.column_stack([40 * np.cos(a), 40 * np.sin(a)])
    got = np.array([(x, y) for x, y, _ in _alley_chain(P, np.ones(len(P)), 8.0)])
    d = np.hypot(*(got[:, None] - got[None]).transpose(2, 0, 1))
    d[np.arange(len(got)), np.arange(len(got))] = 1e9
    assert d.min() >= 8.0 * 0.95
    arc = 40 * np.pi / 2
    assert len(got) >= int(arc / 8.0 * 0.85), len(got)
    r = np.hypot(got[:, 0], got[:, 1])
    assert np.allclose(r, 40, atol=0.5)            # не сходит с полосы


def test_existing_row_is_extended_with_same_step():
    import types
    from raster_engine import existing_row_extensions
    cons = types.SimpleNamespace(x0=0.0, y0=0.0, cell=0.5, nx=400, ny=40)
    cons.centers = {"existing_tree": np.array([[10.0, 10], [16, 10], [22, 10]])}
    mask = np.ones((40, 400), dtype=bool)
    got = sorted(round(x) for x, y, _ in existing_row_extensions(cons, mask))
    # продолжение в обе стороны с шагом 6 м: 4 и 28, 34 ...
    assert 4 in got and 28 in got and 34 in got, got
    assert all(abs(y - 10) < 1e-6 for _, y, _ in existing_row_extensions(cons, mask))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok    {name}")
