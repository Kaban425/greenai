# -*- coding: utf-8 -*-
"""Подгрузка внешних ссылок (xref) чертежа.

Генплан часто — пустая рамка, а вся геометрия лежит во внешних ссылках:
топосъёмка, сети ДЖКХ, борт, заливки, граница благоустройства. Так
устроен, например, комплект eTransmit Лодочной: в главном файле 103
объекта, в 18 ссылках — всё остальное. Без подгрузки расчёт видел
только рамку и останавливался.

Файл ссылки ищется по пути из чертежа (абсолютному или относительно
чертежа), затем по имени в папке чертежа и на уровень выше — туда
eTransmit и организаторы кладут подпапки. Геометрия переносится в
координаты плана по точке вставки, масштабу и повороту. Слои получают
имена «ССЫЛКА|слой», как в AutoCAD: так их знают правила и модели.
"""
import hashlib
import math
import os

import numpy as np
import shapely

from dxf_io import XREFS, read_dxf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "cache", "xref")
MAX_DEPTH = 3
MIN_OVERLAP = 0.3          # доля объектов ссылки в габарите плана


MAX_SCAN = 30000           # файлов при поиске: не обходить весь диск


def _search_dirs(base_dir, extra_dirs=()):
    """Где искать файлы ссылок: папка чертежа и до двух уровней выше.

    Файлы подосновы кладут в «Исходные данные» рядом с «Проектные
    решения/DWG» — это два уровня вверх (Куликовская). Корень диска и
    папка самой программы не просматриваются.
    """
    out, d = [], os.path.abspath(base_dir)
    for _ in range(3):
        if (not d or os.path.dirname(d) == d
                or os.path.normcase(d) == os.path.normcase(ROOT)):
            break
        out.append(d)
        d = os.path.dirname(d)
    return out + [x for x in extra_dirs if x]


def _index(folders):
    """Имя файла (без регистра) -> пути: для поиска ссылок по имени."""
    idx, seen, n = {}, set(), 0
    for top in folders:
        if not top or not os.path.isdir(top):
            continue
        for d, _, fs in os.walk(top):
            key = os.path.normcase(d)
            if "PaxHeader" in d or key in seen:
                continue
            seen.add(key)
            n += len(fs)
            if n > MAX_SCAN:
                return idx
            for f in fs:
                if f.lower().endswith((".dwg", ".dxf")):
                    idx.setdefault(f.lower(), []).append(os.path.join(d, f))
    return idx


def resolve(xpath, base_dir, index):
    """Файл ссылки на диске или None."""
    p = xpath.replace("\\", "/")
    if not os.path.splitext(p)[1].lower() in (".dwg", ".dxf"):
        p += ".dwg"                  # «ЗАЛИВКА» — путь без расширения
    cands = [p] if os.path.isabs(p) else []
    cands.append(os.path.normpath(os.path.join(base_dir, p)))
    for c in cands:
        if os.path.isfile(c):
            return c
    name = os.path.basename(p).lower()
    for n in (name, os.path.splitext(name)[0] + "_oda.dxf"):
        hits = index.get(n)
        if hits:
            return sorted(hits, key=len)[0]      # ближайший к корню
    return None


def _affine(xr):
    """Матрица переноса координат ссылки в координаты плана."""
    sx, sy = xr["scale"]
    a = math.radians(xr["rotation"] or 0.0)
    c, s = math.cos(a), math.sin(a)
    bx, by = xr["base"]
    ix, iy = xr["insert"]
    # p' = insert + R · S · (p − base)
    m = np.array([[c * sx, -s * sy], [s * sx, c * sy]])
    t = np.array([ix, iy]) - m @ np.array([bx, by])
    return m, t


def _identity(m, t):
    return np.allclose(m, np.eye(2)) and np.allclose(t, 0)


def _centers(bl, cap=4000):
    """Центры части объектов ссылки — для проверки, ложится ли она на план."""
    geoms = [g for gs in bl.values() for g in gs[:500]][:cap]
    if not geoms:
        return np.empty((0, 2))
    b = shapely.bounds(np.asarray(geoms, dtype=object))
    b = b[np.isfinite(b).all(axis=1)]
    return np.column_stack([(b[:, 0] + b[:, 2]) / 2, (b[:, 1] + b[:, 3]) / 2])


