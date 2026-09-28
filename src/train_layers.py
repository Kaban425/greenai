# -*- coding: utf-8 -*-
"""Обучение модели имён слоёв на накопленной выборке.

    python src/train_layers.py

Выборка — config/layer_dataset.jsonl. Она пополняется автоматически при
каждом расчёте: туда попадают слои, класс которых определён правилом или
назначен вручную. Модель сохраняется в config/layer_model.json и сразу
используется в расчёте для слоёв, которые правила не распознали.
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layer_model as LM                             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    got = LM.absorb_harvest(ROOT)
    if got:
        print(f"Из сбора по проектам добавлено имён, которые теперь знают "
              f"правила: {got}")
    changed, dropped = LM.refresh_dataset(ROOT)
    if changed or dropped:
        print(f"Выборка сверена с текущими правилами: исправлено {changed}, "
              f"убрано {dropped}")
    rows = LM.load_dataset(ROOT)
    # По одному примеру на очищенное имя: варианты с разными префиксами
    # подосновы («output[1-12]_…$0$Колодцы») ничего не добавляют, а сравнение
    # «каждый с каждым» на 17 тысячах имён шло десятки минут.
    from layers import normalize
    seen, uniq = set(), []
    for r in rows:
        key = normalize(r["name"]).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    rows = uniq
    if len(rows) < 20:
        print(f"Мало примеров для обучения: {len(rows)}. Прогоните несколько "
              f"чертежей или назначьте слои вручную — выборка пополняется сама.")
        return 1
    by = Counter(r["cls"] for r in rows)
    print(f"Выборка: {len(rows)} примеров, {len(by)} классов")
    for c, n in by.most_common():
        print(f"  {c:<22} {n}")

    print("\nПроверка на именах, которых модель не видела (5 частей):")
    rep = {}
    for thr in (0.3, 0.4, 0.5, 0.6):
        r = LM.cross_validate(rows, threshold=thr)
        rep[str(thr)] = r
        print(f"  порог уверенности {thr}: точность {r['accuracy_pct']:5.1f}%, "
              f"отвечает на {r['coverage_pct']:5.1f}% имён")

    model = LM.LayerModel().fit([r["name"] for r in rows], [r["cls"] for r in rows])
    LM.save(model, ROOT, report={"examples": len(rows), "cv": rep})
    print(f"\nМодель сохранена: config/layer_model.json")
    # Векторы bge-m3 для второго мнения готовятся здесь, а не в расчёте.
    try:
        import embed
        import llm_layers
        if any(m.split(":")[0] == embed.MODEL for m in llm_layers.available()):
            emb = embed.Embedder(ROOT)
            if emb.vectors(model.names) is not None:
                print(f"Векторы bge-m3 для {len(model.names)} имён готовы")
    except Exception as e:
        print(f"Векторы bge-m3 не подготовлены: {e}")
    print(f"Расчёт использует её с порогом {LM.THRESHOLD}: неуверенные ответы "
          "отбрасываются, слой остаётся нераспознанным.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
