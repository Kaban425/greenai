# -*- coding: utf-8 -*-
"""Растровый движок: карта ограничений и расстановка посадок.

Точная работа с полигонами не выдерживает реальных подоснов: пунктирные
линии в чертеже разбиты на десятки тысяч отдельных штрихов, и построение
буферных зон занимает часы.

Здесь территория разбивается на сетку ячеек. Для каждого класса объектов
строится карта расстояний (преобразование расстояний, distance transform),
после чего проверка норматива — это сравнение двух чисел в ячейке.
Время расчёта зависит от площади участка, а не от числа линий.
"""
import re
import time

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree

from layers import is_filled, normalize

DEFAULT_CELL = 0.25          # размер ячейки сетки, м
MAX_CELLS = 40_000_000       # потолок по памяти


def _rings(geom):
    """Геометрия shapely -> список массивов координат."""
    t = geom.geom_type
    if t in ("LineString", "LinearRing"):
        return [np.asarray(geom.coords, dtype=float)]
    if t == "Polygon":
        out = [np.asarray(geom.exterior.coords, dtype=float)]
        out += [np.asarray(r.coords, dtype=float) for r in geom.interiors]
        return out
    if t == "Point":
        return [np.asarray([[geom.x, geom.y]], dtype=float)]
    if t.startswith("Multi") or t == "GeometryCollection":
        out = []
        for g in geom.geoms:
            out.extend(_rings(g))
        return out
    return []


def _densify(ring, step):
    """Полилиния -> точки не реже, чем через step метров."""
    if len(ring) == 1:
        return ring
    seg = np.diff(ring, axis=0)
    length = np.hypot(seg[:, 0], seg[:, 1])
    n = np.maximum(1, np.ceil(length / step)).astype(int)
    pts = [ring[:1]]
    for i, k in enumerate(n):
        t = np.linspace(0.0, 1.0, k + 1)[1:, None]
        pts.append(ring[i] + t * seg[i])
    return np.vstack(pts)


# Что чем является на поверхности. Код больше — рисуется позже и перекрывает.
SURFACE_CODE = {"road": 1, "walkway": 1, "tram": 1,
                "building": 2, "school_kindergarten": 2, "lawn": 3}
# Проектные покрытия кладутся поверх топоплана: топоплан показывает, как было,
# проект — как станет.
PROJECT_RE = re.compile(r"ДВ_ПП|покрыти|новые", re.I)


def _spans(shape, rings_px):
    """Отрезки строк внутри фигуры.

    Чётность пересечений считается сразу по всем контурам фигуры, поэтому
    внутренние контуры автоматически становятся дырками: газон-островок
    посреди асфальта не закрашивается асфальтом.
    """
    ny, nx = shape
    xa = np.concatenate([r[:, 0] for r in rings_px])
    ya = np.concatenate([r[:, 1] for r in rings_px])
    xb = np.concatenate([np.roll(r[:, 0], -1) for r in rings_px])
    yb = np.concatenate([np.roll(r[:, 1], -1) for r in rings_px])
    y0 = max(0, int(np.floor(ya.min())))
    y1 = min(ny - 1, int(np.ceil(ya.max())))
    for row in range(y0, y1 + 1):
        yc = row + 0.5
        hit = ((ya <= yc) & (yb > yc)) | ((yb <= yc) & (ya > yc))
        if not hit.any():
            continue
        t = (yc - ya[hit]) / (yb[hit] - ya[hit])
        xs = np.sort(xa[hit] + t * (xb[hit] - xa[hit]))
        for i in range(0, len(xs) - 1, 2):
            a = max(0, int(np.ceil(xs[i] - 0.5)))
            b = min(nx - 1, int(np.floor(xs[i + 1] - 0.5)))
            if b >= a:
                yield row, a, b


def _fill_polygons(shape, rings_px):
    """Растеризация полигонов построчно (правило чётности пересечений).

    Заливка приходит прямо из чертежа, поэтому площадь покрытия не нужно
    восстанавливать морфологией: контур уже замкнут и закрашен.
    """
    ny, nx = shape
    mask = np.zeros(shape, dtype=bool)
    for ring in rings_px:
        if len(ring) < 3:
            continue
        xs, ys = ring[:, 0], ring[:, 1]
        y0 = max(0, int(np.floor(ys.min())))
        y1 = min(ny - 1, int(np.ceil(ys.max())))
        if y1 < y0:
            continue
        x_a, y_a = xs, ys
        x_b, y_b = np.roll(xs, -1), np.roll(ys, -1)
        for row in range(y0, y1 + 1):
            yc = row + 0.5
            hit = ((y_a <= yc) & (y_b > yc)) | ((y_b <= yc) & (y_a > yc))
            if not hit.any():
                continue
            t = (yc - y_a[hit]) / (y_b[hit] - y_a[hit])
            xint = np.sort(x_a[hit] + t * (x_b[hit] - x_a[hit]))
            for i in range(0, len(xint) - 1, 2):
                a = max(0, int(np.ceil(xint[i] - 0.5)))
                b = min(nx - 1, int(np.floor(xint[i + 1] - 0.5)))
                if b >= a:
                    mask[row, a:b + 1] = True
    return mask


