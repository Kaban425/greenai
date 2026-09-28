# -*- coding: utf-8 -*-
"""Карта ограничений: где сажать можно, где нельзя и почему.

Расстояния считаются через пространственный индекс STRtree, иначе на
реальном чертеже (сто тысяч линий) расчёт занимает часы.
"""
import time

import shapely
from shapely import STRtree
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from layers import classify_all, NON_OBSTACLE


def _explode(geom):
    return [g for g in getattr(geom, "geoms", [geom])
            if g is not None and not g.is_empty]


class Constraints:
    NETWORK_CLASSES = {"gas", "sewer", "water", "heating", "power_cable"}

    def __init__(self, by_layer, norms, site_geom=None, simplify=0.25,
                 verbose=True, areas=True, clip=None):
        self.norms = norms
        self.rules = norms["rules"]
        self.margin = norms["placement"]["boundary_margin_m"]
        self.verbose = verbose
        self.areas = areas
        self.clip = clip

        self.layer_class = classify_all(by_layer.keys())
        self.parts = {}           # класс -> список отдельных геометрий
        self.trees = {}           # класс -> пространственный индекс
        self._merged = {}         # класс -> объединённая геометрия (лениво)
        self.unknown_layers = []

        for layer, geoms in by_layer.items():
            cls = self.layer_class[layer]
            if cls == "unknown":
                self.unknown_layers.append(layer)
                continue
            if cls in NON_OBSTACLE:
                continue          # чужие проектные посадки — не ограничение
            bucket = self.parts.setdefault(cls, [])
            for g in geoms:
                if g is None or g.is_empty:
                    continue
                if simplify:
                    try:
                        g = g.simplify(simplify, preserve_topology=False)
                    except Exception:
                        pass
                if self.clip is not None:
                    try:
                        if not g.intersects(self.clip):
                            continue
                        g = g.intersection(self.clip)
                    except Exception:
                        pass
                bucket.extend(_explode(g))

        for cls, plist in list(self.parts.items()):
            if not plist:
                del self.parts[cls]
                continue
            self.trees[cls] = STRtree(plist)

        self.site = site_geom if site_geom is not None else self._guess_site()

    # ------------------------------------------------------------------ #

    def _log(self, msg):
        if self.verbose:
            print(msg, flush=True)

    @property
    def obstacles(self):
        """Совместимость со старым кодом: класс -> объединённая геометрия."""
        return {cls: self.merged(cls) for cls in self.parts}

    GRID = 0.01      # привязка к сетке 1 см ускоряет объединение в разы

    def merged(self, cls):
        if cls not in self._merged:
            try:
                self._merged[cls] = shapely.union_all(self.parts[cls],
                                                      grid_size=self.GRID)
            except Exception:
                self._merged[cls] = unary_union(self.parts[cls])
        return self._merged[cls]

    def distance(self, cls, x, y):
        """Расстояние от точки до ближайшего объекта класса."""
        tree = self.trees.get(cls)
        if tree is None:
            return float("inf")
        p = Point(x, y)
        idx = tree.nearest(p)
        if idx is None:
            return float("inf")
        return p.distance(self.parts[cls][int(idx)])

    def _guess_site(self):
        if "site_boundary" in self.parts:
            g = unary_union(self.parts["site_boundary"])
            polys = [p for p in _explode(g) if p.geom_type == "Polygon"]
            if polys:
                return unary_union(polys)
            hull = g.convex_hull
            if hull.geom_type == "Polygon" and hull.area > 0:
                return hull
        allg = [self.merged(c) for c in self.parts]
        if not allg:
            return Polygon()
        minx, miny, maxx, maxy = unary_union(allg).bounds
        return box(minx, miny, maxx, maxy)

    # ------------------------------------------------------------------ #

    def forbidden(self, kind):
        """kind = 'tree' | 'shrub'. (геометрия запрета, справка по классам)."""
        pieces, info = [], []
        service = self.norms["placement"].get("network_service_buffer_m", 1.0)
        for cls in self.parts:
            rule = self.rules.get(cls)
            if rule is None:
                continue
            dist = rule.get(kind)
            assumption = False
            if dist is None:
                if cls in self.NETWORK_CLASSES:
                    dist = service
                    assumption = True
                else:
                    continue
            t0 = time.time()
            self._log(f"        {cls:<16} {len(self.parts[cls]):>7,} линий, "
                      f"буфер {dist} м ...")
            buf = self.merged(cls).buffer(dist + self.margin, quad_segs=2)
            pieces.append(buf)
            info.append({
                "class": cls,
                "title": rule["title"],
                "distance_m": dist,
                "assumption": assumption,
                "hard_ban": False,
                "act": "Проектное допущение (норма таблицей не установлена)"
                       if assumption else rule["act"],
                "clause": "технологическая полоса обслуживания сети"
                          if assumption else rule["clause"],
                "excluded_area_m2": (round(buf.intersection(self.site).area, 1)
                                     if self.areas else None),
            })
            self._log(f"        {cls:<16} готово за {time.time()-t0:.1f} с")
        t0 = time.time()
        self._log("        объединяю зоны ...")
        if pieces:
            try:
                forb = shapely.union_all(pieces, grid_size=self.GRID)
            except Exception:
                forb = unary_union(pieces)
        else:
            forb = Polygon()
        self._log(f"        объединение зон ({time.time()-t0:.1f} с)")
        return forb, info

    def allowed(self, kind):
        forb, info = self.forbidden(kind)
        zone = self.site.difference(forb) if not forb.is_empty else self.site
        if zone.is_empty:
            return zone, info
        return zone.buffer(0), info

    def explain_point(self, x, y, kind, radius=30.0):
        """Проверка отступов в точке: ближайший объект каждого класса."""
        checks = []
        for cls in self.parts:
            rule = self.rules.get(cls)
            if rule is None:
                continue
            req = rule.get(kind)
            if req is None:
                if cls in self.NETWORK_CLASSES:
                    req = self.norms["placement"].get("network_service_buffer_m", 1.0)
                else:
                    continue
            d = self.distance(cls, x, y)
            if d > radius:
                continue
            checks.append({
                "class": cls,
                "title": rule["title"],
                "required_m": req,
                "actual_m": round(d, 2),
                "ok": d >= req,
                "act": rule["act"],
                "clause": rule["clause"],
            })
        checks.sort(key=lambda c: c["actual_m"] - c["required_m"])
        return checks


def format_explanation(checks, kind_ru="дерево"):
    if not checks:
        return f"Посадка ({kind_ru}) допустима: нормируемых объектов в радиусе 30 м не обнаружено."
    crit = checks[0]
    parts = [
        f"Посадка ({kind_ru}) допустима. Определяющее ограничение — "
        f"{crit['title'].lower()}: требуется не менее {crit['required_m']} м, "
        f"фактически {crit['actual_m']} м ({crit['act']}, {crit['clause']})."
    ]
    others = checks[1:4]
    if others:
        tail = "; ".join(
            f"{c['title'].lower()} — {c['actual_m']} м при норме {c['required_m']} м"
            for c in others
        )
        parts.append("Прочие проверенные отступы: " + tail + ".")
    return " ".join(parts)
