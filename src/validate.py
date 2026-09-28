# -*- coding: utf-8 -*-
"""Сверка сгенерированного плана с эталонным решением проектировщиков.

Эталон — слои проектных посадок из того же чертежа. Важно, что деревья и
кустарники проверяются по своим нормам: кустарнику у кромки проезжей части
достаточно 1 м, дереву нужно 2 м, и смешивать их нельзя.
"""
import math
import re

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree
from scipy.optimize import linear_sum_assignment
from shapely.geometry import Polygon

# По имени слоя понятно, что за посадка: кустарник, изгородь или дерево.
# Дендропланы пишут породы сокращённо («Горт», «СпирВАНГУТА», «Пузыр»,
# «ЧЕРЕМ»), поэтому корни короткие. Кустарники проверяются первыми:
# «рябинник» — не рябина, «сосна горная» — стелющийся кустарник.
SHRUB_RE = re.compile(r"кустарн|изгород|кизильн|спир|пузыр|роз[аы]|"
                      r"снежноягод|можжевельн|форзиц|дерен|барбарис|"
                      r"чубушник|горт|лапчатк|скумпи|вейгел|бузин|"
                      r"рябинник|вейник|мискантус|овсяниц|злак|сирен|"
                      r"берескл|лещин|жимолост|туя|сосна[\W_]*горн|калин|"
                      r"plnt[_ ]*bush|_bush_|\bpl[_ ]bush|кусты", re.I)
TREE_RE = re.compile(r"липа|клен|клён|дуб|берез|берёз|рябин|ябл|черем|"
                     r"черём|тополь|сосна|\bель\b|_ель_|пихт|лиственниц|"
                     r"псевдотцуг|каштан|вяз|ясен|боярышн|\bив[аы]\b|кедр|"
                     r"вишн|слив|груш|абрикос|plnt[_ ]*tree|_tree_|\bpl[_ ]tree|"
                     r"дерев", re.I)
# Общие слои посадочных мест: вид растения по ним не понять. Если в
# чертеже есть слои с породами, это дубли их знаков и в эталон не идут.
PLACE_RE = re.compile(r"^!посадки|посадочн\w*\s+мест", re.I)

# Слои «Растения фр1», «Растения 2-2» из чертежей, переведённых из PDF:
# породы в имени нет, она только в ведомости. Вид растения виден по
# размеру знака: деревья нарисованы кругами 4–6 м, кустарники — 1–3 м.
# Проверено по ведомостям: Понтрягина — 23 дерева и 23 круга 4 м,
# 81 сирень и 81 круг 2,5 м; Куликовская — 49 деревьев кругами 6 м.
GENERIC_RE = re.compile(r"^(новый_)?растения\b", re.I)
TREE_SYMBOL_M = 3.8
MIN_SYMBOL_M = 0.6        # внутренние метки ствола 0,2–0,5 м — не растения

DEDUP_M = {"tree": 1.5, "shrub": 0.3}


# Одиночный знак растения — компактный, близкий к кругу контур небольшого
# размера. Контур массива посадок вытянут и велик: его центр лежит где угодно,
# в том числе поверх сети, и точкой посадки считаться не может.
MAX_SYMBOL_R = 3.5       # круг кроны 6 м из PDF — радиус ровно 3 м
MIN_CIRCULARITY = 0.55


def _is_symbol(g):
    """Знак растения — замкнутый компактный контур небольшого размера.

    Незамкнутые штрихи (куски пунктира, штриховка массивов, перекрестия)
    знаками не являются: раньше они шли в эталон и завышали его.
    """
    try:
        a, p = g.area, g.length
        if a <= 0 or p <= 0:
            return False
        if math.sqrt(a / math.pi) > MAX_SYMBOL_R:
            return False
        return (4 * math.pi * a / (p * p)) >= MIN_CIRCULARITY
    except Exception:
        return False


