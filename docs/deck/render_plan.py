# -*- coding: utf-8 -*-
"""Картинки «до/после» для презентации по результату расчёта.

    python docs/deck/render_plan.py <исходный.dxf> <папка результата> <выход.png> [--window x0,y0,x1,y1]

«До» — подоснова: сети, дороги, тротуары, здания, существующие деревья.
«После» — то же плюс допустимая зона посадки и посадки расчёта.
"""
import argparse
import glob
import json
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                       # noqa: E402
from matplotlib.collections import LineCollection    # noqa: E402
import shapely                                       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
import preview                                       # noqa: E402

STYLE = {  # класс: (цвет, толщина, подпись)
    "road": ("#6b6b6b", 1.0, "проезжая часть, борт"),
    "walkway": ("#b9b9b9", 0.7, "тротуар"),
    "building": ("#8d6e63", 1.0, "здание"),
    "power_cable": ("#e67e22", 1.2, "кабель"),
    "water": ("#1e88e5", 1.2, "водопровод, водосток"),
    "sewer": ("#795548", 1.2, "канализация"),
    "heating": ("#d81b60", 1.2, "теплосеть"),
    "gas": ("#f9a825", 1.2, "газопровод"),
    "utility": ("#5e35b1", 1.0, "прочие сети"),
    "existing_tree": ("#2e7d32", 0.6, "существующие деревья"),
}


def segments(geoms, box):
    out = []
    for g in geoms:
        try:
            if not g.intersects(box):
                continue
            for part in getattr(g, "geoms", [g]):
                if part.geom_type == "Polygon":
                    part = part.exterior
                if part.geom_type in ("LineString", "LinearRing"):
                    c = np.asarray(part.coords)[:, :2]
                    if len(c) > 1:
                        out.append(c)
                elif part.geom_type == "Point":
                    x, y = part.x, part.y
                    t = np.linspace(0, 2 * np.pi, 9)
                    out.append(np.column_stack([x + 0.6 * np.cos(t), y + 0.6 * np.sin(t)]))
        except Exception:
            continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dxf")
    ap.add_argument("result")
    ap.add_argument("out")
    ap.add_argument("--window", default=None)
    ap.add_argument("--only-after", action="store_true", help="одна панель: результат")
    ap.add_argument("--aspect", type=float, default=240 / 135, help="ширина/высота окна")
    ap.add_argument("--span", type=float, default=240.0, help="ширина окна, м")
    args = ap.parse_args()

    by_layer, _ = preview.load(args.dxf)
    cls, _ = preview.classify(by_layer, {})
    js = glob.glob(os.path.join(args.result, "*_explain.json"))[0]
    pl = json.load(open(js, encoding="utf-8"))["plantings"]
    trees = np.array([[p["x"], p["y"], p.get("crown_m", 6)] for p in pl if p["type"] == "дерево"])
    shrubs = np.array([[p["x"], p["y"]] for p in pl if p["type"] == "кустарник"])
    m = np.load(glob.glob(os.path.join(args.result, "*_masks.npz"))[0])

    if args.window:
        x0, y0, x1, y1 = map(float, args.window.split(","))
    else:
        # окно 240×135 м, где много и посадок, и подземных сетей: на слайде
        # должно быть видно, как посадки обходят коммуникации
        from scipy.spatial import cKDTree
        k = cKDTree(trees[:, :2]).query_ball_point(trees[:, :2], 60, return_length=True)
        nets = [l for l, c in cls.items()
                if c in ("power_cable", "water", "sewer", "heating", "gas")]
        pts = [np.asarray(p.coords)[:, :2] for l in nets for g in by_layer[l]
               for p in getattr(g, "geoms", [g]) if p.geom_type == "LineString"]
        if pts:
            nk = cKDTree(np.vstack(pts)).query_ball_point(trees[:, :2], 60,
                                                          return_length=True)
            k = k * np.log1p(nk)
        cx, cy = trees[int(np.argmax(k)), :2]
        hw, hh = args.span / 2, args.span / args.aspect / 2
        x0, y0, x1, y1 = cx - hw, cy - hh, cx + hw, cy + hh
    box = shapely.box(x0, y0, x1, y1)

    panels = (True,) if args.only_after else (False, True)
    fw = 8.0 if args.only_after else 16.0
    fig, axes = plt.subplots(1, len(panels), figsize=(fw, fw / len(panels) / args.aspect), dpi=150)
    axes = np.atleast_1d(axes)
    for ax, after in zip(axes, panels):
        ax.set_facecolor("white")
        if after:
            zone = m["tree"]
            ext = (float(m["x0"]), float(m["x0"]) + m["nx"] * float(m["cell"]),
                   float(m["y0"]), float(m["y0"]) + m["ny"] * float(m["cell"]))
            ax.imshow(np.ma.masked_where(~zone, zone), origin="lower", extent=ext,
                      cmap=matplotlib.colors.ListedColormap(["#c8e6c9"]), alpha=0.9,
                      interpolation="nearest", zorder=0)
        for c, (col, lw, _) in STYLE.items():
            lays = [l for l, k in cls.items() if k == c]
            segs = segments([g for l in lays for g in by_layer.get(l, [])], box)
            if segs:
                ax.add_collection(LineCollection(segs, colors=col, linewidths=lw, zorder=2))
        if after:
            sel = (trees[:, 0] > x0) & (trees[:, 0] < x1) & (trees[:, 1] > y0) & (trees[:, 1] < y1)
            for x, y, cr in trees[sel]:
                ax.add_patch(plt.Circle((x, y), cr / 2, color="#1b5e20", alpha=0.35, lw=0, zorder=3))
                ax.add_patch(plt.Circle((x, y), 0.45, color="#1b5e20", zorder=4))
            if len(shrubs):
                s = shrubs[(shrubs[:, 0] > x0) & (shrubs[:, 0] < x1)
                           & (shrubs[:, 1] > y0) & (shrubs[:, 1] < y1)]
                ax.scatter(s[:, 0], s[:, 1], s=4, c="#9ccc65", zorder=3, linewidths=0)
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])
        for sp in ax.spines.values():
            sp.set_color("#dddddd")
    fig.subplots_adjust(left=0.005, right=0.995, top=0.99, bottom=0.01, wspace=0.02)
    fig.savefig(args.out, dpi=150, facecolor="white")
    print("saved", args.out, "window", round(x0), round(y0), round(x1), round(y1),
          "trees in window", int(sel.sum()))


if __name__ == "__main__":
    main()
