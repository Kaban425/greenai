# -*- coding: utf-8 -*-
"""Модель выбора места посадки, обученная на решениях проектировщиков.

Раньше пригодность места считалась по весам, подобранным вручную: корневой
объём 40%, свет 25% и так далее. Здесь веса берутся из данных. В чертежах
с эталоном (слои проектных посадок) известно, где живой проектировщик
посадил дерево. Модель сравнивает условия в этих точках с условиями в
случайных точках той же зелёной зоны, где посадки нет, и учится отличать
одно от другого.

Модель — логистическая регрессия. Выбрана сознательно:
  * работает на сотнях примеров, а больше в одном чертеже и нет;
  * её веса читаются напрямую: видно, какие условия проектировщик считает
    важными и в какую сторону. Для экспертизы это обязательное требование,
    «чёрный ящик» ТЗ не принимает;
  * не требует внешних библиотек — только numpy.

Качество проверяется на отложенной части выборки по ROC AUC: 0,5 — модель
угадывает не лучше монетки, 1,0 — безошибочно отличает места посадки.

    python src/main.py --input улица.dxf --train      обучить и сохранить
    python src/main.py --input другая.dxf --learned   применить
"""
import json
import os

import numpy as np

# Признаки места. Расстояния обрезаются на 15 м: дальше влияние объекта
# на решение проектировщика уже не зависит от расстояния.
FEATURES = [
    ("road",          "до проезжей части"),
    ("walkway",       "до тротуара, дорожки"),
    ("building",      "до здания"),
    ("power_cable",   "до кабеля"),
    ("water",         "до водопровода, водостока"),
    ("sewer",         "до канализации"),
    ("gas",           "до газопровода"),
    ("heating",       "до теплосети"),
    ("existing_tree", "до существующего дерева"),
    ("lighting_pole", "до опоры освещения"),
    ("insolation",    "освещённость"),
    ("root_space",    "до ближайшего покрытия (корневой объём)"),
    ("strip_width",   "ширина зелёной полосы"),
    ("strip_local",   "ширина полосы в окрестности"),
    ("strip_center",  "близость к оси полосы"),
]
# Окно, в котором ищется ось полосы: шире самых широких газонов улиц.
AXIS_WINDOW_M = 12.0
CLIP_M = 15.0
# Пороги, после которых признак «насыщается». Проектировщик думает не
# «чем дальше от дороги, тем лучше», а «не ближе 1,5 м, а дальше уже всё
# равно». Прямая линия этого не передаёт, поэтому к каждому признаку
# добавляются min(x, 1,5), min(x, 3) и min(x, 6): модель остаётся
# логистической регрессией, но зависимость становится ломаной.
# Проверка «без одной улицы»: 0,776 → 0,835, Камчатская 0,65 → 0,81.
HINGES = (1.5, 3.0, 6.0)


def expand(X, hinges=HINGES):
    """Исходные признаки плюс ломаные: (..., F) → (..., F·(1+len(hinges)))."""
    if not hinges:
        return X
    cols = [X] + [np.minimum(X, t) for t in hinges]
    return np.concatenate(cols, axis=-1)


def importance(w, Z, X, n_base):
    """Вклад каждого исходного признака вместе с его ломаными.

    Величина — разброс вклада в оценку места на обучающих данных, знак —
    растёт ли вклад с ростом признака. Так таблица «чему научилась
    модель» остаётся читаемой: одна строка на признак, как раньше.
    """
    out = []
    for k in range(n_base):
        idx = list(range(k, Z.shape[1], n_base))
        contrib = Z[:, idx] @ w[idx]
        spread = float(contrib.std())
        if spread < 0.05:
            continue
        c = np.corrcoef(contrib, X[:, k])[0, 1] if X[:, k].std() > 0 else 0.0
        out.append({"feature": FEATURES[k][1],
                    "weight": round(spread * (1 if c >= 0 else -1), 3)})
    return sorted(out, key=lambda r: -abs(r["weight"]))


def feature_grid(cons, env, green):
    """Признаки для каждой ячейки сетки: массив (ny, nx, число признаков)."""
    from scipy import ndimage
    ny, nx = cons.ny, cons.nx
    out = np.zeros((ny, nx, len(FEATURES)), dtype=np.float32)
    edt = ndimage.distance_transform_edt(green, sampling=cons.cell)
    width = edt * 2.0
    # Проектировщик ставит дерево на ось газона. Ширина в точке этого не
    # видит: у края она мала и в узкой, и в широкой полосе. Поэтому берётся
    # наибольшее расстояние до края в окрестности (половина ширины полосы)
    # и отношение к нему: 1 — точка на оси, 0 — у самого края.
    size = max(3, int(AXIS_WINDOW_M / cons.cell) | 1)
    half = ndimage.maximum_filter(edt, size=size)
    for k, (name, _) in enumerate(FEATURES):
        if name == "insolation":
            out[..., k] = env.get("insolation", np.ones((ny, nx)))
        elif name == "root_space":
            out[..., k] = np.minimum(env.get("root_space",
                                             np.full((ny, nx), CLIP_M)), CLIP_M)
        elif name == "strip_width":
            out[..., k] = np.minimum(width, 2 * CLIP_M)
        elif name == "strip_local":
            out[..., k] = np.minimum(half * 2.0, 2 * CLIP_M)
        elif name == "strip_center":
            out[..., k] = np.where(half > 0, edt / np.maximum(half, 1e-6), 0.0)
        elif name in cons.dist:
            out[..., k] = np.minimum(cons.dist[name], CLIP_M)
        else:
            out[..., k] = CLIP_M          # объекта нет — как будто он далеко
    return out