def load(doc, path, extra_dirs=(), verbose=True, extent=None):
    """Геометрия всех внешних ссылок чертежа, уже в его координатах.

    Возвращает (by_layer, fills, texts, report): texts — подписи ссылок
    (x, y, текст) в координатах плана, report — строки {name, path, found,
    layers, objects} для отчёта и сайта.
    """
    base_dir = os.path.dirname(os.path.abspath(path))
    index = _index(_search_dirs(base_dir, extra_dirs))
    import context_classify as CC
    by_layer, fills, texts, report = {}, [], [], []
    seen = set()
    loaded = {}                   # файл -> (doc, слои, заливки, центры)

    def read(f):
        if f not in loaded:
            h = hashlib.sha1(os.path.dirname(os.path.abspath(f)).encode()).hexdigest()[:10]
            try:
                xd, bl, fl = read_dxf(f, convert_dir=os.path.join(CACHE, h))
            except (Exception, SystemExit) as e:
                if verbose:
                    print(f"        не прочитана: {str(e)[:120]}", flush=True)
                loaded[f] = None
                return None
            loaded[f] = (xd, bl, fl, _centers(bl))
        return loaded[f]

    def overlap(c, m, t):
        """Доля объектов ссылки, попавших в габарит плана."""
        if extent is None or not len(c):
            return 1.0
        q = c @ m.T + t
        x0, y0, x1, y1 = extent
        return float(((q[:, 0] >= x0) & (q[:, 0] <= x1)
                      & (q[:, 1] >= y0) & (q[:, 1] <= y1)).mean())

    def walk(d, d_path, m0, t0, depth):
        d_dir = os.path.dirname(os.path.abspath(d_path))
        groups = {}
        for xr in XREFS.get(id(d), []):
            f = resolve(xr["path"], d_dir, index)
            if f is None:
                report.append({"name": xr["name"], "path": xr["path"],
                               "found": None, "layers": 0, "objects": 0})
                if verbose:
                    print(f"        нет файла ссылки: {xr['path']}", flush=True)
                continue
            groups.setdefault(os.path.normcase(os.path.abspath(f)), (f, []))[1].append(xr)
        for f, xrs in groups.values():
            got = read(f)
            name = xrs[0]["name"]
            row = {"name": name, "path": xrs[0]["path"], "found": f,
                   "layers": 0, "objects": 0}
            report.append(row)
            if got is None:
                row["found"] = None
                continue
            xd, bl, fl, cen = got
            # Одна ссылка бывает вставлена несколько раз (листы, фрагменты).
            # Берётся вставка, чья геометрия ложится на план; если ни одна
            # не ложится — ссылка в другой системе координат, её пропускаем.
            best = None
            for xr in xrs:
                m1, t1 = _affine(xr)
                m, t = m0 @ m1, m0 @ t1 + t0            # вложенная ссылка
                r = overlap(cen, m, t)
                if best is None or r > best[0]:
                    best = (r, m, t)
            r, m, t = best
            if r < MIN_OVERLAP:
                row["found"] = None
                row["skipped"] = f"не ложится на план ({100 * r:.0f}%)"
                if verbose:
                    print(f"        ссылка {name} не ложится на план "
                          f"(совпадение {100 * r:.0f}%) — пропущена", flush=True)
                continue
            key = (os.path.normcase(os.path.abspath(f)),
                   tuple(np.round(m, 6).ravel()), tuple(np.round(t, 3)))
            if key in seen:
                continue
            seen.add(key)
            if verbose:
                print(f"      + ссылка {name} ← {os.path.basename(f)}"
                      + (f" (вставок {len(xrs)}, взята лучшая)" if len(xrs) > 1 else ""),
                      flush=True)
            ident = _identity(m, t)

            def tr(coords, m=m, t=t):
                return coords @ m.T + t

            for lay, geoms in bl.items():
                key_l = f"{name}|{lay}"
                gs = geoms if ident else list(shapely.transform(
                    np.asarray(geoms, dtype=object), tr))
                by_layer.setdefault(key_l, []).extend(gs)
                row["objects"] += len(gs)
            row["layers"] = len(bl)
            fills.extend((f"{name}|{lay}", rings if ident else [tr(r) for r in rings])
                         for lay, rings in fl)
            for x, y, txt in CC.collect_texts(xd):
                x2, y2 = tr(np.array([[x, y]]))[0]
                texts.append((float(x2), float(y2), txt))
            if depth < MAX_DEPTH:
                walk(xd, f, m, t, depth + 1)

    walk(doc, path, np.eye(2), np.zeros(2), 1)
    return by_layer, fills, texts, report