def _dedup(arr, tol):
    """Убирает дубли ближе tol. Округление к сетке пропускало пары,
    попавшие по разные стороны границы ячейки."""
    if len(arr) < 2:
        return arr
    keep = np.ones(len(arr), dtype=bool)
    for i, j in sorted(cKDTree(arr).query_pairs(tol)):
        if keep[i] and keep[j]:
            keep[j] = False
    return arr[keep]


def _points(geoms, tol=1.5, symbols_only=True):
    """Условные знаки -> точки посадок без дублей."""
    pts = []
    skipped = 0
    for g in geoms:
        if g is None or g.is_empty:
            continue
        if symbols_only and not _is_symbol(g):
            skipped += 1
            continue
        try:
            c = g.centroid
            if not c.is_empty:
                pts.append((c.x, c.y))
        except Exception:
            continue
    _points.skipped = skipped
    if not pts:
        return np.empty((0, 2))
    return _dedup(np.array(pts), tol)


def collect_reference(by_layer, layer_class):
    """Точки эталона, разделённые на деревья и кустарники."""
    groups = {"tree": [], "shrub": []}
    used = {"tree": [], "shrub": []}
    places = []
    for layer, geoms in by_layer.items():
        if layer_class.get(layer) != "reference_planting":
            continue
        if PLACE_RE.search(layer):
            places.append(layer)
            continue
        if SHRUB_RE.search(layer):
            kind = "shrub"
        elif TREE_RE.search(layer):
            kind = "tree"
        elif GENERIC_RE.search(layer):
            for g in geoms:
                try:
                    b = g.bounds
                    d = max(b[2] - b[0], b[3] - b[1])
                except Exception:
                    continue
                if d < MIN_SYMBOL_M:
                    continue
                if g.geom_type == "LineString" and g.is_closed and len(g.coords) >= 4:
                    g = Polygon(g.coords)    # из PDF круг приходит линией
                groups["tree" if d >= TREE_SYMBOL_M else "shrub"].append(g)
            for k in ("tree", "shrub"):
                if layer not in used[k]:
                    used[k].append(layer)
            continue
        else:
            continue          # служебные слои композиции и отступов пропускаем
        groups[kind].extend(geoms)
        used[kind].append(layer)
    out, skipped = {}, {}
    for k, v in groups.items():
        out[k] = _points(v, tol=DEDUP_M.get(k, 1.0))
        skipped[k] = getattr(_points, "skipped", 0)
    # Посадочные места: там, где рядом нет знака породы, это деревья без
    # подписи породы; рядом со знаком — дубль того же растения. Раньше места
    # брались, только если пород в чертеже не было совсем, и на Наташинском
    # от 242 деревьев оставалось одно: у него один слой породы на всех.
    if places:
        pl = _points([g for l in places for g in by_layer[l]],
                     tol=DEDUP_M["tree"])
        known = [a for a in (out["tree"], out["shrub"]) if len(a)]
        if len(pl) and known:
            d, _ = cKDTree(np.vstack(known)).query(pl)
            pl = pl[d > 2.0]
        if len(pl):
            out["tree"] = (_dedup(np.vstack([out["tree"], pl]), DEDUP_M["tree"])
                           if len(out["tree"]) else pl)
            used["tree"].extend(places)
    return out, used, skipped


