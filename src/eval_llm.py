# -*- coding: utf-8 -*-
"""Проверка локальной модели на размеченной выборке слоёв.

    python src/eval_llm.py                          своя модель greenai-layers
    python src/eval_llm.py --no-library             без подбора примеров
    python src/eval_llm.py --model greenai-layers   своя модель
    python src/eval_llm.py --n 200

Модель получает имена из config/layer_dataset.jsonl и отвечает, как в
расчёте. Ответ сверяется с классом из выборки. Подбор похожих примеров
исключает имя-вопрос и его варианты, так что модель не списывает.

Главные цифры:
  точность    — доля верных среди уверенных ответов (от 0,5);
  охват       — доля имён, на которые модель ответила уверенно;
  опасные     — ответ газон или граница работ там, где их нет. В расчёте
                такие ответы отбрасываются, но их доля показывает, насколько
                модели можно доверять;
  потеря      — препятствие (сеть, опора) названо оформлением: такой слой
                выпадает из ограничений.
"""
import argparse
import os
import random
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layer_model as LM                              # noqa: E402
import llm_layers                                     # noqa: E402
from layers import PERMISSIVE, normalize              # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NON_OBSTACLE = {"annotation", "unknown"} | PERMISSIVE


def sample(rows, n, seed):
    """Уникальные по очищенному имени, поровну из классов."""
    by = defaultdict(list)
    seen = set()
    for r in rows:
        key = normalize(r["name"]).lower()
        if key not in seen:
            seen.add(key)
            by[r["cls"]].append(r)
    rnd = random.Random(seed)
    for v in by.values():
        rnd.shuffle(v)
    out, k = [], 0
    while len(out) < n and any(k < len(v) for v in by.values()):
        for v in by.values():
            if k < len(v) and len(out) < n:
                out.append(v[k])
        k += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="greenai-layers")
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-library", action="store_true")
    ap.add_argument("--holdout", action="store_true",
                    help="проверять только на именах, отложенных при "
                         "дообучении (config/lora_split.json)")
    ap.add_argument("--embed", action="store_true",
                    help="подбирать примеры ещё и по смыслу (bge-m3)")
    args = ap.parse_args()

    LM.refresh_dataset(ROOT)
    rows = LM.load_dataset(ROOT)
    if args.holdout:
        import json
        with open(os.path.join(ROOT, "config", "lora_split.json"),
                  encoding="utf-8") as f:
            held = set(json.load(f)["test"])
        # подбор примеров — только из того, что модель видела при обучении
        test = sample([r for r in rows if r["name"] in held], args.n, args.seed)
        keys = {normalize(r["name"]).lower() for r in test}
        rows = [r for r in rows if normalize(r["name"]).lower() not in keys]
    else:
        test = sample(rows, args.n, args.seed)
    emb = None
    if args.embed:
        import embed
        emb = embed.Embedder(ROOT)
    lib = None if args.no_library else llm_layers.Library(rows, embedder=emb)
    names = [r["name"] for r in test]
    gold = {r["name"]: r["cls"] for r in test}

    t0 = time.time()
    got, msg = llm_layers.suggest(names, model=args.model, verbose=True,
                                  library=lib, exact=True)
    if not got:
        print(msg)
        return 1
    pred = {g["layer"]: (g["cls"] if g["confidence"] >= 0.5 else "unknown")
            for g in got}

    answered = [n for n in names if pred.get(n, "unknown") != "unknown"]
    right = [n for n in answered if pred[n] == gold[n]]
    danger = [n for n in answered if pred[n] in PERMISSIVE
              and gold[n] != pred[n]]
    lost = [n for n in answered if pred[n] == "annotation"
            and gold[n] not in NON_OBSTACLE]
    print(f"\n{msg}, подбор примеров: {'нет' if lib is None else 'да'}, "
          f"{len(names)} имён за {time.time()-t0:.0f} с")
    print(f"  точность {100*len(right)/max(len(answered),1):5.1f}%  "
          f"охват {100*len(answered)/len(names):5.1f}%  "
          f"опасные {len(danger)}  потеря препятствий {len(lost)}")
    errs = Counter((gold[n], pred[n]) for n in answered if pred[n] != gold[n])
    if errs:
        print("  частые ошибки (верно -> ответ):")
        for (g, p), k in errs.most_common(8):
            print(f"    {g:>18} -> {p:<18} {k}")
    for n in (danger + lost)[:10]:
        print(f"    ! {gold[n]:>14} -> {pred[n]:<14} {n[:70]}")

    # Итог запоминается: страница «Модель посадки» показывает, какая
    # модель чего стоит, без повторного прогона.
    import json
    path = os.path.join(ROOT, "config", "llm_eval.json")
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
    except Exception:
        saved = {}
    key = f"{msg.replace('модель ', '')}" + (" (отложенные имена)"
                                              if args.holdout else "")
    saved[key] = {"accuracy_pct": round(100 * len(right) / max(len(answered), 1), 1),
                  "coverage_pct": round(100 * len(answered) / len(names), 1),
                  "dangerous": len(danger), "lost": len(lost), "n": len(names),
                  "library": lib is not None,
                  "date": time.strftime("%d.%m.%Y %H:%M")}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(saved, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
