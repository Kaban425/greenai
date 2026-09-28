# -*- coding: utf-8 -*-
"""Интерпретируемость: отчёты JSON / CSV / Markdown."""
import csv
import json
import os
from datetime import datetime


def write_json(path, payload):
    """Массивы значений для графика в отчёт не пишем: они нужны только при
    построении SVG и раздувают файл на порядок. Исходные данные не трогаем,
    иначе график, который строится позже, останется без значений."""
    def strip(obj):
        if isinstance(obj, dict):
            return {k: strip(v) for k, v in obj.items() if k != "values"}
        if isinstance(obj, list):
            return [strip(v) for v in obj]
        return obj

    with open(path, "w", encoding="utf-8") as f:
        json.dump(strip(payload), f, ensure_ascii=False, indent=2)


def write_csv(path, items):
    cols = ["id", "type", "species_name", "x", "y", "act", "clause",
            "critical_object", "required_m", "actual_m", "explanation"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
        w.writeheader()
        for it in items:
            crit = it["checks"][0] if it["checks"] else {}
            w.writerow({
                "id": it["id"],
                "type": it["type"],
                "species_name": it["species_name"],
                "x": round(it["x"], 3),
                "y": round(it["y"], 3),
                "act": crit.get("act", ""),
                "clause": crit.get("clause", ""),
                "critical_object": crit.get("title", ""),
                "required_m": crit.get("required_m", ""),
                "actual_m": crit.get("actual_m", ""),
                "explanation": it["explanation"],
            })


def write_markdown(path, items, zones_info, meta):
    lines = [
        "# Отчёт о нормативном обосновании плана посадок",
        "",
        f"Исходный файл: `{meta['source']}`  ",
        f"Сценарий расчёта: {meta.get('scenario_title', '—')}  ",
        f"Дата формирования: {datetime.now():%Y-%m-%d %H:%M}  ",
        f"Площадь участка: {meta['site_area_m2']} м²  ",
        f"Допустимая зона для деревьев: {meta['tree_zone_m2']} м²  ",
        f"Допустимая зона для кустарников: {meta['shrub_zone_m2']} м²",
        "",
        (("> **ВНИМАНИЕ.** " + meta["warning"] + "\n")
         if meta.get("warning") else ""),
        "## 1. Применённые ограничения",
        "",
        "| Объект | Отступ дерева, м | Норматив | Исключено площади, м² |",
        "|---|---|---|---|",
    ]
    for z in zones_info:
        d = "посадка запрещена" if z["hard_ban"] else (z["distance_m"] or "—")
        area = z["excluded_area_m2"]
        lines.append(f"| {z['title']} | {d} | {z['act']}, {z['clause']} | "
                     f"{'—' if area is None else area} |")

    if meta.get("unknown_layers"):
        lines += [
            "",
            "> **Внимание.** Слои чертежа, не отнесённые ни к одному классу и потому "
            "не учтённые как ограничения: " + ", ".join(f"`{n}`" for n in meta["unknown_layers"][:40]) + ".",
        ]

    xr = meta.get("xrefs") or []
    if xr:
        got = [r for r in xr if r.get("found")]
        lines += ["", "## Внешние ссылки", "",
                  f"Подгружено {len(got)} из {len(xr)}.", "",
                  "| Ссылка | Файл | Слоёв | Объектов |", "|---|---|---|---|"]
        for r in xr:
            where = (os.path.basename(r["found"]) if r.get("found")
                     else r.get("skipped") or f"нет файла: `{r['path']}`")
            lines.append(f"| {r['name']} | {where} | {r.get('layers', 0)} | "
                         f"{r.get('objects', 0):,} |")

    c = meta.get("classification") or {}
    if c.get("by_source_objects"):
        ru = {"manual": "назначено вручную", "rules": "правила по имени слоя",
              "color": "цвет линии", "geometry": "форма объектов",
              "types": "номер типа покрытия",
              "colocate": "соседство с распознанными линиями",
              "tokens": "слова в именах этого чертежа",
              "labels": "подписи у линий", "model": "обученная модель имён",
              "llm": "локальная модель", "unknown": "не распознано"}
        obj = c["by_source_objects"]
        lay = c.get("by_source_layers", {})
        tot = sum(obj.values()) or 1
        lines += ["", "## Как распознан чертёж", "",
                  "| Источник | Слоёв | Объектов | Доля |", "|---|---|---|---|"]
        for k in ("manual", "rules", "color", "geometry", "types",
                  "colocate", "tokens", "labels", "model", "llm", "unknown"):
            if obj.get(k):
                lines.append(f"| {ru[k]} | {lay.get(k, 0)} | {obj[k]:,} | "
                             f"{100*obj[k]/tot:.1f}% |")
        if c.get("llm_layers"):
            lines += ["", "Слои, распознанные локальной моделью:", "",
                      "| Слой | Класс |", "|---|---|"]
            lines += [f"| {k} | {v} |" for k, v in c["llm_layers"].items()]

    v = meta.get("validation")
    if v:
        lines += ["", "## 2. Сверка с эталонным решением проектировщиков", ""]
        lines += ["| Тип | Эталонных | В зелёной зоне | Строго по норме | "
                  "С допуском |", "|---|---|---|---|---|"]
        ru = {"tree": "Деревья", "shrub": "Кустарники"}
        for kind in ("tree", "shrub"):
            r = v.get(kind)
            if r:
                lines.append(
                    f"| {ru[kind]} | {r['count']} | "
                    f"{r['on_surface']} ({r['on_surface_pct']}%) | "
                    f"{r['inside_allowed']} ({r['inside_pct']}%) | "
                    f"{r['inside_tol']} ({r['inside_tol_pct']}%) |")
        tol = next((v[k]["tolerance_m"] for k in ("tree", "shrub")
                    if v.get(k)), None)
        if tol:
            lines += ["", f"Допуск {tol} м учитывает шаг расчётной сетки и "
                          f"погрешность восстановления геометрии чертежа."]
        for kind in ("tree", "shrub"):
            r = v.get(kind)
            if not r or not r["violations"]:
                continue
            lines += ["", f"Что не проходит у эталонных посадок "
                          f"({ru[kind].lower()}) по модели сервиса:", "",
                      "| Ограничение | Посадок |", "|---|---|"]
            lines += [f"| {t} | {n} |" for t, n in r["violations"][:8]]
        lines += ["", "## 3. Ведомость посадок с обоснованием", ""]
    else:
        lines += ["", "## 2. Ведомость посадок с обоснованием", ""]
    lines += ["| № | Тип | Порода | X | Y | Обоснование |", "|---|---|---|---|---|---|"]
    for it in items:
        lines.append(
            f"| {it['id']} | {it['type']} | {it['species_name']} | "
            f"{it['x']:.2f} | {it['y']:.2f} | {it['explanation']} |"
        )

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_schedule(path, items):
    """Ассортиментная ведомость: порода -> количество."""
    counts = {}
    for it in items:
        key = (it["type"], it["species_name"])
        counts[key] = counts.get(key, 0) + 1
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Тип", "Наименование породы", "Количество, шт"])
        for (t, n), c in sorted(counts.items()):
            w.writerow([t, n, c])