def _sample(cons, grid, green, pts, neg_ratio=3, min_gap_m=3.0, seed=0):
    """Положительные примеры — места посадок эталона, отрицательные —
    случайные ячейки той же зелёной зоны вдали от эталонных посадок."""
    rng = np.random.default_rng(seed)
    ix = ((pts[:, 0] - cons.x0) / cons.cell).astype(int)
    iy = ((pts[:, 1] - cons.y0) / cons.cell).astype(int)
    ok = (ix >= 0) & (ix < cons.nx) & (iy >= 0) & (iy < cons.ny)
    ix, iy = ix[ok], iy[ok]
    ok = green[iy, ix]
    ix, iy = ix[ok], iy[ok]
    if len(ix) < 10:
        return None, None

    from scipy import ndimage
    near = np.zeros_like(green)
    near[iy, ix] = True
    r = max(1, int(min_gap_m / cons.cell))
    near = ndimage.binary_dilation(near, iterations=r)
    cand = np.argwhere(green & ~near)
    if len(cand) == 0:
        return None, None
    n_neg = min(len(cand), neg_ratio * len(ix))
    pick = cand[rng.choice(len(cand), n_neg, replace=False)]

    X = np.vstack([grid[iy, ix], grid[pick[:, 0], pick[:, 1]]])
    y = np.concatenate([np.ones(len(ix)), np.zeros(n_neg)])
    return X, y


def _auc(y, p):
    """ROC AUC через ранги: доля пар, где посадка оценена выше пустого места."""
    order = np.argsort(p)
    ranks = np.empty(len(p))
    ranks[order] = np.arange(1, len(p) + 1)
    pos = y == 1
    n_pos, n_neg = pos.sum(), (~pos).sum()
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def _fit(X, y, l2=1.0, iters=600, lr=0.1):
    """Логистическая регрессия градиентным спуском с регуляризацией."""
    w = np.zeros(X.shape[1])
    b = 0.0
    n = len(y)
    for _ in range(iters):
        z = np.clip(X @ w + b, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        g = p - y
        w -= lr * (X.T @ g / n + l2 * w / n)
        b -= lr * g.mean()
    return w, b


def train(cons, env, green, ref_pts, verbose=True, seed=0):
    X, y = _sample(cons, feature_grid(cons, env, green), green, ref_pts,
                   seed=seed)
    if X is None:
        if verbose:
            print("      обучение: мало эталонных посадок в зелёной зоне",
                  flush=True)
        return None

    raw = X
    X = expand(raw)
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-6
    Z = (X - mu) / sd

    # отложенная выборка для честной оценки качества
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(y))
    cut = int(len(y) * 0.75)
    tr, te = idx[:cut], idx[cut:]
    w, b = _fit(Z[tr], y[tr])
    auc_test = _auc(y[te], Z[te] @ w + b)

    # итоговая модель — на всех данных
    w, b = _fit(Z, y)
    ranked = importance(w, Z, raw, len(FEATURES))
    model = {
        "importance": ranked, "hinges": list(HINGES),
        "features": [f for f, _ in FEATURES],
        "labels": [l for _, l in FEATURES],
        "mean": mu.tolist(), "std": sd.tolist(),
        "weights": w.tolist(), "bias": float(b),
        "auc_holdout": auc_test,
        "positives": int(y.sum()), "negatives": int((1 - y).sum()),
    }
    if verbose:
        print(f"      обучение: {model['positives']} посадок эталона против "
              f"{model['negatives']} пустых мест, "
              f"качество на отложенной выборке AUC = {auc_test:.3f}",
              flush=True)
        print("      что важно проектировщику (знак — в какую сторону):",
              flush=True)
        if not ranked:
            print("        значимых признаков не найдено", flush=True)
        for r in ranked[:6]:
            sign = "дальше или больше лучше" if r["weight"] > 0 else "ближе или меньше лучше"
            print(f"        {r['feature']:<40} {r['weight']:+.2f}  ({sign})",
                  flush=True)
    return model


def save(model, root, name="placement_model.json"):
    path = os.path.join(root, "config", name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=1)
    return path


def load(root):
    path = os.path.join(root, "config", "placement_model.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def score(model, cons, env, green):
    """Пригодность каждой ячейки по обученной модели, от 0 до 1."""
    grid = feature_grid(cons, env, green)
    names = [f for f, _ in FEATURES]
    want = model.get("features", names)
    if want != names:                     # модель обучена на другом наборе
        grid = grid[..., [names.index(f) for f in want]]
    mu = np.array(model["mean"], dtype=np.float32)
    sd = np.array(model["std"], dtype=np.float32)
    w = np.array(model["weights"], dtype=np.float32)
    hinges = tuple(model.get("hinges", ()))   # старые модели — без ломаных
    z = np.empty(grid.shape[:2], dtype=np.float32)
    step = 256                                # по полосам: расширенная сетка велика
    for r in range(0, grid.shape[0], step):
        z[r:r + step] = ((expand(grid[r:r + step], hinges) - mu) / sd) @ w
    z += model["bias"]
    p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
    return p.astype(np.float32)