def distance_stats(cons, ref, kind):
    """Фактические расстояния от эталонных посадок до каждого класса.

    Нужно, чтобы отличить ошибку геометрии от реального отступления от норм:
    если медиана близка к нулю, скорее всего слой распознан неверно; если
    она сопоставима с нормой, проект действительно посажен плотнее.
    """
    if len(ref) == 0:
        return []
    ix = ((ref[:, 0] - cons.x0) / cons.cell).astype(int)
    iy = ((ref[:, 1] - cons.y0) / cons.cell).astype(int)
    ok = (ix >= 0) & (ix < cons.nx) & (iy >= 0) & (iy < cons.ny)
    ix, iy = ix[ok], iy[ok]
    rows = []
    for cls, d in cons.dist.items():
        if cls in cons.SURFACE_CLASSES:
            continue
        req = cons.required(cls, kind)
        if req is None:
            continue
        vals = d[iy, ix]
        # Статистика — по полной выборке. values нужны только для гистограммы,
        # поэтому длинный хвост из них убираем, а медиану и долю ниже нормы
        # берём из полных данных, иначе цифры на графике разойдутся с отчётом.
        hi_plot = max(req * 3.0, float(np.percentile(vals, 90)), 1.0)
        keep = vals[vals <= hi_plot]
        rows.append({
            "values": [round(float(v), 2) for v in keep[:4000]],
            "plot_hi": round(float(hi_plot), 2),
            "tail_pct": round(100.0 * float((vals > hi_plot).mean()), 1),
            "count": int(len(vals)),
            "class": cls,
            "title": cons.rules[cls]["title"],
            "required_m": req,
            "p05": round(float(np.percentile(vals, 5)), 2),
            "median": round(float(np.median(vals)), 2),
            "p95": round(float(np.percentile(vals, 95)), 2),
            "below_norm_pct": round(100.0 * float((vals < req).mean()), 1),
        })
    rows.sort(key=lambda r: -r["below_norm_pct"])
    return rows


def match_metrics(cons, ref, ours, radius=3.0):
    """Честное сопоставление: каждая посадка участвует в одной паре.

    Медиана расстояния до ближайшей посадки выглядит хорошо, даже если все
    наши деревья стоят кучей, поэтому пары ищутся венгерским алгоритмом.
    Дополнительно сравнивается число деревьев на каждом отдельном газоне:
    эта метрика устойчива к сдвигу на пару метров и показывает, совпал ли
    замысел, а не координаты.
    """
    if not len(ref) or not len(ours):
        return None
    D = np.hypot(ref[:, None, 0] - ours[None, :, 0],
                 ref[:, None, 1] - ours[None, :, 1])
    r, c = linear_sum_assignment(D)
    ok = D[r, c] <= radius
    tp = int(ok.sum())

    lab, n = ndimage.label(cons.green_mask)

    def per_zone(p):
        ix = np.clip(((p[:, 0] - cons.x0) / cons.cell).astype(int), 0, cons.nx - 1)
        iy = np.clip(((p[:, 1] - cons.y0) / cons.cell).astype(int), 0, cons.ny - 1)
        return np.bincount(lab[iy, ix], minlength=n + 1)[1:]

    a, b = per_zone(ref), per_zone(ours)
    z = (a + b) > 0
    return {
        "radius_m": radius,
        "matched": tp,
        "precision_pct": round(100.0 * tp / len(ours), 1),
        "recall_pct": round(100.0 * tp / len(ref), 1),
        "mean_offset_m": round(float(D[r, c][ok].mean()), 2) if tp else None,
        "zones": int(z.sum()),
        "zone_count_mae": round(float(np.abs(a[z] - b[z]).mean()), 2) if z.any() else None,
        "zones_exact_pct": round(100.0 * float((a[z] == b[z]).mean()), 1) if z.any() else None,
    }


