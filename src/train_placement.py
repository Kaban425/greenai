# -*- coding: utf-8 -*-
"""Модель выбора места, обученная на посадочных планах многих улиц.

    python src/train_placement.py "<папка «Пилотный проект 20 улиц»>"
    python src/train_placement.py <папка> --only-collect   только собрать примеры

Раньше модель училась на эталоне одного чертежа (--train в main.py) и
знала привычки одного проектировщика. Здесь из каждой улицы берётся
посадочный план, точки деревьев проектировщика сравниваются со случайными
местами той же зелёной зоны, и одна модель учится на всех улицах сразу.

Проверка — «без одной улицы»: модель учится на всех, кроме одной, и
оценивается на ней (ROC AUC: 0,5 — как монетка, 1,0 — безошибочно). Это
честная оценка того, как модель поведёт себя на новой улице.

Нормы строгие (сценарий strict): расчёт сажает только в строго
допустимой зоне, модель лишь ранжирует места внутри неё.
Примеры по улицам кешируются в cache/placement/: повторный запуск
пересчитывает только новые чертежи.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "cache", "placement")
PLAN_RE = re.compile(r"посадоч|рч\s*посад|разбивочн\w*.{0,3}посадоч|"
                     r"озеленени|рч\s*озелен", re.IGNORECASE)
MIN_TREES = 20


def fit_weighted(X, y, sw, l2=30.0, iters=1500, lr=0.1):
    """Логистическая регрессия с весами примеров.

    Каждая улица весит одинаково: Олимпийская деревня дала больше половины
    всех посадок, и без весов модель учила её привычки вместо общих —
    на Харьковской AUC падал ниже монетки.
    """
    w = np.zeros(X.shape[1])
    b = 0.0
    sw = sw / sw.sum()
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(X @ w + b, -30, 30)))
        g = (p - y) * sw
        w -= lr * (X.T @ g + l2 * w / len(y))
        b -= lr * g.sum()
    return w, b


def find_plans(folder):
    """Посадочные планы проектировщиков: по одному-два на улицу."""
    out = []
    for d, _, fs in os.walk(folder):
        if "PaxHeader" in d:
            continue
        for f in fs:
            p = os.path.join(d, f)
            if (f.lower().endswith((".dwg", ".dxf")) and PLAN_RE.search(f)
                    and os.path.getsize(p) > 1_000_000):
                out.append(p)
    return sorted(out)


def street_of(path, folder):
    return os.path.relpath(path, folder).split(os.sep)[0]


def collect(path, norms):
    """Признаки и метки одного посадочного плана или None."""
    import learn
    import preview
    import raster_engine as R
    import validate
    import geo_classify
    from layers import NON_OBSTACLE, PERMISSIVE
    by_layer, fills = preview.load(path, convert_dir=os.path.join(CACHE, "dxf"))
    cls, _ = preview.classify(by_layer, {})
    for k, v in geo_classify.classify_unknown(by_layer, cls).items():
        if v not in PERMISSIVE:
            cls[k] = v
    ref, used, _ = validate.collect_reference(by_layer, cls)
    trees = ref.get("tree")
    if trees is None or len(trees) < MIN_TREES:
        return None, f"деревьев эталона {0 if trees is None else len(trees)}"
    cons = R.RasterConstraints(by_layer, norms, cls, non_obstacle=NON_OBSTACLE,
                               verbose=False, fills=fills)
    env = R.env_maps(cons, verbose=False)
    grid = learn.feature_grid(cons, env, cons.green_mask)
    X, y = learn._sample(cons, grid, cons.green_mask, trees)
    if X is None:
        return None, "эталон вне зелёной зоны"
    return {"X": X, "y": y, "trees": len(trees),
            "in_green": int(y.sum()), "layers": used["tree"][:6]}, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--only-collect", action="store_true")
    ap.add_argument("--exclude", action="append", default=[],
                    help="часть имени улицы, которую не брать в обучение: "
                         "территория другого типа (двор, парк) учит модель "
                         "не тому, что делают на улицах")
    args = ap.parse_args()

    import yaml
    import learn
    with open(os.path.join(ROOT, "config", "norms.yaml"), encoding="utf-8") as f:
        norms = yaml.safe_load(f)
    os.makedirs(CACHE, exist_ok=True)

    plans = find_plans(args.folder)
    print(f"Посадочных планов: {len(plans)}", flush=True)
    data = {}
    for p in plans:
        street = street_of(p, args.folder)
        st = os.stat(p)
        key = hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()
        cp = os.path.join(CACHE, key + ".npz")
        t0 = time.time()
        if os.path.exists(cp):
            d = np.load(cp, allow_pickle=True)
            got = {k: d[k] for k in d.files}
            got["meta"] = json.loads(str(got["meta"]))
        else:
            try:
                res, why = collect(p, norms)
            except Exception as e:
                res, why = None, f"ошибка: {type(e).__name__}: {e}"
            if res is None:
                print(f"  пропуск  {street[:30]:<30} {os.path.basename(p)[:45]} — {why}",
                      flush=True)
                continue
            meta = {"street": street, "file": os.path.basename(p),
                    "trees": res["trees"], "in_green": res["in_green"],
                    "layers": res["layers"]}
            np.savez_compressed(cp, X=res["X"], y=res["y"],
                                meta=json.dumps(meta, ensure_ascii=False))
            got = {"X": res["X"], "y": res["y"], "meta": meta}
        m = got["meta"]
        # на улице бывает несколько планов (дуги, линейный) — примеры суммируются
        d = data.setdefault(street, {"X": [], "y": [], "trees": 0, "files": []})
        d["X"].append(got["X"])
        d["y"].append(got["y"])
        d["trees"] += m["in_green"]
        d["files"].append(m["file"])
        print(f"  {street[:30]:<30} {m['file'][:45]:<45} деревьев {m['trees']:>4}, "
              f"в зелёной зоне {m['in_green']:>4}  ({time.time()-t0:.0f} с)",
              flush=True)
    if args.only_collect or len(data) < 3:
        print(f"Улиц с эталоном: {len(data)}" +
              ("" if len(data) >= 3 else " — мало для обучения"))
        return 0

    dropped = [s for s in data if any(e.lower() in s.lower() for e in args.exclude)]
    for s in dropped:
        print(f"Исключена из обучения: {s}")
        data.pop(s)
    streets = sorted(data)
    RAW = {s: np.vstack(data[s]["X"]) for s in streets}
    XS = {s: learn.expand(RAW[s]) for s in streets}
    YS = {s: np.concatenate(data[s]["y"]) for s in streets}
    X = np.vstack([XS[s] for s in streets])
    y = np.concatenate([YS[s] for s in streets])
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-6

    print("\nПроверка «без одной улицы» (AUC на улице, которую модель не видела):")
    loso = {}
    for s in streets:
        tr = [t for t in streets if t != s]
        Xtr = (np.vstack([XS[t] for t in tr]) - mu) / sd
        ytr = np.concatenate([YS[t] for t in tr])
        w, b = fit_weighted(Xtr, ytr, np.concatenate(
            [np.full(len(YS[t]), 1.0 / len(YS[t])) for t in tr]))
        auc = learn._auc(YS[s], ((XS[s] - mu) / sd) @ w + b)
        loso[s] = round(float(auc), 3)
        print(f"  {s[:40]:<40} посадок {int(YS[s].sum()):>4}  AUC {auc:.3f}")
    mean_auc = float(np.mean(list(loso.values())))
    print(f"  среднее: {mean_auc:.3f}")

    Z = (X - mu) / sd
    w, b = fit_weighted(Z, y, np.concatenate(
        [np.full(len(YS[s]), 1.0 / len(YS[s])) for s in streets]))
    F = learn.FEATURES
    ranked = learn.importance(w, Z, np.vstack([RAW[s] for s in streets]), len(F))
    model = {
        "importance": ranked, "hinges": list(learn.HINGES),
        "features": [f for f, _ in F],
        "labels": [l for _, l in F], "mean": mu.tolist(), "std": sd.tolist(),
        "weights": w.tolist(), "bias": float(b),
        "auc_holdout": mean_auc, "auc_by_street": loso,
        "positives": int(y.sum()), "negatives": int((1 - y).sum()),
        "streets": {s: data[s]["files"] for s in streets},
        "trained": time.strftime("%d.%m.%Y %H:%M"),
        "excluded": dropped,
    }
    old = os.path.join(ROOT, "config", "placement_model.json")
    if os.path.exists(old):
        shutil.copy(old, old + ".bak")
    learn.save(model, ROOT)
    print("\nЧто важно проектировщикам (знак — в какую сторону):")
    for r in ranked[:8]:
        print(f"  {r['feature']:<42} {r['weight']:+.2f}")
    print(f"\nМодель сохранена: config/placement_model.json "
          f"({model['positives']} посадок, {len(streets)} улиц). "
          f"В расчёте: ключ --learned или галочка «по обученной модели».")
    return 0


if __name__ == "__main__":
    sys.exit(main())