class RasterConstraints:
    """Карта ограничений на растровой сетке."""

    NETWORK_CLASSES = {"gas", "sewer", "water", "heating", "power_cable"}
    # Это не препятствия, а поверхности: границы участка и газоны.
    SURFACE_CLASSES = {"site_boundary", "site_boundary_aux", "lawn"}
    # Условные знаки, которые в чертеже рисуются кругом: дерево обозначается
    # проекцией кроны, опора — окружностью. Норматив отсчитывается от ствола
    # и от оси опоры, поэтому такие знаки сводятся к центру.
    CENTROID_CLASSES = {"existing_tree", "lighting_pole", "well"}
    # Крона дерева на чертеже собрана из нескольких дуг, опора — из нескольких
    # окружностей. Центр каждой дуги отдельным стволом считать нельзя: вместо
    # одной точки получается россыпь ложных стволов по всей кроне, и отступ
    # 4 м вырастает до семи. Поэтому центры склеиваются по расстоянию.
    CENTROID_MERGE_M = {"existing_tree": 2.5, "lighting_pole": 1.0, "well": 0.8}
    # Знак не бывает больше этого. Всё крупнее на слое знаков — линия:
    # кабель к опоре, подвес контактной сети, контур камеры или массива.
    # Раньше она сводилась к одной точке в середине, и дерево сажали
    # прямо на кабель освещения.
    SYMBOL_MAX_M = {"existing_tree": 16.0, "lighting_pole": 2.5, "well": 3.0}
    # Куда уходит линия со слоя знаков: у опор освещения это кабель.
    LINE_ON_SYMBOL_LAYER = {"lighting_pole": "power_cable"}
    # Граница работ главнее красных линий и границ улиц топоплана: те
    # тянутся по всей подоснове, и участок, собранный по всем сразу,
    # выходил за проект. Вспомогательные границы берутся, только если
    # граница работ не замкнулась или её нет.
    WORK_BOUNDARY = re.compile(r"границ\w*\s+(работ|проектир|благоустр|участк|"
                               r"отвод|объект)", re.IGNORECASE)

    def __init__(self, by_layer, norms, layer_class, non_obstacle=(),
                 cell=DEFAULT_CELL, clip=None, verbose=True, fills=None,
                 ignore_boundary=False):
        self.norms = norms
        self.rules = norms["rules"]
        self.margin = norms["placement"]["boundary_margin_m"]
        self.verbose = verbose
        self.layer_class = layer_class
        self.unknown_layers = []
        # Примечание к таблице 9.1: приведённые нормы относятся к деревьям
        # с диаметром кроны до 5 м и увеличиваются для более крупных крон.
        self.crown_factor = 1.0
        self.ignore_boundary = ignore_boundary
        self.boundary_lawn_share = None
        # Точность у границ: крона дерева не должна выходить за границу
        # работ, а ствол — стоять на самом краю газона.
        self.tree_crown_m = None
        self.edge_margin = {"tree": 0.7, "shrub": 0.3}
        # Требовать, чтобы вся проекция кроны лежала внутри границы работ,
        # — правило строгое: в практике крона свободно нависает над
        # тротуаром. По умолчанию держим внутри участка только ствол,
        # с запасом в метр. Полное вписывание кроны включается отдельно.
        self.crown_in_site = False
        self.site_trunk_margin = 1.0
        self.rings = {}          # исходные полилинии для отрисовки
        self.filled = {}         # закрашенные контуры: готовые площади

        # 1. Собираем точки по классам
        pts = {}
        lines = {}                # линии со слоёв условных знаков
        massifs = []              # контуры существующих массивов насаждений
        vec = {}                  # исходные полилинии и центры знаков — для
                                  # точной векторной проверки итога
        for layer, geoms in by_layer.items():
            cls = layer_class.get(layer, "unknown")
            if cls == "unknown":
                self.unknown_layers.append(layer)
                continue
            if cls == "ignore":
                continue          # слой помечен человеком как не значимый
            if cls in non_obstacle:
                continue
            raw = self.rings.setdefault(cls, [])
            if cls == "annotation":
                # Оформление в расчёт не идёт: рамки, таблицы и профили
                # раздували габарит сетки на километры, ячейка грубела,
                # а участок захватывал поле листа. Линии — только на схему.
                for g in geoms:
                    if g is not None and not g.is_empty and len(raw) < 4000:
                        raw.extend(r for r in _rings(g) if len(r) > 1)
                continue
            key = cls
            if cls == "site_boundary" and not self.WORK_BOUNDARY.search(
                    normalize(layer)):
                key = "site_boundary_aux"
            bucket = pts.setdefault(key, [])
            # Своё имя, а не fills: раньше переменная цикла затирала параметр
            # конструктора, и карта поверхностей по штриховкам для DXF не
            # строилась ни разу — заштрихованный асфальт посадку не запрещал.
            layer_fills = (self.filled.setdefault(key, [])
                           if is_filled(layer) else None)
            limit = self.SYMBOL_MAX_M.get(cls)
            for g in geoms:
                if g is None or g.is_empty:
                    continue
                for ring in _rings(g):
                    if not len(ring):
                        continue
                    if layer_fills is not None and len(ring) >= 3:
                        layer_fills.append(ring)
                    if cls in self.CENTROID_CLASSES and (
                            len(ring) == 1 or np.ptp(ring, axis=0).max() <= limit):
                        # круг кроны или опоры -> одна точка в центре
                        bucket.append(ring.mean(axis=0)[None, :])
                    elif cls in self.CENTROID_CLASSES:
                        # отдельно от центров знаков: их склейка по
                        # расстоянию слила бы всю линию в одну точку
                        to = self.LINE_ON_SYMBOL_LAYER.get(cls, cls)
                        lines.setdefault(to, []).append(_densify(ring, cell))
                        vec.setdefault(to, []).append(ring)
                        if cls == "existing_tree" and len(ring) >= 4 and \
                                np.hypot(*(ring[0] - ring[-1])) < 0.5:
                            massifs.append(ring)
                    else:
                        bucket.append(_densify(ring, cell))
                        vec.setdefault(key, []).append(ring)
                    if len(raw) < 12000:
                        raw.append(ring)

        self.vec = vec
        self.centers = {}
        self.points = {}
        for cls in set(pts) | set(lines):
            chunks = pts.get(cls) or []
            arr = np.vstack(chunks) if chunks else np.empty((0, 2))
            if cls in self.CENTROID_CLASSES and len(arr) > 1:
                before = len(arr)
                arr = self._merge_close(arr, self.CENTROID_MERGE_M.get(cls, 1.0))
                if before != len(arr):
                    self._log(f"        {cls}: условных знаков {before:,} -> "
                              f"{len(arr):,} после склейки дуг одного знака")
            if cls in self.CENTROID_CLASSES:
                # центры знаков отдельно от линий: по ним ищутся ряды деревьев
                self.centers[cls] = arr
            if lines.get(cls):
                ln = np.vstack(lines[cls])
                arr = np.vstack([arr, ln])
                self._log(f"        {cls}: линий со слоёв условных знаков "
                          f"{len(lines[cls]):,} — учтены линиями, а не точкой")
            if len(arr):
                self.points[cls] = arr

        if not self.points:
            raise SystemExit("В чертеже не распознано ни одного ограничения.")

        # 2. Границы расчётной области
        if clip is not None:
            x0, y0, x1, y1 = clip
        else:
            x0, y0, x1, y1 = self._robust_extent()

        pad = 5.0
        self.x0, self.y0 = x0 - pad, y0 - pad
        self.x1, self.y1 = x1 + pad, y1 + pad

        w = self.x1 - self.x0
        h = self.y1 - self.y0
        while (w / cell) * (h / cell) > MAX_CELLS:
            cell *= 1.5
        self.cell = cell
        self.nx = max(2, int(np.ceil(w / cell)))
        self.ny = max(2, int(np.ceil(h / cell)))
        self._log(f"      сетка {self.nx} x {self.ny} ячеек по {cell:.2f} м "
                  f"({w:.0f} x {h:.0f} м)")

        # 3. Карты расстояний по классам
        self.dist = {}
        for cls, arr in self.points.items():
            t0 = time.time()
            grid = self._mark(arr)
            n_on = int(grid.sum())
            if n_on == 0:
                continue
            d = ndimage.distance_transform_edt(~grid, sampling=self.cell)
            self.dist[cls] = d.astype(np.float32)
            self._log(f"        {cls:<16} {len(arr):>9,} точек, "
                      f"{time.time()-t0:.1f} с")

        # Площадные маски: граница работ и газоны.
        # Контуры в чертеже пунктирные, поэтому пунктир сначала замыкается
        # морфологическим расширением, затем контур заливается и сжимается назад.
        self.site_mask = None
        self.site_source = None
        if not self.ignore_boundary:
            for key, ru in (("site_boundary", "граница работ"),
                            ("site_boundary_aux", "красные линии и границы "
                                                  "улиц топоплана")):
                got = self._close_boundary(key)
                if got is None:
                    continue
                mask, share, cm = got
                # Пунктирный контур на длинной улице нередко замыкается лишь
                # на части: тогда засаживается кусок, а остальное молча
                # считается «за границей». Признак — линии границы, которые
                # остались вдали от замкнутого участка.
                if share < 0.6:
                    self._log(f"      {ru}: замкнулась лишь на части — у края "
                              f"участка {100*share:.0f}% её линий; не беру")
                    continue
                self.site_mask, self.site_source = mask, key
                self._log(f"      {ru} замкнута при зазоре {cm:g} м, у края "
                          f"участка {100*share:.0f}% её линий")
                break
        else:
            self._log("      граница работ не учитывается по запросу: "
                      "участок взят по следу геометрии")
        if self.site_mask is None:
            # Контур не замкнулся или отключён. Берём след самой геометрии.
            self.site_mask = self._footprint()
            self.site_source = "footprint"
            if not self.ignore_boundary:
                self._log(f"      ВНИМАНИЕ: граница работ не замкнулась, "
                          f"участок взят по следу геометрии: "
                          f"{self.site_mask.sum()*self.cell**2:,.0f} м². "
                          f"Посадки могут выйти за проект — проверьте "
                          f"слой границы работ")
        else:
            self._log(f"      участок в границах: "
                      f"{self.site_mask.sum()*self.cell**2:,.0f} м²")
        site_cells = float(self.site_mask.sum())

        self.lawn_mask = None
        for cm in (1.5, 3.0, 6.0):
            self.lawn_mask = self._area_mask("lawn", close_m=cm, min_frac=0.003)
            if self.lawn_mask is not None:
                break
        if self.lawn_mask is None:
            self._log("      ВНИМАНИЕ: контуры газонов не замкнулись — "
                      "привязка посадок к газонам отключена")
        else:
            self.lawn_mask &= self.site_mask
            self._log(f"      площадь газонов: "
                      f"{self.lawn_mask.sum()*self.cell**2:,.0f} м²")

        # Покрытия: проезжая часть и тротуары заданы линиями бортов и кромок.
        # Сажать внутри них нельзя, поэтому контуры заливаются и вычитаются.
        self.pavement_mask = self._pavement(site_cells)

        # Застройка: заливаем контуры зданий и павильонов.
        self.building_mask = self._area_mask("building", close_m=1.0,
                                             min_frac=0.0005)
        if self.building_mask is not None:
            self.building_mask &= self.site_mask
            if site_cells and self.building_mask.sum() > 0.5 * site_cells:
                self._log("      заливка застройки отвергнута: слишком велика")
                self.building_mask = None
        if self.building_mask is None:
            self.building_mask = np.zeros((self.ny, self.nx), dtype=bool)

        # Существующие массивы и живые изгороди: сажать внутрь нельзя.
        # Отступ от контура даёт карта расстояний, а середина крупного
        # массива от контура далеко — её закрывает заливка.
        self.massif_mask = np.zeros((self.ny, self.nx), dtype=bool)
        if massifs:
            px = [np.column_stack(((r[:, 0] - self.x0) / self.cell,
                                   (r[:, 1] - self.y0) / self.cell))
                  for r in massifs]
            self.massif_mask = _fill_polygons((self.ny, self.nx), px)
            self._log(f"      существующие массивы насаждений: {len(massifs)}, "
                      f"{self.massif_mask.sum()*self.cell**2:,.0f} м² закрыто "
                      f"для посадки")

        # Зелёная зона = участок минус твёрдые покрытия и застройка.
        # Такой способ надёжнее заливки контуров газонов: покрытия заданы
        # замкнутыми контурами бортов и кромок, а газоны — штриховкой.
        self.green_mask = (self.site_mask & ~self.pavement_mask
                           & ~self.building_mask & ~self.massif_mask)
        self._log(f"      зелёная зона (участок минус покрытия и застройка): "
                  f"{self.green_mask.sum()*self.cell**2:,.0f} м²")

        # Карта поверхностей по заливкам чертежа. Улица — открытая полоса,
        # её контур не замыкается, поэтому восстанавливать асфальт заливкой
        # дырок нельзя: это и приводило к посадкам посреди проезжей части.
        self.surface = np.zeros((self.ny, self.nx), dtype=np.uint8)
        self.surface_ok = False
        self.unpainted_mask = None
        if fills:
            t0 = time.time()
            order = sorted(range(len(fills)),
                           key=lambda i: 1 if PROJECT_RE.search(fills[i][0]) else 0)
            used = 0
            for i in order:
                layer, rings = fills[i]
                code = SURFACE_CODE.get(layer_class.get(layer))
                if code is None:
                    continue
                px = [np.column_stack(((r[:, 0] - self.x0) / self.cell,
                                       (r[:, 1] - self.y0) / self.cell))
                      for r in rings]
                try:
                    for row, a, b in _spans((self.ny, self.nx), px):
                        self.surface[row, a:b + 1] = code
                    used += 1
                except Exception:
                    continue
            lawn = self.site_mask & (self.surface == 3)
            painted = float((self.site_mask & (self.surface > 0)).sum())
            # Заштрихованные покрытия и застройка — запрет при любом исходе.
            # Раньше они вычитались, только если заливки дали газон; иначе
            # штриховка асфальта не учитывалась вовсе, и контуры бортов,
            # не замкнувшиеся в площадь, пускали деревья на проезжую часть.
            paved_hatch = self.site_mask & (self.surface == 1)
            built_hatch = self.site_mask & (self.surface == 2)
            self.pavement_mask |= paved_hatch
            self.building_mask |= built_hatch
            self.green_mask &= ~(paved_hatch | built_hatch)
            if site_cells and lawn.sum() > 0.02 * site_cells:
                self.surface_ok = True
                # Газоном считается не только закрашенное как газон. Часть
                # газонов в чертежах нарисована контуром без заливки, и раньше
                # они выбрасывались целиком — половина улицы оставалась без
                # посадок и без объяснения. Не закрашенное, не покрытое и не
                # застроенное пространство участка тоже считается пригодным;
                # отступы от кромок и сетей всё равно срежут лишнее.
                unpainted = (self.site_mask & (self.surface == 0)
                             & ~self.pavement_mask & ~self.building_mask)
                # Незакрашенное считать пригодным можно, только если покрытия
                # в чертеже действительно закрашены. Если проезжая часть
                # нарисована одними линиями бортов, она тоже «незакрашенная»,
                # и деревья уходили на асфальт, а честно закрашенные газон и
                # тротуар оставались пустыми.
                paved_fill = float((self.site_mask & (self.surface == 1)).sum())
                if paved_fill < 0.10 * site_cells:
                    self._log(f"      незакрашенное НЕ считается пригодным: "
                              f"покрытия закрашены лишь на "
                              f"{100*paved_fill/max(site_cells,1):.0f}% участка, "
                              f"проезжая часть могла остаться без заливки")
                    unpainted = np.zeros_like(unpainted)
                self.unpainted_mask = unpainted
                self.green_mask = (lawn | unpainted) & ~self.massif_mask
                self._log(f"      поверхности по заливкам: газоны "
                          f"{lawn.sum()*self.cell**2:,.0f} м², "
                          f"незакрашенное пригодное "
                          f"{unpainted.sum()*self.cell**2:,.0f} м², "
                          f"закрашено {100*painted/site_cells:.0f}% участка "
                          f"({used:,} заливок, {time.time()-t0:.1f} с)")
            else:
                self._log(f"      заливки не дали газонов "
                          f"(закрашено {100*painted/max(site_cells,1):.0f}% участка)")

        self.site_area = float(self.site_mask.sum()) * self.cell ** 2


    # ------------------------------------------------------------------ #

    def _robust_extent(self, keep=0.98):
        """Габариты расчётной области, устойчивые к выбросам координат.

        В чертежах попадаются объекты, унесённые за километры: рамки листов,
        схема расположения частей, сбойные точки. Габарит по крайним точкам
        раздувает область до десятков квадратных километров, ячейка сетки
        грубеет, а расчёт идёт впустую по пустому полю. Поэтому края
        отсекаются по процентилям, а результат проверяется на вменяемость.
        """
        # Габарит строится по ВСЕЙ распознанной геометрии, а не по границе
        # работ. Если граница замкнулась лишь на части улицы, по ней одной
        # сетка покрывала только эту часть — вторая половина улицы вообще
        # не попадала в расчёт и не могла быть ни засажена, ни объяснена.
        # Рамки листа сюда не попадают: неклассифицированные слои и
        # оформление в points не входят.
        src = np.vstack(list(self.points.values()))
        label = "всей распознанной геометрии"

        lo = (1.0 - keep) / 2 * 100
        hi = 100 - lo
        x0, x1 = np.percentile(src[:, 0], [lo, hi])
        y0, y1 = np.percentile(src[:, 1], [lo, hi])

        full_w = src[:, 0].max() - src[:, 0].min()
        full_h = src[:, 1].max() - src[:, 1].min()

        # Отсечение одиночного мусора. Раньше оставлялось одно крупнейшее
        # скопление, и это было ошибкой: улица состоит из нескольких
        # параллельных полос (газон, дорога, газон напротив), которые на
        # грубой сетке распадаются на отдельные скопления. Оставляя одно,
        # алгоритм выбрасывал остальную улицу — отсюда «половина чертежа
        # без посадок». Теперь сохраняются все скопления, где лежит хотя бы
        # два процента геометрии, а отбрасываются только редкие точки-выбросы.
        span = max(x1 - x0, y1 - y0)
        if span > 0 and len(src) > 50:
            step = max(span / 150.0, 2.0)
            ix = np.floor((src[:, 0] - x0) / step).astype(np.int64)
            iy = np.floor((src[:, 1] - y0) / step).astype(np.int64)
            gw = int((x1 - x0) / step) + 1
            gh = int((y1 - y0) / step) + 1
            ok = (ix >= 0) & (iy >= 0) & (ix < gw) & (iy < gh)
            if ok.sum() > 50:
                occ = np.zeros((gh, gw), dtype=bool)
                occ[iy[ok], ix[ok]] = True
                # склеиваем близкие полосы улицы в одно целое
                occ = ndimage.binary_dilation(occ, structure=np.ones((5, 5), bool))
                lab, n = ndimage.label(occ)
                if n > 1:
                    pl = np.zeros(len(src), dtype=np.int64)
                    pl[ok] = lab[iy[ok], ix[ok]]
                    counts = np.bincount(pl[ok], minlength=n + 1)
                    keep = counts >= 0.02 * ok.sum()
                    keep[0] = False
                    keep = self._main_group(lab, keep, counts, step)
                    sel = keep[pl] & ok
                    dropped = int(ok.sum() - sel.sum())
                    if sel.sum() > 0 and dropped > 0:
                        kx, ky = src[sel, 0], src[sel, 1]
                        nx0, nx1 = float(kx.min()), float(kx.max())
                        ny0, ny1 = float(ky.min()), float(ky.max())
                        self._log(f"      отброшено одиночных выбросов: "
                                  f"{dropped} точек; скоплений оставлено "
                                  f"{int(keep.sum())} из {n}")
                        x0, x1, y0, y1 = nx0, nx1, ny0, ny1

        # Граница работ входит в область целиком. На Олимпийской деревне
        # отсечение выбросов обрезало её краем сетки: контур переставал быть
        # замкнутым, и за участок принимался другой, чужой контур — все
        # деревья проектировщика оказывались «за границей».
        wb = self.points.get("site_boundary")
        if wb is not None and len(wb):
            bx0, by0 = wb.min(axis=0)
            bx1, by1 = wb.max(axis=0)
            if max(bx1 - bx0, by1 - by0) < 5000 and (
                    bx0 < x0 or by0 < y0 or bx1 > x1 or by1 > y1):
                x0, y0 = min(x0, bx0), min(y0, by0)
                x1, y1 = max(x1, bx1), max(y1, by1)
                self._log("      область расширена до границы работ целиком")

        w, h = x1 - x0, y1 - y0
        if full_w * full_h > 0 and (w * h) < 0.6 * full_w * full_h:
            self._log(f"      отброшены выбросы координат: габарит "
                      f"{full_w:.0f} x {full_h:.0f} м сократился до "
                      f"{w:.0f} x {h:.0f} м")
        self._log(f"      расчётная область по {label}: {w:.0f} x {h:.0f} м")
        return float(x0), float(y0), float(x1), float(y1)

    def _main_group(self, lab, keep, counts, step, gap_m=400.0):
        """Из далеко разнесённых кусков чертежа оставляет основной.

        В чертеж бывают вставлены копии и соседние объекты за километры
        от улицы (на посадочном плане Берзарины — в 5 и 11 км). Габарит по
        всем кускам растягивался на 15 км, ячейка сетки грубела, а граница
        работ, собранная из трёх мест, не замыкалась. Скопления ближе
        gap_m друг к другу — одна улица; из групп дальше друг от друга
        берётся та, где больше геометрии.
        """
        ids = [k for k in range(1, len(keep)) if keep[k]]
        if len(ids) < 2:
            return keep
        boxes = {k: ndimage.find_objects((lab == k).astype(np.int8))[0]
                 for k in ids}
        parent = {k: k for k in ids}

        def find(k):
            while parent[k] != k:
                k = parent[k]
            return k

        g = gap_m / step
        for i in ids:
            for j in ids:
                if i < j:
                    a, b = boxes[i], boxes[j]
                    dy = max(0, a[0].start - b[0].stop, b[0].start - a[0].stop)
                    dx = max(0, a[1].start - b[1].stop, b[1].start - a[1].stop)
                    if max(dx, dy) <= g:
                        parent[find(j)] = find(i)
        groups = {}
        for k in ids:
            groups.setdefault(find(k), []).append(k)
        if len(groups) < 2:
            return keep
        main = max(groups.values(), key=lambda ks: sum(counts[k] for k in ks))
        out = np.zeros_like(keep)
        out[main] = True
        lost = sum(counts[k] for k in ids if k not in main)
        self._log(f"      чертёж состоит из {len(groups)} разнесённых частей; "
                  f"расчёт по основной, отброшено точек: {lost:,}")
        return out

    def _close_boundary(self, key):
        """Замыкает пунктирную границу и выбирает зазор, при котором она
        замкнулась целиком. Возвращает (маска, доля линий у края, зазор).

        Раньше брался первый зазор, при котором хоть что-то замкнулось, —
        на длинной улице это бывал один квартал из пяти.
        """
        arr = self.points.get(key)
        if arr is None or not len(arr):
            return None
        best = None
        for cm in (2.0, 4.0, 8.0, 14.0, 25.0):
            m = self._area_mask(key, close_m=cm, min_frac=0.02)
            if m is None:
                continue
            share = self._edge_share(arr, m)
            if best is None or share > best[1] + 0.02:
                best = (m, share, cm)
            if share >= 0.9:
                break
        return best

    def _edge_share(self, arr, mask, tol_m=3.0):
        """Доля точек границы, лежащих на краю участка (не дальше tol_m)."""
        ix = ((arr[:, 0] - self.x0) / self.cell).astype(np.int64)
        iy = ((arr[:, 1] - self.y0) / self.cell).astype(np.int64)
        ok = (ix >= 0) & (ix < self.nx) & (iy >= 0) & (iy < self.ny)
        if not ok.any():
            return 0.0
        near = ndimage.distance_transform_edt(~mask, sampling=self.cell) <= tol_m
        return float(near[iy[ok], ix[ok]].mean())

    def _footprint(self, grow_m=6.0):
        """След геометрии: всё, что нарисовано на чертеже, с полями.

        Объекты расширяются, пустоты между ними заливаются, остаются
        крупные связные области. Это и есть коридор улицы, а не пустое поле
        чертежа вокруг неё и не кусок, до которого дотянулась граница работ.
        """
        allp = np.vstack(list(self.points.values()))
        grid = self._mark(allp)
        grown = ndimage.binary_fill_holes(self._grow(grid, grow_m))
        lab, n = ndimage.label(grown)
        if not n:
            return np.ones((self.ny, self.nx), dtype=bool)
        sizes = ndimage.sum(grown, lab, index=np.arange(1, n + 1))
        # не одна крупнейшая область, а все заметные: улица бывает разорвана
        big = 1 + np.flatnonzero(sizes >= 0.05 * sizes.max())
        keep = np.isin(lab, big)
        fp = self._shrink(keep, grow_m)
        return fp if fp.any() else keep

    @staticmethod
    def _merge_close(arr, tol):
        """Склеивает точки ближе tol в их общий центр."""
        if tol <= 0 or len(arr) < 2:
            return arr
        tree = cKDTree(arr)
        parent = list(range(len(arr)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, j in tree.query_pairs(tol):
            a, b = find(i), find(j)
            if a != b:
                parent[b] = a

        groups = {}
        for i in range(len(arr)):
            groups.setdefault(find(i), []).append(i)
        return np.array([arr[idx].mean(axis=0) for idx in groups.values()])

    def _log(self, msg):
        if self.verbose:
            print(msg, flush=True)

    def _mark(self, arr):
        grid = np.zeros((self.ny, self.nx), dtype=bool)
        ix = ((arr[:, 0] - self.x0) / self.cell).astype(np.int64)
        iy = ((arr[:, 1] - self.y0) / self.cell).astype(np.int64)
        ok = (ix >= 0) & (ix < self.nx) & (iy >= 0) & (iy < self.ny)
        grid[iy[ok], ix[ok]] = True
        return grid

    def _area_mask(self, cls, close_m, min_frac):
        """Площадь класса. Сначала пробуем готовые заливки из чертежа,
        и только если их нет — восстанавливаем площадь из пунктира."""
        rings = self.filled.get(cls) or []
        if rings:
            px = [np.column_stack(((r[:, 0] - self.x0) / self.cell,
                                   (r[:, 1] - self.y0) / self.cell))
                  for r in rings]
            m = _fill_polygons((self.ny, self.nx), px)
            if m.sum() >= min_frac * self.nx * self.ny:
                self._log(f"        {cls}: площадь из заливок чертежа "
                          f"({len(rings):,} контуров)")
                return m

        arr = self.points.get(cls)
        if arr is None:
            return None
        grid = self._mark(arr)
        filled = ndimage.binary_fill_holes(self._grow(grid, close_m))
        inner = self._shrink(filled, close_m)
        if inner.sum() < min_frac * self.nx * self.ny:
            return None
        return inner

    def _grow(self, mask, r_m):
        """Расширение на r_m метров. Через карту расстояний, а не кругом:
        круг в сотню ячеек на сетке в десятки миллионов считался минутами."""
        r_m = max(r_m, self.cell)
        return ndimage.distance_transform_edt(~mask, sampling=self.cell) <= r_m

    def _shrink(self, mask, r_m):
        """Сжатие на r_m метров; край сетки считается пустым, как у эрозии."""
        r_m = max(r_m, self.cell)
        padded = np.pad(mask, 1, constant_values=False)
        d = ndimage.distance_transform_edt(padded, sampling=self.cell)
        return d[1:-1, 1:-1] > r_m

    def _pavement(self, site_cells):
        """Залитые покрытия: проезжая часть и тротуары."""
        pav = np.zeros((self.ny, self.nx), dtype=bool)
        got = []
        for cls, close_m in (("road", 2.0), ("walkway", 1.5), ("tram", 2.0)):
            m = self._area_mask(cls, close_m=close_m, min_frac=0.001)
            if m is None:
                continue
            m &= self.site_mask
            # защита от неверной заливки: покрытие не может съесть весь участок
            if site_cells and m.sum() > 0.75 * site_cells:
                self._log(f"      заливка '{cls}' отвергнута: "
                          f"накрыла {100*m.sum()/site_cells:.0f}% участка")
                continue
            pav |= m
            got.append((cls, float(m.sum()) * self.cell ** 2))
        if got:
            self._log("      покрытия исключены: "
                      + ", ".join(f"{c} {a:,.0f} м²" for c, a in got))
        else:
            self._log("      ВНИМАНИЕ: покрытия не замкнулись, "
                      "исключены только нормативные отступы от кромок")
        return pav

    def cell_center(self, iy, ix):
        return (self.x0 + (ix + 0.5) * self.cell,
                self.y0 + (iy + 0.5) * self.cell)

    def required(self, cls, kind):
        """Нормативный отступ для класса; None — ограничения нет."""
        rule = self.rules.get(cls)
        if rule is None:
            return None
        d = rule.get(kind)
        if d is None:
            if cls in self.NETWORK_CLASSES:
                d = self.norms["placement"].get("network_service_buffer_m", 1.0)
            else:
                return None
        if kind == "tree" and self.crown_factor > 1.0:
            d = round(d * self.crown_factor, 2)
        return d

    def allowed_mask(self, kind, surface="auto"):
        """Булева маска: где посадка данного типа допустима.

        surface = "auto"  — газон, если его контуры восстановились (по умолчанию);
                  "green" — участок минус покрытия и застройка;
                  "lawn"  — только внутри восстановленных контуров газонов.
        """
        mask = self.green_mask.copy()
        info = []
        if self.pavement_mask.any() or self.building_mask.any():
            info.append({
                "class": "pavement",
                "title": "Твёрдые покрытия и застройка",
                "distance_m": None, "assumption": False, "hard_ban": True,
                "act": "СП 82.13330.2016",
                "clause": "посадка в границах покрытий и застройки не допускается",
                "excluded_area_m2": round(float((self.pavement_mask
                                                 | self.building_mask).sum())
                                          * self.cell ** 2, 1),
                "share_pct": None,
            })
        if surface == "auto" and self.surface_ok:
            # газон уже взят из заливок проекта, старые газоны топоплана не нужны
            surface = "green"
        if surface == "auto":
            # Газон — целевая поверхность посадки. Берём его, если контуры
            # восстановились и дали заметную площадь; иначе считаем по
            # остатку от покрытий, чтобы не потерять территорию целиком.
            frac = (float(self.lawn_mask.sum()) / max(float(self.site_mask.sum()), 1)
                    if self.lawn_mask is not None else 0.0)
            surface = "lawn" if frac >= 0.05 else "green"
        if surface == "lawn" and self.lawn_mask is not None:
            mask &= self.lawn_mask
            info.append({
                "class": "lawn", "title": "Посадка только в границах газонов",
                "distance_m": None, "assumption": True, "hard_ban": False,
                "act": "Проектное решение",
                "clause": "посадка вне асфальтовых и плиточных покрытий",
                "excluded_area_m2": None,
            })
        base = mask.copy()
        base_cells = float(base.sum()) or 1.0
        for cls, d in self.dist.items():
            if cls in self.SURFACE_CLASSES:
                continue
            req = self.required(cls, kind)
            if req is None:
                continue
            need = req + self.margin
            bad = d < need
            eaten = float((base & bad).sum()) * self.cell ** 2
            mask &= ~bad
            rule = self.rules[cls]
            assumption = rule.get(kind) is None
            info.append({
                "class": cls,
                "title": rule["title"],
                "distance_m": req,
                "assumption": assumption,
                "hard_ban": False,
                "act": "Проектное допущение (норма таблицей не установлена)"
                       if assumption else rule["act"],
                "clause": "технологическая полоса обслуживания сети"
                          if assumption else rule["clause"],
                "excluded_area_m2": round(eaten, 1),
                "share_pct": round(100.0 * eaten / (base_cells * self.cell ** 2), 1),
            })

        # Отступ от края газона: ствол на самом краю зоны — это посадка
        # на бордюре или на границе покрытия.
        em = self.edge_margin.get(kind, 0.0)
        if em > 0 and mask.any():
            inner = ndimage.distance_transform_edt(mask, sampling=self.cell) >= em
            lost = float((mask & ~inner).sum()) * self.cell ** 2
            mask &= inner
            info.append({"class": "edge", "title": "Край газона: ствол не на границе зоны",
                         "distance_m": em, "assumption": True, "hard_ban": False,
                         "act": "Проектное допущение",
                         "clause": "посадка не на кромке газона и не на бортовом камне",
                         "excluded_area_m2": round(lost, 1), "share_pct": None})

        # Крона дерева целиком внутри границы работ: иначе дерево формально
        # в участке, а крона нависает над соседней территорией.
        if kind == "tree" and not self.ignore_boundary:
            r = (self.tree_crown_m / 2.0 if (self.crown_in_site
                                             and self.tree_crown_m)
                 else self.site_trunk_margin)
            site_in = ndimage.distance_transform_edt(self.site_mask,
                                                     sampling=self.cell) >= r
            lost = float((mask & ~site_in).sum()) * self.cell ** 2
            mask &= site_in
            info.append({"class": "crown_in_site",
                         "title": ("Крона внутри границы работ"
                                   if self.crown_in_site
                                   else "Отступ ствола от границы работ"),
                         "distance_m": r, "assumption": True, "hard_ban": False,
                         "act": "Проектное допущение",
                         "clause": ("проекция кроны не выходит за границу участка"
                                    if self.crown_in_site
                                    else "ствол не на самой границе участка"),
                         "excluded_area_m2": round(lost, 1), "share_pct": None})

        if self.verbose:
            top = sorted((i for i in info if i.get("excluded_area_m2")),
                         key=lambda i: -i["excluded_area_m2"])[:6]
            if top:
                self._log(f"      что исключает площадь ({kind}):")
                for i in top:
                    sh = i.get("share_pct")
                    self._log(f"        {i['title'][:46]:<48} "
                              f"{i['excluded_area_m2']:>9,.0f} м²  "
                              + (f"{sh:>5.1f}%" if sh is not None else "    —"))
        return mask, info

    def vector_audit(self, items, tol=0.05):
        """Независимая проверка отступов по исходной геометрии чертежа.

        Расстановка и первая проверка смотрят растровые карты расстояний с
        шагом ячейки (0,5 м, на больших чертежах до 1–2 м), и ошибка растра
        прошла бы обе. Здесь расстояние считается точно: от посадки до
        ближайшей полилинии или знака каждого класса. Возвращает нарушения
        глубже tol: [{id, class, title, required_m, actual_m}].
        """
        import shapely
        if not items:
            return []
        kinds = {"дерево": "tree", "кустарник": "shrub"}
        pts = shapely.points([(it["x"], it["y"]) for it in items])
        out = []
        for cls in set(self.vec) | (self.CENTROID_CLASSES & set(self.points)):
            if cls in self.SURFACE_CLASSES:
                continue
            req = {k: self.required(cls, k) for k in ("tree", "shrub")}
            if req["tree"] is None and req["shrub"] is None:
                continue
            if cls in self.CENTROID_CLASSES:
                # знаки — теми же склеенными центрами, что и в расчёте:
                # центр отдельной дуги кроны лежит на её краю, а не у ствола
                geoms = list(shapely.points(self.points[cls]))
            else:
                geoms = [shapely.points(r[0]) if len(r) == 1 else shapely.linestrings(r)
                         for r in self.vec[cls]]
            if not geoms:
                continue
            tree = shapely.STRtree(geoms)
            idx, dist = tree.query_nearest(pts, return_distance=True,
                                           all_matches=False)
            for i, d in zip(idx[0], dist):
                it = items[i]
                r = req.get(kinds.get(it["type"], "tree"))
                if r is not None and d < r - tol:
                    out.append({"id": it["id"], "class": cls,
                                "title": self.rules[cls]["title"],
                                "required_m": r, "actual_m": round(float(d), 2)})
        return out

    def explain_xy(self, x, y, kind, radius=30.0):
        """Проверка отступов в точке по картам расстояний."""
        ix = int((x - self.x0) / self.cell)
        iy = int((y - self.y0) / self.cell)
        if not (0 <= ix < self.nx and 0 <= iy < self.ny):
            return []
        checks = []
        for cls, d in self.dist.items():
            if cls in self.SURFACE_CLASSES:
                continue
            req = self.required(cls, kind)
            if req is None:
                continue
            actual = float(d[iy, ix])
            if actual > radius:
                continue
            rule = self.rules[cls]
            checks.append({
                "class": cls,
                "title": rule["title"],
                "required_m": req,
                "actual_m": round(actual, 2),
                "ok": actual >= req,
                "act": rule["act"],
                "clause": rule["clause"],
            })
        checks.sort(key=lambda c: c["actual_m"] - c["required_m"])
        return checks


# ---------------------------------------------------------------------- #
#  Расстановка
# ---------------------------------------------------------------------- #

WEIGHTS = {
    "street": {"clearance": 0.30, "walk": 0.20, "road": 0.40, "exist": 0.10},
    "grove":  {"clearance": 0.60, "walk": 0.10, "road": 0.00, "exist": 0.30},
    "mixed":  {"clearance": 0.45, "walk": 0.25, "road": 0.15, "exist": 0.15},
}


def score_map(cons, mask, mode="street"):
    """Карта привлекательности точек для посадки дерева."""
    w = WEIGHTS.get(mode, WEIGHTS["street"])
    # запас до ближайшей запретной ячейки
    clearance = ndimage.distance_transform_edt(mask, sampling=cons.cell)
    s = w["clearance"] * np.clip(clearance / 3.0, 0, 1)

    if w["walk"] and "walkway" in cons.dist:
        d = cons.dist["walkway"]
        near = np.where((d >= 1.5) & (d <= 5.0), 1.0,
                        np.clip(1.0 - np.abs(d - 3.0) / 8.0, 0, 1))
        s += w["walk"] * near
    if w["road"] and "road" in cons.dist:
        s += w["road"] * np.clip(1.0 - cons.dist["road"] / 15.0, 0, 1)
    if w["exist"] and "existing_tree" in cons.dist:
        s += w["exist"] * np.clip(cons.dist["existing_tree"] / 10.0, 0, 1)

    s = s.astype(np.float32)
    s[~mask] = -1.0
    return s


def _disc(radius_cells):
    r = int(np.ceil(radius_cells))
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y) <= radius_cells ** 2, r


def pick_points(cons, score, spacing_m, count, jitter=0.0, seed=42):
    """Жадный отбор: лучшая точка, затем гашение круга вокруг неё.

    Кандидаты сортируются по оценке один раз, дальше идёт проход сверху
    вниз с пропуском уже погашенных ячеек. Результат тот же, что у поиска
    максимума заново на каждом шаге, но без повторного обхода всей сетки:
    на тысячах посадок это разница в десятки раз.
    """
    rnd = np.random.default_rng(seed)
    flat = score.ravel()
    cand = np.flatnonzero(flat > 0)
    if cand.size == 0 or count <= 0:
        return []
    order = cand[np.argsort(-flat[cand], kind="stable")]
    blocked = np.zeros(score.shape, dtype=bool)
    disc, r = _disc(spacing_m / cons.cell)
    out = []
    nx, ny = cons.nx, cons.ny
    limit = int(count)
    for idx in order:
        iy, ix = divmod(int(idx), nx)
        if blocked[iy, ix]:
            continue
        x, y = cons.cell_center(iy, ix)
        if jitter:
            x += float(rnd.uniform(-jitter, jitter))
            y += float(rnd.uniform(-jitter, jitter))
        out.append((x, y, float(flat[idx])))
        if len(out) >= limit:
            break
        y0, y1 = max(0, iy - r), min(ny, iy + r + 1)
        x0, x1 = max(0, ix - r), min(nx, ix + r + 1)
        sub = disc[(y0 - iy + r):(y1 - iy + r), (x0 - ix + r):(x1 - ix + r)]
        blocked[y0:y1, x0:x1] |= sub
    return out


def solar_positions(lat_deg=55.75, days=(105, 172, 236, 288),
                    hours=(8, 10, 12, 14, 16)):
    """Положения солнца для широты Москвы.

    days — номера дней года: 15 апреля, 21 июня, 24 августа, 15 октября,
    то есть вегетационный период целиком. hours — истинное солнечное время.
    Возвращает список (азимут от севера по часовой, высота над горизонтом)
    только для моментов, когда солнце выше 5°: ниже тени бесконечны и
    смысла не имеют.
    """
    import math as _m
    lat = _m.radians(lat_deg)
    out = []
    for n in days:
        decl = _m.radians(23.45 * _m.sin(_m.radians(360 * (284 + n) / 365)))
        for h in hours:
            omega = _m.radians(15.0 * (h - 12))          # часовой угол
            sin_alt = (_m.sin(lat) * _m.sin(decl)
                       + _m.cos(lat) * _m.cos(decl) * _m.cos(omega))
            alt = _m.asin(max(-1.0, min(1.0, sin_alt)))
            if _m.degrees(alt) < 5:
                continue
            cos_az = ((_m.sin(decl) - _m.sin(alt) * _m.sin(lat))
                      / max(_m.cos(alt) * _m.cos(lat), 1e-6))
            az = _m.acos(max(-1.0, min(1.0, cos_az)))
            if omega > 0:                                 # после полудня
                az = 2 * _m.pi - az
            out.append((_m.degrees(az), _m.degrees(alt)))
    return out


def env_maps(cons, building_height_m=15.0, north_deg=0.0, verbose=True):
    """Карты условий среды для выбора места посадки.

    insolation — доля времени, когда точка освещена. Тень строится для
        нескольких положений солнца за вегетационный период и усредняется:
        одно полуденное положение даёт заниженную картину, потому что за
        день тень поворачивается почти на 180 градусов.
    north_deg — куда на чертеже смотрит север: угол в градусах по часовой
        стрелке от оси Y. Чертёж бывает повёрнут, и без этого угла тень
        ложится не туда.
    root_space — объём для корней: расстояние до ближайшего покрытия.
    comfort — польза людям: тень на пешеходном пути в 2-5 м от дорожки.
    drainage — водный режим по близости к ливневой сети и дождеприёмникам.
    """
    import math as _m
    out = {}

    sun = solar_positions()
    lit = np.ones((cons.ny, cons.nx), dtype=np.float32)
    if cons.building_mask is not None and cons.building_mask.any() and sun:
        acc = np.zeros((cons.ny, cons.nx), dtype=np.float32)
        for az, alt in sun:
            length_m = building_height_m / max(_m.tan(_m.radians(alt)), 0.05)
            length_m = min(length_m, 60.0)          # длинные тени не считаем
            # направление тени — противоположное азимуту солнца
            a = _m.radians(az + 180.0 - north_deg)
            dx, dy = _m.sin(a), _m.cos(a)
            steps = max(1, int(round(length_m / cons.cell)))
            sh = np.zeros((cons.ny, cons.nx), dtype=bool)
            for k in range(1, steps + 1):
                ox, oy = int(round(dx * k)), int(round(dy * k))
                src = cons.building_mask
                ys = slice(max(0, oy), cons.ny + min(0, oy))
                xs = slice(max(0, ox), cons.nx + min(0, ox))
                ys0 = slice(max(0, -oy), cons.ny + min(0, -oy))
                xs0 = slice(max(0, -ox), cons.nx + min(0, -ox))
                sh[ys, xs] |= src[ys0, xs0]
            acc += sh.astype(np.float32)
        shaded = acc / len(sun)
        lit = (1.0 - shaded).astype(np.float32)
        if verbose:
            heavy = float((shaded > 0.5).sum()) * cons.cell ** 2
            print(f"      инсоляция: {len(sun)} положений солнца, "
                  f"в тени больше половины времени {heavy:,.0f} м²", flush=True)
    out["insolation"] = lit
    out["shadow"] = lit < 0.5

    pav = cons.pavement_mask | cons.building_mask
    out["root_space"] = ndimage.distance_transform_edt(
        ~pav, sampling=cons.cell).astype(np.float32)

    if "walkway" in cons.dist:
        d = cons.dist["walkway"]
        out["comfort"] = np.clip(1.0 - np.abs(d - 3.0) / 6.0, 0, 1).astype(np.float32)
    else:
        out["comfort"] = np.zeros((cons.ny, cons.nx), dtype=np.float32)

    drain = None
    for cls in ("well", "water", "sewer"):
        if cls in cons.dist:
            drain = cons.dist[cls] if drain is None else np.minimum(
                drain, cons.dist[cls])
    out["drainage"] = (np.clip(1.0 - np.abs((drain if drain is not None
                                             else np.full((cons.ny, cons.nx), 10.0))
                                            - 6.0) / 12.0, 0, 1)
                       .astype(np.float32))
    return out


# Веса условий среды при выборе точки в створе ряда.
ENV_WEIGHTS = {"root_space": 0.40, "insolation": 0.25,
               "comfort": 0.20, "drainage": 0.15}


def env_score(cons, mask, env=None, weights=None, verbose=True):
    """Сводная пригодность места: корневой объём, свет, польза людям, влага."""
    env = env or env_maps(cons, verbose=verbose)
    w = weights or ENV_WEIGHTS
    rs = np.clip(env["root_space"] / 3.0, 0, 1)
    sc = (w["root_space"] * rs
          + w["insolation"] * env["insolation"]
          + w["comfort"] * env["comfort"]
          + w["drainage"] * env["drainage"]).astype(np.float32)
    sc[~mask] = -1.0
    return sc


NARROW_WIDTH_M = 6.0     # полоса уже этого — сажаем компактные породы


def place_rows(cons, mask, spacing_m, count, min_zone_m2=12.0, phases=6,
               seed=42, verbose=True, quality=None, narrow_spacing=None,
               alley=True, seeds=None):
    """Рядовая посадка вдоль осевой линии зелёной полосы.

    Проектировщик ставит деревья не там, где «лучше по баллам», а рядом
    с постоянным шагом по середине газона — так формируется аллея. Поэтому
    каждая связная зона раскладывается по главной оси (метод главных
    компонент), вдоль оси берутся точки через заданный шаг, и в каждом
    створе выбирается ячейка с наибольшим запасом до границы: она лежит
    на середине полосы. Кривизна учитывается сама собой, так как середина
    ищется отдельно в каждом створе.

    phases — сколько начальных сдвигов ряда перебрать: ряд, начатый на
    полшага раньше, может дать на одно дерево больше.
    """
    if mask is None or not mask.any():
        return []

    clearance = ndimage.distance_transform_edt(mask, sampling=cons.cell)
    if quality is None:
        quality = clearance
    else:
        # запас до границы всё равно важен: узкое место дерево не прокормит
        quality = quality * 0.7 + 0.3 * np.clip(clearance / 3.0, 0, 1)
    lab, n_zones = ndimage.label(mask)
    if n_zones == 0:
        return []

    cell_area = cons.cell ** 2
    sizes = ndimage.sum(mask, lab, index=np.arange(1, n_zones + 1)) * cell_area
    order = np.argsort(-sizes)                  # крупные полосы важнее

    out = []
    groups = alleys = 0
    road = cons.dist.get("road") if alley else None
    for zi in order:
        if count and sum(len(b[0]) for b in out) >= count:
            break
        if sizes[zi] < min_zone_m2:
            continue
        rows, cols = np.where(lab == zi + 1)
        xs = cons.x0 + (cols + 0.5) * cons.cell
        ys = cons.y0 + (rows + 0.5) * cons.cell
        pts = np.column_stack([xs, ys])
        clr = quality[rows, cols]

        # В узкой полосе крупная крона не помещается: там сажают штамбовые
        # и компактные формы с кроной 4-5 м, и шаг у них меньше. Иначе полоса
        # вдоль тротуара получает деревья через 10 м, как будто там липа.
        zone_w = float(clearance[rows, cols].max()) * 2.0
        spc = spacing_m
        is_narrow = bool(narrow_spacing) and zone_w < NARROW_WIDTH_M
        if is_narrow:
            spc = narrow_spacing

        if road is not None and len(pts) >= 4:
            rz = road[rows, cols]
            if rz.min() <= ALLEY_NEAR_M:
                band = np.abs(rz - (rz.min() + ALLEY_OFFSET_M)) <= max(
                    0.75 * cons.cell, 0.3)
                if band.sum() >= 2:
                    row_pts = _alley_chain(pts[band], clr[band], spc)
                    if row_pts:
                        alleys += 1
                        out.append((row_pts, spc, is_narrow))
                        continue

        if len(pts) < 4:
            k = int(np.argmax(clr))
            out.append(([(float(pts[k, 0]), float(pts[k, 1]), float(clr[k]))],
                        spc, is_narrow))
            continue

        centre = pts.mean(axis=0)
        rel = pts - centre
        # главная ось полосы
        cov = np.cov(rel.T)
        w, v = np.linalg.eigh(cov)
        u = v[:, int(np.argmax(w))]
        t = rel @ u

        # Схема выбирается по форме самой зоны, а не одна на весь объект.
        # Вытянутая полоса вдоль улицы — рядовая посадка; широкое пятно
        # у школы или во дворе — свободная группа. На улице длиной в
        # километры и то и другое встречается одновременно.
        elong = float(np.sqrt(max(w) / max(min(w), 1e-9)))
        # Вытянутая, но широкая зона вмещает несколько рядов: один ряд
        # по оси и россыпь досадки по бокам выглядели неряшливо, поэтому
        # там тоже сетка, выровненная по оси полосы.
        wide = zone_w >= 1.5 * spc
        if (elong < 2.5 or wide) and sizes[zi] > (spc ** 2) * 2:
            block = _fill_zone_grid(pts, clr, spc, u, cons.cell)
            if verbose and len(block) > 1:
                groups += 1
            out.append((block, spc, is_narrow))
            continue
        t0, t1 = t.min(), t.max()
        length = t1 - t0
        if length < spc * 0.5:
            k = int(np.argmax(clr))
            out.append(([(float(pts[k, 0]), float(pts[k, 1]), float(clr[k]))],
                        spc, is_narrow))
            continue

        best = []
        for ph in range(max(1, phases)):
            shift = spc * ph / max(1, phases)
            targets = np.arange(t0 + shift, t1 + 1e-9, spc)
            cand = []
            for tt in targets:
                near = np.abs(t - tt) <= spc * 0.2
                if not near.any():
                    continue
                idx = np.where(near)[0]
                k = idx[int(np.argmax(clr[idx]))]
                cand.append((float(pts[k, 0]), float(pts[k, 1]), float(clr[k])))
            if len(cand) > len(best):
                best = cand
        out.append((best, spc, is_narrow))      # ряд целиком, одним блоком

    # Интервал между стволами соблюдается жёстко — и между зонами, и внутри
    # одного ряда: в створе точка выбирается в окне вокруг расчётной позиции,
    # и соседние точки могли съехать друг к другу.
    kept = []
    narrow_idx = []
    arr = np.empty((0, 2))
    if seeds:
        # продолжения рядов существующих деревьев — первыми: их шаг задан
        # самим рядом, остальные посадки держат интервал уже от них
        kept = list(seeds)
        arr = np.array([[x, y] for x, y, _ in seeds])
    for row_pts, spc_b, narrow_b in sorted(out, key=lambda r: -len(r[0])):
        if not row_pts:
            continue
        block = []
        for x, y, sc in row_pts:
            near_kept = len(arr) and np.hypot(arr[:, 0] - x,
                                              arr[:, 1] - y).min() < spc_b * 0.95
            near_block = block and min(np.hypot(bx - x, by - y)
                                       for bx, by, _ in block) < spc_b * 0.95
            if near_kept or near_block:
                continue
            block.append((x, y, sc))
            if count and len(kept) + len(block) >= count:
                break
        if block:
            if narrow_b:
                narrow_idx.extend(range(len(kept), len(kept) + len(block)))
            kept.extend(block)
            arr = np.vstack([arr, np.array([[p[0], p[1]] for p in block])])
        if count and len(kept) >= count:
            break
    place_rows.last_narrow = narrow_idx
    if verbose:
        print(f"      размещение: зон {int((sizes >= min_zone_m2).sum())}, "
              f"из них аллеей вдоль борта {alleys}, групповых {groups}, "
              f"продолжений существующих рядов {len(seeds or [])}, "
              f"точек {len(kept)}, "
              f"в узких полосах компактными породами {len(narrow_idx)}",
              flush=True)
    return kept


# Аллея вдоль борта: зона, край которой ближе ALLEY_NEAR_M к проезжей части,
# засаживается рядом на постоянном отступе — ближайшем допустимом плюс
# ALLEY_OFFSET_M. Так делают проектировщики (медиана у Камчатской 2,75 м
# при норме 2 м), а ряд по оси зоны или сетка в широкой полосе уводили
# деревья от улицы.
ALLEY_NEAR_M = 5.0
ALLEY_OFFSET_M = 0.5


def _alley_chain(P, q, spc):
    """Ряд с шагом spc по узкой полосе точек P, повторяющий её изгибы.

    Каждое следующее дерево — точка полосы на расстоянии около шага
    впереди текущего; направление держится, поэтому ряд огибает повороты
    и не возвращается назад. Где полоса прерывается (въезд, остановка),
    ряд начинается заново с ближайшего конца оставшейся полосы.
    """
    from scipy.spatial import cKDTree
    if len(P) == 0:
        return []
    kd = cKDTree(P)
    left = np.ones(len(P), dtype=bool)
    chosen = []
    qn = (q - q.min()) / (np.ptp(q) or 1.0)
    for _ in range(200):
        idx = np.where(left)[0]
        if not len(idx):
            break
        R = P[idx]
        if len(R) >= 3:
            w, v = np.linalg.eigh(np.cov((R - R.mean(axis=0)).T))
            u = v[:, int(np.argmax(w))]
            cur = int(idx[int(np.argmin((R - R.mean(axis=0)) @ u))])
        else:
            cur = int(idx[0])
        chain, dirv = [cur], None
        while True:
            best, best_s = None, None
            for j in kd.query_ball_point(P[cur], spc * 1.35):
                if not left[j]:
                    continue
                dv = P[j] - P[cur]
                d = float(np.hypot(*dv))
                if d < spc * 0.95:
                    continue
                if dirv is not None and float(dv @ dirv) < 0.6 * d:
                    continue
                if chosen and np.hypot(*(np.asarray([P[c] for c in chosen]) - P[j]).T).min() < spc * 0.95:
                    continue
                s = abs(d - spc) / spc - 0.3 * qn[j]
                if best_s is None or s < best_s:
                    best, best_s = j, s
            if best is None:
                break
            dirv = (P[best] - P[cur]) / (np.hypot(*(P[best] - P[cur])) or 1.0)
            chain.append(best)
            cur = best
        for c in chain:
            left[kd.query_ball_point(P[c], spc * 0.95)] = False
            left[c] = False
        chosen.extend(chain)
    return [(float(P[i, 0]), float(P[i, 1]), float(q[i])) for i in chosen]


def existing_row_extensions(cons, mask, step_range=(4.0, 12.0), max_ext=6):
    """Места, продолжающие ряды существующих деревьев с тем же шагом.

    Ряд — три дерева почти на одной прямой с равным шагом. От последнего
    дерева ряда откладывается тот же шаг, пока место допустимо; два
    недопустимых места подряд (въезд, колодец) обрывают продолжение.
    Проектировщики так и достраивают аллеи: модель места показала, что
    близость к существующему дереву для них — главный признак.
    """
    from scipy.spatial import cKDTree
    E = getattr(cons, "centers", {}).get("existing_tree")
    if E is None or len(E) < 3:
        return []
    kd = cKDTree(E)
    lo, hi = step_range
    out = []

    def allowed(p):
        ix = int((p[0] - cons.x0) / cons.cell)
        iy = int((p[1] - cons.y0) / cons.cell)
        return 0 <= ix < cons.nx and 0 <= iy < cons.ny and bool(mask[iy, ix])

    for i, j in kd.query_pairs(hi):
        v = E[j] - E[i]
        L = float(np.hypot(*v))
        if L < lo:
            continue
        tol = 0.5 + 0.1 * L
        for a, b, vv in ((i, j, v), (j, i, -v)):
            if kd.query(E[b] + vv)[0] > tol:      # нет третьего дерева ряда
                continue
            if kd.query(E[a] - vv)[0] <= tol:     # a — не конец ряда
                continue
            miss = 0
            for m in range(1, max_ext + 1):
                p = E[a] - m * vv
                if allowed(p):
                    out.append((float(p[0]), float(p[1]), L))
                    miss = 0
                else:
                    miss += 1
                    if miss >= 2:
                        break
    # одно место на ряд с двух концов может прийти дважды, ряды пересекаются
    kept = []
    for x, y, L in sorted(out, key=lambda r: r[2]):
        if all(np.hypot(x - kx, y - ky) >= 0.9 * min(L, kL) for kx, ky, kL in kept):
            kept.append((x, y, L))
    return [(x, y, 1.0) for x, y, _ in kept]


def _fill_zone_grid(pts, quality, spacing_m, axis, cell, phases=4):
    """Широкая зона: шахматная сетка вдоль главной оси зоны.

    Жадный отбор лучших мест давал россыпь без порядка — в сквере так не
    сажают. Шахматный порядок держит шаг до всех соседей и при том же
    шаге вмещает больше деревьев, чем квадратная сетка. Перебираются
    сдвиги сетки; берётся тот, что вмещает больше деревьев, при равенстве —
    с лучшими местами. Если сетка не легла (зона рваная), остаётся
    свободная посадка.
    """
    u = np.asarray(axis, dtype=float)
    u = u / (np.hypot(*u) or 1.0)
    v = np.array([-u[1], u[0]])
    a = pts @ u
    b = pts @ v
    # ячейка зоны -> индекс точки: принадлежность узла сетки зоне
    key = {(int(round(x / cell)), int(round(y / cell))): i
           for i, (x, y) in enumerate(pts)}
    row = spacing_m * np.sqrt(3) / 2
    best, best_score = [], (-1, -1.0)
    for pa in range(phases):
        for pb in range(phases):
            a0 = a.min() + spacing_m * pa / phases
            b0 = b.min() + row * pb / phases
            got = []
            for k, bb in enumerate(np.arange(b0, b.max() + 1e-9, row)):
                shift = spacing_m / 2 if k % 2 else 0.0
                for aa in np.arange(a0 + shift, a.max() + 1e-9, spacing_m):
                    x, y = aa * u + bb * v
                    i = key.get((int(round(x / cell)), int(round(y / cell))))
                    if i is not None:
                        # в центр принятой ячейки: узел мог лежать в соседней,
                        # запретной ячейке с тем же ключом округления, и
                        # дерево вставало за границей зоны (8 из 5538 на
                        # Олимпийской деревне)
                        got.append((float(pts[i][0]), float(pts[i][1]),
                                    float(quality[i])))
            score = (len(got), sum(g[2] for g in got))
            if score > best_score:
                best, best_score = got, score
    free = _fill_zone(pts, quality, spacing_m)
    return best if len(best) >= 0.8 * len(free) else free


def _fill_zone(pts, quality, spacing_m):
    """Свободная посадка в широкой зоне: жадный отбор с гарантией интервала.

    Ряд в таком пятне выглядел бы искусственно, поэтому точки берутся по
    убыванию пригодности места, а интервал между стволами соблюдается.
    """
    order = np.argsort(-quality)
    taken = np.empty((0, 2))
    out = []
    for idx in order:
        p = pts[idx]
        if len(taken) and np.hypot(taken[:, 0] - p[0],
                                   taken[:, 1] - p[1]).min() < spacing_m:
            continue
        out.append((float(p[0]), float(p[1]), float(quality[idx])))
        taken = np.vstack([taken, p])
    return out


def fill_remaining(cons, mask, placed, spacing_m, quality=None, count=None,
                   verbose=True):
    """Досадка оставшейся допустимой площади.

    Рядовая посадка ставит в вытянутой зоне один ряд по осевой линии.
    В узкой полосе вдоль улицы этого и нужно, но в широкой зоне — газоне
    в 30 метров, сквере, парке — две трети ширины оставались пустыми, и
    результат выглядел засаженным наполовину. Добор проходит по всей
    оставшейся площади и ставит деревья везде, где соблюдается интервал
    до уже посаженных.
    """
    if mask is None or not mask.any():
        return []
    free = mask.copy()
    r = spacing_m / cons.cell
    disc, rr = _disc(r)
    for x, y, *_ in placed:
        ix = int((x - cons.x0) / cons.cell)
        iy = int((y - cons.y0) / cons.cell)
        y0, y1 = max(0, iy - rr), min(cons.ny, iy + rr + 1)
        x0, x1 = max(0, ix - rr), min(cons.nx, ix + rr + 1)
        if y1 <= y0 or x1 <= x0:
            continue
        sub = disc[(y0 - iy + rr):(y1 - iy + rr), (x0 - ix + rr):(x1 - ix + rr)]
        free[y0:y1, x0:x1][sub] = False
    if not free.any():
        return []
    if quality is None:
        sc = ndimage.distance_transform_edt(free, sampling=cons.cell)
    else:
        sc = quality.copy()
    sc = np.where(free, np.maximum(sc, 0.01), -1.0).astype(np.float32)
    extra = pick_points(cons, sc, spacing_m, count or 10 ** 7)
    if verbose:
        print(f"      досадка оставшейся площади: +{len(extra)} деревьев",
              flush=True)
    return extra


def lawn_everywhere(cons):
    """Весь газон с чертежа, без обрезки по границе работ.

    Разбор пустот должен видеть каждый участок, который человек видит как
    газон на схеме. Раньше учитывался только газон внутри границы работ,
    и всё снаружи пропадало молча — без посадок и без объяснения.
    """
    m = np.zeros((cons.ny, cons.nx), dtype=bool)
    if getattr(cons, "lawn_mask", None) is not None:
        m |= cons.lawn_mask
    if getattr(cons, "surface", None) is not None:
        m |= cons.surface == 3
    if getattr(cons, "green_mask", None) is not None:
        m |= cons.green_mask
    if "lawn" in cons.points and not m.any():
        m |= cons._mark(cons.points["lawn"])
    return m


def explain_empty(cons, green, tree_mask, pts, spacing_m, min_area_m2=25.0,
                  kind="tree", site=None, pavement=None):
    """Разбор пустых участков газона: почему там ничего не посажено.

    Причины по порядку проверки:
      * участок за границей работ — в проект не входит;
      * участок перекрыт покрытием проекта — газон на топоплане, а по
        проекту здесь тротуар или проезд;
      * под участком сеть или рядом здание — отступ по таблице 9.1;
      * полоса слишком узкая;
      * место есть, а посадки нет — недосадка, ошибка расстановки.
    """
    if green is None or not green.any():
        return []
    lab, n = ndimage.label(green)
    if n == 0:
        return []
    occupied = np.zeros(n + 1, dtype=np.int64)
    for p in pts:
        ix = int((p[0] - cons.x0) / cons.cell)
        iy = int((p[1] - cons.y0) / cons.cell)
        if 0 <= iy < cons.ny and 0 <= ix < cons.nx:
            occupied[lab[iy, ix]] += 1
    area = ndimage.sum(np.ones_like(green, dtype=np.float32), lab,
                       index=np.arange(n + 1)) * cons.cell ** 2

    reasons = {}
    for cls, d in cons.dist.items():
        if cls in cons.SURFACE_CLASSES:
            continue
        req = cons.required(cls, kind)
        if req is None:
            continue
        reasons[cls] = d < (req + cons.margin)

    width = ndimage.distance_transform_edt(green, sampling=cons.cell) * 2.0
    out = []
    for z in range(1, n + 1):
        if occupied[z] or area[z] < min_area_m2:
            continue
        zone = lab == z
        cells = float(zone.sum())
        items = []

        if site is not None:
            outside = float((zone & ~site).sum()) / cells
            if outside > 0.5:
                items.append((outside, "за границей работ: участок не входит "
                                       "в проект благоустройства"))
        if pavement is not None:
            paved = float((zone & pavement).sum()) / cells
            if paved > 0.3:
                items.append((paved, "по проекту здесь покрытие: газон на "
                                     "топоплане заменяется тротуаром или проездом"))

        allowed = float((zone & tree_mask).sum())
        a_share = allowed / cells
        for cls, bad in reasons.items():
            share = float((zone & bad).sum()) / cells
            if share > 0.05:
                items.append((share, cons.rules[cls]["title"]))

        max_w = float(width[zone].max())
        bug = False
        inside_site = site is None or float((zone & site).sum()) / cells > 0.5
        if inside_site and a_share >= 0.1 and \
                allowed * cons.cell ** 2 >= spacing_m ** 2 * 0.5:
            bug = True
            items.insert(0, (a_share,
                             f"НЕДОСАДКА: допустимо {100*a_share:.0f}% площади, "
                             f"но деревьев нет"))
        if not items and max_w <= 3.0:
            items.append((1.0, f"полоса слишком узкая: до {max_w:.1f} м"))
        if not items and allowed > 0:
            items.append((a_share, "допустимое место меньше интервала "
                                   f"{spacing_m:g} м до соседних деревьев"))
        if not items:
            items.append((1.0, "допустимого места нет: совокупность отступов"))

        items.sort(key=lambda t: (not t[1].startswith("НЕДОСАДКА"), -t[0]))
        ys, xs = np.where(zone)
        out.append({
            "zone": int(z),
            "area_m2": round(float(area[z]), 1),
            "allowed_share": round(a_share, 3),
            "max_width_m": round(max_w, 1),
            "x": float(cons.x0 + (xs.mean() + 0.5) * cons.cell),
            "y": float(cons.y0 + (ys.mean() + 0.5) * cons.cell),
            "bbox": [float(cons.x0 + xs.min() * cons.cell),
                     float(cons.y0 + ys.min() * cons.cell),
                     float(cons.x0 + (xs.max() + 1) * cons.cell),
                     float(cons.y0 + (ys.max() + 1) * cons.cell)],
            "reasons": [{"title": t, "share": round(sh, 3)} for sh, t in items[:3]],
            "underplanted": bug,
        })
    out.sort(key=lambda r: (not r["underplanted"], -r["area_m2"]))
    return out


def coverage(cons, mask, pts, crown_m):
    """Доля допустимой зоны, накрытая проекциями крон."""
    if mask is None or not mask.any() or not pts:
        return 0.0
    cov = np.zeros_like(mask)
    disc, rr = _disc(crown_m / 2 / cons.cell)
    for p in pts:
        ix = int((p[0] - cons.x0) / cons.cell)
        iy = int((p[1] - cons.y0) / cons.cell)
        y0, y1 = max(0, iy - rr), min(cons.ny, iy + rr + 1)
        x0, x1 = max(0, ix - rr), min(cons.nx, ix + rr + 1)
        if y1 <= y0 or x1 <= x0:
            continue
        sub = disc[(y0 - iy + rr):(y1 - iy + rr), (x0 - ix + rr):(x1 - ix + rr)]
        cov[y0:y1, x0:x1] |= sub
    return float((cov & mask).sum()) / float(mask.sum())


def shrub_massifs(cons, zone, existing, spacing_m=1.2, count=20000,
                  min_width_m=1.0, verbose=True):
    """Кустарниковые массивы в зоне, где дереву нельзя, а кустарнику можно.

    Типовой случай — газон над кабелем: дереву нужно 2 м от кабеля,
    кустарнику 0,7 м. Изгородь вдоль дорожки занимает только край, а сама
    полоса над сетью оставалась пустой. Проектировщики засаживают такие
    места массивами — спиреей, пузыреплодником, кизильником.
    """
    if zone is None or not zone.any():
        return []
    # слишком узкие места не трогаем — там массив не сформируется
    wide = ndimage.distance_transform_edt(zone, sampling=cons.cell) * 2 >= min_width_m
    free = zone & wide
    if not free.any():
        return []
    disc, rr = _disc(spacing_m / cons.cell)
    for p in existing:
        ix = int((p[0] - cons.x0) / cons.cell)
        iy = int((p[1] - cons.y0) / cons.cell)
        y0, y1 = max(0, iy - rr), min(cons.ny, iy + rr + 1)
        x0, x1 = max(0, ix - rr), min(cons.nx, ix + rr + 1)
        if y1 <= y0 or x1 <= x0:
            continue
        sub = disc[(y0 - iy + rr):(y1 - iy + rr), (x0 - ix + rr):(x1 - ix + rr)]
        free[y0:y1, x0:x1][sub] = False
    if not free.any():
        return []
    sc = np.where(free, 1.0, -1.0).astype(np.float32)
    pts = pick_points(cons, sc, spacing_m, count)
    if verbose:
        print(f"      кустарниковые массивы в зоне «только кустарники»: "
              f"+{len(pts)} шт", flush=True)
    return pts


def hedge_points(cons, mask, interval_m, count, verbose=True):
    """Кустарники: изгородь вдоль линейных элементов, иначе куртины.

    Изгородь строится вдоль проезжей части, а если её в чертеже нет — вдоль
    тротуаров или по краю самой зелёной зоны. Раньше отсутствие класса
    «проезжая часть» давало ноль кустарников на весь объект, что неверно:
    живая изгородь вдоль дорожки — такое же типовое решение.
    Там, где линейной привязки нет совсем, кустарники сажаются куртинами:
    свободными группами внутри зоны, как в проектной практике.
    """
    if mask is None or not mask.any():
        return []

    for cls, label in (("road", "проезжей части"), ("walkway", "дорожек")):
        if cls not in cons.dist:
            continue
        rule = cons.rules.get(cls) or {}
        off = (rule.get("shrub") or 0.5) + cons.margin
        d = cons.dist[cls]
        band = mask & (d >= off) & (d <= off + max(0.8, 2 * cons.cell))
        if band.sum() * cons.cell ** 2 < 2.0:
            continue
        s = np.where(band, 1.0, -1.0).astype(np.float32)
        pts = pick_points(cons, s, interval_m, count)
        if pts:
            if verbose:
                print(f"      изгородь вдоль {label}: {len(pts)} шт", flush=True)
            return pts

    # линейной привязки нет — сажаем куртинами по краю зоны
    edge = mask & ~ndimage.binary_erosion(
        mask, structure=np.ones((3, 3), bool),
        iterations=max(1, int(round(1.0 / cons.cell))))
    base = edge if edge.sum() * cons.cell ** 2 > 5 else mask
    s = np.where(base, 1.0, -1.0).astype(np.float32)
    pts = pick_points(cons, s, interval_m, count)
    if verbose:
        print(f"      кустарники куртинами по краю зоны: {len(pts)} шт",
              flush=True)
    return pts


# ---------------------------------------------------------------------- #
#  Преобразование маски в прямоугольники (для DXF и веб-схемы)
# ---------------------------------------------------------------------- #

def mask_to_rects(cons, mask, downsample=4, limit=20000, reduce="any"):
    """Маска -> список прямоугольников (x0, y0, x1, y1) в метрах.

    Соседние ячейки в строке склеиваются, чтобы не плодить геометрию.
    reduce — как огрублять блок: "any" закрашивает блок, если в нём есть
    хоть одна ячейка маски; "all" — только если заполнен весь блок.
    Для запретной зоны нужен "all": иначе узкие допустимые полосы
    шириной в метр целиком тонут в красных блоках, и кажется, что посадки
    стоят в запретной зоне, хотя расчёт верен.
    """
    k = max(1, int(downsample))
    if k > 1:
        ny = (cons.ny // k) * k
        nx = (cons.nx // k) * k
        blk = mask[:ny, :nx].reshape(ny // k, k, nx // k, k)
        m = blk.all(axis=(1, 3)) if reduce == "all" else blk.any(axis=(1, 3))
    else:
        m = mask
    cell = cons.cell * k
    rects = []
    for row in range(m.shape[0]):
        line = m[row]
        if not line.any():
            continue
        d = np.diff(line.astype(np.int8))
        starts = list(np.flatnonzero(d == 1) + 1)
        ends = list(np.flatnonzero(d == -1) + 1)
        if line[0]:
            starts.insert(0, 0)
        if line[-1]:
            ends.append(len(line))
        for a, b in zip(starts, ends):
            rects.append((cons.x0 + a * cell, cons.y0 + row * cell,
                          cons.x0 + b * cell, cons.y0 + (row + 1) * cell))
            if len(rects) >= limit:
                return rects
    return rects
