# -*- coding: utf-8 -*-
"""Сбор имён слоёв из папки с проектами — пополнение обучающей выборки.

    python src/harvest_layers.py "<папка «Пилотный проект 20 улиц»>"
    python src/harvest_layers.py <папка> --tmp <временная папка>

Для каждой подпапки верхнего уровня (улицы) все DWG конвертируются
ODA File Converter одним запуском во временную папку, из DXF читается
только таблица слоёв — начало файла, без геометрии, поэтому быстро.
Временные DXF сразу удаляются.

Имена с классом, который дают правила, дописываются в
config/layer_dataset.jsonl (источник rules). Нераспознанные имена
сохраняются в config/harvest_unknown.txt — это кандидаты на ручную
разметку: именно они учат модель тому, чего не знают правила.
Прочитанные файлы запоминаются в config/harvest_done.json.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layer_model as LM                                  # noqa: E402
from dxf_io import _find_odafc                            # noqa: E402
from layers import classify_with_source                   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def dxf_layer_names(path):
    """Имена слоёв из таблицы LAYER. Читает файл до конца таблицы."""
    names = []
    with open(path, encoding="utf-8", errors="replace") as f:
        in_layer = False
        prev = None
        for i, line in enumerate(f):
            s = line.strip()
            if prev == "2" and s == "LAYER" and not in_layer:
                in_layer = True             # 0 TABLE / 2 LAYER
            elif in_layer:
                if s == "ENDTAB":
                    break
                if prev == "2" and s:
                    names.append(s)
            # пары «код — значение»: код стоит на чётных строках
            prev = s if i % 2 == 0 else None
            if i > 4_000_000:               # таблица слоёв в начале файла
                break
    # первая запись после «2 LAYER» — тип таблицы, не слой
    return [n for n in dict.fromkeys(names) if n != "LAYER"]


def convert(odafc, src_dir, out_dir):
    """Все DWG папки (с подпапками) -> DXF 2013 в out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    # порядок аргументов ODA: версия, тип, обход подпапок, проверка, фильтр
    subprocess.run([odafc, src_dir, out_dir, "ACAD2013", "DXF", "1", "0",
                    "*.DWG"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=3600)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--tmp", default=os.path.join(
        os.environ.get("TEMP", "."), "greenai_harvest"))
    args = ap.parse_args()

    odafc = _find_odafc()
    if not odafc:
        raise SystemExit("ODA File Converter не найден: укажите odafc_path "
                         "в config/local.yaml")
    done_path = os.path.join(ROOT, "config", "harvest_done.json")
    try:
        done = json.load(open(done_path, encoding="utf-8"))
    except Exception:
        done = {}
    unknown = Counter()
    added_total = 0
    for sub in sorted(os.listdir(args.folder)):
        src = os.path.join(args.folder, sub)
        if not os.path.isdir(src):
            continue
        dwgs = [os.path.join(d, f) for d, _, fs in os.walk(src) for f in fs
                if f.lower().endswith(".dwg")]
        todo = [p for p in dwgs if done.get(p) != os.path.getsize(p)]
        if not todo:
            continue
        t0 = time.time()
        out = os.path.join(args.tmp, "street")
        shutil.rmtree(out, ignore_errors=True)
        convert(odafc, src, out)
        names = Counter()
        files = 0
        for d, _, fs in os.walk(out):
            for f in fs:
                if f.lower().endswith(".dxf"):
                    files += 1
                    try:
                        names.update(dxf_layer_names(os.path.join(d, f)))
                    except Exception:
                        continue
        shutil.rmtree(out, ignore_errors=True)
        cls, srcs = classify_with_source(names.keys())
        added = LM.append_examples(ROOT, cls, srcs, sub)
        added_total += added
        for n, c in cls.items():
            if c == "unknown":
                unknown[n] += names[n]
        if not files:
            print(f"{sub[:40]:<40} конвертер не выдал ни одного DXF — пропускаю",
                  flush=True)
            continue
        for p in todo:
            done[p] = os.path.getsize(p)
        with open(done_path, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False)
        print(f"{sub[:40]:<40} DWG {len(todo):>4}, прочитано {files:>4}, "
              f"слоёв {len(names):>5}, в выборку +{added:>4}, "
              f"без класса {sum(1 for c in cls.values() if c == 'unknown'):>4}"
              f"  ({time.time()-t0:.0f} с)", flush=True)

    upath = os.path.join(ROOT, "config", "harvest_unknown.txt")
    old = Counter()
    if os.path.exists(upath):
        for line in open(upath, encoding="utf-8"):
            n, _, name = line.rstrip("\n").partition("\t")
            if name:
                old[name] += int(n or 0)
    old.update(unknown)
    with open(upath, "w", encoding="utf-8") as f:
        for name, n in old.most_common():
            f.write(f"{n}\t{name}\n")
    rows = LM.load_dataset(ROOT)
    print(f"\nДобавлено в выборку: {added_total}, всего примеров: {len(rows)}. "
          f"Нераспознанные имена: {os.path.relpath(upath, ROOT)} ({len(old)})")


if __name__ == "__main__":
    main()
