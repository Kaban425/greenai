# -*- coding: utf-8 -*-
"""Независимая проверка результата расчёта по требованиям ТЗ.

    python src/verify_output.py <входной.dxf|dwg> <папка результата>
    python src/verify_output.py --batch            все улицы из config/batch_report.json

Проверяет то, что эксперты проверят в CAD:
  1. исходные слои не изменены — на каждом слое входного чертежа столько же
     объектов, сколько в выходном DXF;
  2. посадки лежат только на слоях GREEN_AI_*, у каждой — атрибуты
     ID, PORODA, NORMA;
  3. число посадок в DXF совпадает с файлом объяснений;
  4. у каждой посадки есть объяснение со ссылкой на акт, и все её проверки
     пройдены.
Итог — config/verify_report.json.
"""
import collections
import glob
import json
import os
import sys
import time

import ezdxf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def layer_counts(doc):
    c = collections.Counter()
    for e in doc.modelspace():
        c[e.dxf.layer] += 1
    return c


def converted(path):
    """Путь к уже сконвертированному DXF (без повторной конвертации)."""
    if path.lower().endswith(".dxf"):
        return path
    if path.lower().endswith(".pdf"):
        from dxf_io import pdf_cache_path
        c = pdf_cache_path(path)
        return c if os.path.isfile(c) else None
    cands = [os.path.splitext(path)[0] + "_oda.dxf"]
    cands += glob.glob(os.path.join(ROOT, "cache", "dwg", "*",
                                    os.path.basename(os.path.splitext(path)[0]) + "_oda.dxf"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def verify(src, outdir):
    res = {"input": src, "outdir": outdir, "ok": True, "problems": []}

    def bad(msg):
        res["ok"] = False
        res["problems"].append(msg)

    js = glob.glob(os.path.join(outdir, "*_explain.json"))
    dx = [f for f in glob.glob(os.path.join(outdir, "*_GREEN_AI.dxf"))]
    if not js or not dx:
        bad("нет файла объяснений или выходного DXF")
        return res
    data = json.load(open(js[0], encoding="utf-8"))
    pl = data.get("plantings", [])
    n_tree = sum(1 for p in pl if p["type"] == "дерево")
    n_shrub = len(pl) - n_tree
    res.update(trees=n_tree, shrubs=n_shrub)

    # 4. объяснения
    no_act = [p["id"] for p in pl if not any(c.get("act") for c in p.get("checks", []))
              and "не обнаружено" not in p.get("explanation", "")]
    failed = [p["id"] for p in pl if any(not c.get("ok", True) for c in p.get("checks", []))]
    with_npa = sum(1 for p in pl if "743-ПП" in p.get("explanation", ""))
    res.update(explained=len(pl) - len(no_act), with_743=with_npa, failed_checks=len(failed))
    if no_act:
        bad(f"без ссылки на акт: {len(no_act)} ({', '.join(no_act[:5])})")
    if failed:
        bad(f"с непройденной проверкой: {len(failed)} ({', '.join(failed[:5])})")

    # 2–3. слои результата и атрибуты
    out = ezdxf.readfile(dx[0])
    oc = layer_counts(out)
    ins = [e for e in out.modelspace() if e.dxftype() == "INSERT"
           and e.dxf.layer.startswith("GREEN_AI")]
    tags_ok = sum(1 for e in ins if {"ID", "PORODA", "NORMA"} <= {a.dxf.tag for a in e.attribs})
    trees_dxf = sum(1 for e in ins if e.dxf.layer == "GREEN_AI_TREES")
    shrubs_dxf = sum(1 for e in ins if e.dxf.layer == "GREEN_AI_SHRUBS")
    res.update(dxf_trees=trees_dxf, dxf_shrubs=shrubs_dxf, attrs_ok=tags_ok, inserts=len(ins),
               result_layers=sorted(l for l in oc if l.startswith("GREEN_AI")))
    if trees_dxf != n_tree or shrubs_dxf != n_shrub:
        bad(f"в DXF деревьев {trees_dxf}, кустарников {shrubs_dxf}; в объяснениях {n_tree} и {n_shrub}")
    if tags_ok != len(ins):
        bad(f"без атрибутов ID/PORODA/NORMA: {len(ins) - tags_ok}")

    # 1. исходные слои
    inp = converted(src)
    if inp is None:
        res["source_layers"] = "входной DXF не найден — сравнение пропущено"
    else:
        ic = layer_counts(ezdxf.readfile(inp))
        diff = {l: (ic.get(l, 0), oc.get(l, 0)) for l in set(ic) | set(oc)
                if not l.startswith("GREEN_AI") and ic.get(l, 0) != oc.get(l, 0)}
        res.update(source_layers=len(ic), source_objects=sum(ic.values()))
        if diff:
            bad(f"изменены исходные слои: {len(diff)} — " +
                ", ".join(f"{l}: {a}→{b}" for l, (a, b) in list(diff.items())[:5]))
    return res


def main():
    if sys.argv[1:2] == ["--batch"]:
        rep = json.load(open(os.path.join(ROOT, "config", "batch_report.json"), encoding="utf-8"))
        rows = []
        for r in rep["rows"]:
            if not r.get("ok"):
                continue
            t0 = time.time()
            v = verify(r["main"], r["outdir"])
            v["street"] = r["street"]
            v["seconds"] = round(time.time() - t0)
            rows.append(v)
            print(f"{r['street'][:28]:<28} {'OK ' if v['ok'] else 'ОШИБКА'} "
                  f"деревьев {v.get('trees')}/{v.get('dxf_trees')}, атрибуты {v.get('attrs_ok')}/{v.get('inserts')}, "
                  f"743-ПП {v.get('with_743')}, слоёв {v.get('source_layers')} "
                  + "; ".join(v["problems"]), flush=True)
        json.dump({"updated": time.strftime("%d.%m.%Y %H:%M"), "rows": rows},
                  open(os.path.join(ROOT, "config", "verify_report.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"\nПроверено {len(rows)}, без замечаний {sum(v['ok'] for v in rows)}")
    else:
        print(json.dumps(verify(sys.argv[1], sys.argv[2]), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