def _eval_one(cons, mask, ref, ours, kind, tolerance=0.0):
    """Строгая оценка и оценка с допуском.

    Допуск нужен, потому что расстояния меряются по растровой сетке и по
    геометрии, восстановленной из чертежа: точка, стоящая ровно на норме,
    может оказаться на полклетки ближе. Без допуска такие посадки
    засчитываются как нарушение, хотя в проекте они корректны.
    """
    if len(ref) == 0:
        return None
    inside = 0
    inside_tol = 0
    on_surface = 0
    viol = {}
    for x, y in ref:
        ix = int((x - cons.x0) / cons.cell)
        iy = int((y - cons.y0) / cons.cell)
        if not (0 <= ix < cons.nx and 0 <= iy < cons.ny):
            continue
        surf = bool(cons.green_mask[iy, ix])
        if surf:
            on_surface += 1
        if mask[iy, ix]:
            inside += 1
            inside_tol += 1
            continue
        checks = cons.explain_xy(x, y, kind)
        if surf and checks and all(
                c["actual_m"] >= c["required_m"] - tolerance for c in checks):
            inside_tol += 1
        for c in checks:
            if not c["ok"]:
                viol[c["title"]] = viol.get(c["title"], 0) + 1

    dist = None
    if len(ours):
        d = np.hypot(ref[:, None, 0] - ours[None, :, 0],
                     ref[:, None, 1] - ours[None, :, 1])
        near = d.min(axis=1)
        dist = {"median_m": round(float(np.median(near)), 1),
                "within_10m_pct": round(100.0 * float((near <= 10).mean()), 1)}
    return {
        "count": int(len(ref)),
        "distance_stats": distance_stats(cons, ref, kind),
        "inside_allowed": int(inside),
        "inside_pct": round(100.0 * inside / len(ref), 1),
        "inside_tol": int(inside_tol),
        "inside_tol_pct": round(100.0 * inside_tol / len(ref), 1),
        "on_surface": int(on_surface),
        "on_surface_pct": round(100.0 * on_surface / len(ref), 1),
        "tolerance_m": tolerance,
        "match": match_metrics(cons, ref, ours) if kind == "tree" else None,
        "violations": sorted(viol.items(), key=lambda t: -t[1]),
        "distance_to_ours": dist,
    }


def evaluate(cons, masks, ref_pts, our_items, tolerance=0.0):
    """masks = {'tree': маска, 'shrub': маска}."""
    our = {"tree": [], "shrub": []}
    for it in our_items:
        our["tree" if it["type"] == "дерево" else "shrub"].append((it["x"], it["y"]))
    out = {}
    for kind in ("tree", "shrub"):
        arr = np.array(our[kind]) if our[kind] else np.empty((0, 2))
        out[kind] = _eval_one(cons, masks[kind],
                              ref_pts.get(kind, np.empty((0, 2))), arr, kind,
                              tolerance=tolerance)
    return out if any(out.values()) else None


RU = {"tree": "деревья", "shrub": "кустарники"}


def report_lines(res):
    if not res:
        return ["Эталонные посадки в чертеже не распознаны."]
    out = []
    for kind in ("tree", "shrub"):
        r = res.get(kind)
        if not r:
            continue
        out.append(f"{RU[kind].capitalize()}: эталонных {r['count']}")
        out.append(f"    в зелёной зоне (не на покрытии): {r['on_surface']} "
                   f"({r['on_surface_pct']}%)")
        out.append(f"    проходит строго по норме: {r['inside_allowed']} "
                   f"({r['inside_pct']}%)")
        out.append(f"    проходит с допуском {r['tolerance_m']} м: "
                   f"{r['inside_tol']} ({r['inside_tol_pct']}%)")
        for title, n in r["violations"][:4]:
            out.append(f"    не проходит по: {title} — {n}")
        d = r["distance_to_ours"]
        if d:
            out.append(f"    до ближайшей нашей посадки: медиана {d['median_m']} м, "
                       f"в пределах 10 м — {d['within_10m_pct']}%")
        m = r.get("match")
        if m:
            out.append(f"    совпало пар в радиусе {m['radius_m']} м: {m['matched']} — "
                       f"точность {m['precision_pct']}%, полнота {m['recall_pct']}%"
                       + (f", средний сдвиг {m['mean_offset_m']} м"
                          if m['mean_offset_m'] else ""))
            if m.get("zone_count_mae") is not None:
                out.append(f"    по отдельным газонам ({m['zones']} шт): "
                           f"ошибка числа деревьев {m['zone_count_mae']}, "
                           f"точное совпадение {m['zones_exact_pct']}%")
        st = r.get("distance_stats") or []
        if st:
            out.append("    фактические отступы эталона "
                       "(норма / 5% / медиана / ниже нормы):")
            for x in st[:6]:
                out.append(f"      {x['title'][:38]:<40} "
                           f"{x['required_m']:>4} / {x['p05']:>5} / "
                           f"{x['median']:>6} / {x['below_norm_pct']:>5.1f}%")
    return out or ["Эталонные посадки в чертеже не распознаны."]
