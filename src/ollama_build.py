# -*- coding: utf-8 -*-
"""Сборка модели Ollama со встроенными знаниями о слоях.

Ollama не дообучает веса — она запускает готовые модели. Но позволяет
собрать свою модель поверх базовой, зашив в неё постоянные знания:
словарь Мосгоргеотреста, разделы проекта ДВ, кодировку покрытий и
размеченные примеры из накопленной выборки. Такая модель не тратит
запрос на объяснения и отвечает единообразно.

    python src/ollama_build.py                 записать Modelfile
    python src/ollama_build.py --create        и сразу собрать модель
    python src/eval_llm.py --model greenai-layers   проверить точность

Базовая модель — qwen3:8b, если установлена, иначе qwen2.5:7b; другую
можно задать переменной GREENAI_BASE_MODEL.
После сборки расчёт сам выберет модель greenai-layers, если она есть.
"""
import os
import shutil
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layer_model as LM                             # noqa: E402
from layers import normalize                         # noqa: E402
from llm_layers import KNOWLEDGE, OWN_MODEL, PROMPT  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MOSGEO = """Имена файлов выгрузки Мосгоргеотреста, часть имени до знаков $0$:
  tp  — топоплан: здания, ограды, крыльца, береговая линия
  up  — подземные коммуникации
  pp  — проектируемые сети
  brd — рамки листов заказа, это оформление
  kl  — линии градостроительного регулирования
Коды сетей: В1, В2 — водопровод; К1 — канализация; К2 — водосток;
Т1, Т2 — теплосеть; Г — газопровод; КЛС — кабель связи; ДК —
дождеприёмный колодец; НО — наружное освещение.
Покрытия проекта: ДВ_ПП_<тип>_У|Р_<ТР|ПЧ>. У — устройство, Р — ремонт,
АБ — асфальтобетон, ПЛ — плитка. Оборот «за счёт» говорит, что было
раньше; значима часть до него.
Вырубка, к удалению, аварийные, сухостой — деревья под снос."""


def curated():
    """Трудные случаи из регрессионного теста: на них модель ошибалась."""
    try:
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from test_layers import CASES
    except Exception:
        return []
    return [(n, c) for n, c in CASES if c != "ignore"]


def pick_examples(rows, per_class=5):
    """По нескольку разных имён на класс: разнообразие важнее количества."""
    by = defaultdict(list)
    seen = set()
    for r in rows:
        key = normalize(r["name"]).lower()
        if key in seen:
            continue
        seen.add(key)
        by[r["cls"]].append(r["name"])
    out = []
    for cls, names in sorted(by.items()):
        # короткие и длинные вперемешку: образец и простых, и выгрузочных имён
        names = sorted(names, key=len)
        step = max(1, len(names) // per_class)
        out += [(n, cls) for n in names[::step][:per_class]]
    return out


def default_base():
    """qwen3:8b, если установлена: на выборке своя модель на ней дала 90%
    точности против 78% на qwen2.5:7b (python src/eval_llm.py)."""
    from llm_layers import available
    models = available()
    for m in ("qwen3:8b", "qwen2.5:7b"):
        if m in models:
            return m
    return "qwen3:8b"


def main():
    changed, dropped = LM.refresh_dataset(ROOT)
    if changed or dropped:
        print(f"Выборка сверена с текущими правилами: исправлено {changed}, "
              f"убрано {dropped}")
    rows = LM.load_dataset(ROOT)
    examples = curated() + pick_examples(rows)
    classes = PROMPT.split("Классы:", 1)[1].split("Правила разбора:", 1)[0]
    base = os.environ.get("GREENAI_BASE_MODEL") or default_base()
    system = ("Ты классифицируешь слои чертежей благоустройства Москвы.\n\n"
              + MOSGEO + "\n\n" + KNOWLEDGE
              + "\n\nКлассы:" + classes
              + "\nОтвечай строго по схеме: номер слоя, класс, уверенность, "
                "короткое пояснение. Если имя непонятно — unknown и "
                "уверенность ниже 0,5.\n\nПримеры размеченных слоёв:\n"
              + "\n".join(f"  {n} -> {c}" for n, c in examples))
    mf = [f"FROM {base}", "PARAMETER temperature 0",
          "PARAMETER num_ctx 8192",
          'SYSTEM """' + system.replace('"""', "'''") + '"""']
    path = os.path.join(ROOT, "config", "Modelfile")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(mf) + "\n")
    print(f"Modelfile записан: {path}")
    print(f"Базовая модель: {base}; примеров внутри: {len(examples)}")

    if "--create" in sys.argv:
        exe = shutil.which("ollama")
        if not exe:
            print("Ollama не найдена в PATH. Соберите вручную:\n"
                  f"  ollama create {OWN_MODEL} -f config/Modelfile")
            return 1
        print(f"Собираю {OWN_MODEL} ...", flush=True)
        return subprocess.call([exe, "create", OWN_MODEL, "-f", path])
    print("\nДальше в терминале:")
    print(f"  ollama create {OWN_MODEL} -f config/Modelfile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
