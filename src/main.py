# -*- coding: utf-8 -*-
"""GreenAI — генеративный дизайн городского озеленения.

Пайплайн: DXF -> карта ограничений по НПА -> план посадок -> DXF + отчёты.

    python src/main.py --input data/sample.dxf --outdir out
"""
import argparse
import os
import sys
import time
from collections import Counter

import numpy as np
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dxf_io import read_dxf, write_result          # noqa: E402
from layers import classify_all, normalize, NON_OBSTACLE   # noqa: E402


def normalize_safe(name):
    try:
        return normalize(name)
    except Exception:
        return name

import raster_engine as R                          # noqa: E402
import report                                      # noqa: E402
import validate                                    # noqa: E402
import charts                                      # noqa: E402
import assortment                                  # noqa: E402
import viewer                                      # noqa: E402


def accept_weak(got, tag, layer_class, layer_source, refused):
    """Принимает назначения слабого способа: модели, цвета, соседства, подписей.

    Слабый способ может назначить только препятствие или оформление. Газон,
    граница работ и классы, снимающие ограничение, расширяют зону посадки:
    ошибка в них ставит деревья на асфальт, поэтому такие ответы
    отбрасываются и перечисляются — назначить их может человек вручную.
    """
    from layers import PERMISSIVE
    ok = {}
    for name, cls in got.items():
        if cls in PERMISSIVE:
            refused.append((name, cls, tag))
            continue
        ok[name] = cls
        layer_class[name] = cls
        layer_source[name] = tag
    return ok


def load_cfg(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def pick(catalog, wanted, kind):
    for s in catalog[kind]:
        if s["id"] == wanted or s["name"] == wanted:
            return s
    raise SystemExit(f"Порода '{wanted}' не найдена. Доступные {kind}: "
                     + ", ".join(s["id"] for s in catalog[kind]))


def _extent(by_layer, sample=20000):
    """Устойчивые габариты чертежа: 2–98% центров объектов."""
    import shapely
    geoms = [g for gs in by_layer.values() for g in gs[:2000]][:sample]
    if not geoms:
        return None
    b = shapely.bounds(np.asarray(geoms, dtype=object))
    b = b[np.isfinite(b).all(axis=1)]
    if not len(b):
        return None
    cx, cy = (b[:, 0] + b[:, 2]) / 2, (b[:, 1] + b[:, 3]) / 2
    return (np.percentile(cx, 2), np.percentile(cy, 2),
            np.percentile(cx, 98), np.percentile(cy, 98))


def merge_extra(by_layer, fills, paths):
    """Добавляет к плану подоснову той же улицы из других файлов.

    Проект улицы обычно разложен по файлам: топосъёмка, подземные сети,
    красные линии, генплан, посадочный план. Посадочный план без сетей
    расчёт честно останавливал, хотя сети лежали в соседнем файле.
    Слои складываются по имени; заливки подосновы идут раньше заливок
    плана, чтобы проектные покрытия перекрывали существующие. Файл в
    другой системе координат (габариты не пересекаются) пропускается.
    """
    base = _extent(by_layer)
    docs = []
    for path in paths:
        print(f"      + подоснова {os.path.basename(path)} ...", flush=True)
        try:
            d, bl, fl = read_dxf(path)
        except (Exception, SystemExit) as e:   # ошибка подосновы не роняет расчёт
            print(f"        не прочитан: {type(e).__name__}: {e}")
            continue
        ext = _extent(bl)
        if base and ext and (ext[0] > base[2] or ext[2] < base[0]
                             or ext[1] > base[3] or ext[3] < base[1]):
            print(f"        пропущен: не пересекается с планом "
                  f"(другая система координат или другой участок)")
            continue
        n_new = sum(1 for k in bl if k not in by_layer)
        for k, gs in bl.items():
            by_layer.setdefault(k, []).extend(gs)
        fills = list(fl) + list(fills)
        docs.append(d)
        print(f"        слоёв {len(bl)}, из них новых {n_new}, "
              f"объектов {sum(len(v) for v in bl.values()):,}")
    return by_layer, fills, docs


def explanation(checks, kind_ru):
    if not checks:
        return (f"Посадка ({kind_ru}) допустима: нормируемых объектов "
                f"в радиусе 30 м не обнаружено.")
    c = checks[0]
    txt = (f"Посадка ({kind_ru}) допустима. Определяющее ограничение — "
           f"{c['title'].lower()}: требуется не менее {c['required_m']} м, "
           f"фактически {c['actual_m']} м ({c['act']}, {c['clause']}).")
    rest = checks[1:4]
    if rest:
        txt += " Прочие проверенные отступы: " + "; ".join(
            f"{r['title'].lower()} — {r['actual_m']} м при норме {r['required_m']} м"
            for r in rest) + "."
    return txt


def main():
    ap = argparse.ArgumentParser(description="Генеративный дизайн озеленения")
    ap.add_argument("--input", required=True,
                    help="файл из папки input (DXF, DWG или векторный PDF)")
    ap.add_argument("--with", dest="extra", action="append", default=[],
                    metavar="ФАЙЛ",
                    help="дополнительный чертёж той же улицы: топосъёмка, "
                         "сети, красные линии (можно несколько раз)")
    ap.add_argument("--xref-dir", action="append", default=[], metavar="ПАПКА",
                    help="где ещё искать файлы внешних ссылок чертежа")
    ap.add_argument("--no-alley", action="store_true",
                    help="не вести ряд вдоль борта (ряд по оси зоны, как раньше)")
    ap.add_argument("--no-row-extend", action="store_true",
                    help="не продолжать ряды существующих деревьев")
    ap.add_argument("--no-xrefs", action="store_true",
                    help="не подгружать внешние ссылки")
    ap.add_argument("--outdir", default="output")
    ap.add_argument("--norms", default="config/norms.yaml")
    ap.add_argument("--species", default="config/species.yaml")
    ap.add_argument("--tree", default="all",
                    help="порода дерева, список через запятую или all")
    ap.add_argument("--shrub", default="all",
                    help="порода кустарника, список через запятую или all")
    ap.add_argument("--territory", default=None,
                    help="категория территории из официального ассортимента "
                         "ДПиООС: yard, preschool, school, health, highway, "
                         "square, park, industrial")
    ap.add_argument("--llm", action="store_true",
                    help="дораспознать слои локальной моделью через Ollama")
    ap.add_argument("--llm-model", default="qwen3:8b")
    ap.add_argument("--layer-map", default="config/layer_map.yaml",
                    help="файл ручного сопоставления слоёв классам")
    ap.add_argument("--official", default="config/assortment_moscow.yaml",
                    help="файл официального ассортимента")
    ap.add_argument("--max-trees", type=int, default=None,
                    help="жёсткое количество деревьев")
    ap.add_argument("--trees-per-ha", type=float, default=None,
                    help="плотность посадки, шт/га")
    ap.add_argument("--max-shrubs", type=int, default=3000)
    ap.add_argument("--mode", choices=["row", "model", "street", "grove", "mixed"],
                    default=None,
                    help="row — схема выбирается для каждой зелёной зоны по её "
                         "форме: вытянутая полоса вдоль улицы даёт ряд, широкое "
                         "пятно — свободную группу; street/grove/mixed — единый "
                         "отбор точек по оценке места на весь объект")
    ap.add_argument("--north", type=float, default=0.0,
                    help="направление севера на чертеже: градусы по часовой "
                         "стрелке от оси Y (нужно для расчёта теней)")
    ap.add_argument("--spacing", type=float, default=None,
                    help="шаг между деревьями, м (в практике 8-12)")
    ap.add_argument("--no-shrubs", action="store_true")
    ap.add_argument("--cell", type=float, default=R.DEFAULT_CELL,
                    help="размер ячейки расчётной сетки, м")
    ap.add_argument("--clip", default=None, help="фрагмент: xmin,ymin,xmax,ymax")
    ap.add_argument("--scenario", default="strict",
                    help="сценарий расчёта из norms.yaml: strict | root_barrier")
    ap.add_argument("--train", action="store_true",
                    help="обучить модель выбора места на эталоне этого чертежа")
    ap.add_argument("--learned", action="store_true",
                    help="выбирать места по обученной модели вместо ручных весов")
    ap.add_argument("--match-reference", action="store_true",
                    help="сажать столько деревьев, сколько в эталоне "
                         "(для честного сравнения точности)")
    ap.add_argument("--validate", action="store_true",
                    help="сверить результат с эталонными посадками из чертежа")
    ap.add_argument("--crown-correction", action="store_true",
                    help="увеличить отступы пропорционально диаметру кроны "
                         "(примечание к таблице 9.1, интерпретация)")
    ap.add_argument("--tolerance", type=float, default=None,
                    help="допуск сверки, м (по умолчанию ячейка сетки + запас)")
    ap.add_argument("--surface", choices=["auto", "green", "lawn"],
                    default="auto",
                    help="auto — газон, если его контуры восстановились "
                         "(по умолчанию); green — участок минус покрытия; "
                         "lawn — строго внутри контуров газонов")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--crown-in-site", action="store_true",
                    help="требовать, чтобы вся проекция кроны лежала внутри "
                         "границы работ (строже практики)")
    ap.add_argument("--ignore-boundary", action="store_true",
                    help="не ограничивать посадку границей работ: участок "
                         "берётся по всей геометрии чертежа")
    ap.add_argument("--force", action="store_true",
                    help="считать, даже если чертёж распознан ненадёжно")
    ap.add_argument("--plan-only", action="store_true",
                    help="дополнительно выгрузить DXF только со слоями результата")
    args = ap.parse_args()

    # Путь можно указывать коротким именем: файл ищется в папке input.
    if not os.path.exists(args.input):
        for folder in ("input", "data"):
            cand = os.path.join(folder, os.path.basename(args.input))
            if os.path.exists(cand):
                args.input = cand
                break
        else:
            raise SystemExit(f"Файл не найден: {args.input}. "
                             f"Положите его в папку input и укажите имя.")
    os.makedirs(args.outdir, exist_ok=True)
    norms = load_cfg(args.norms)
    catalog = load_cfg(args.species)
    params = dict(norms["placement"])
    if args.trees_per_ha is not None:
        params["target_trees_per_ha"] = args.trees_per_ha
    if args.mode is not None:
        params["mode"] = args.mode
    if args.spacing is not None:
        params["tree_spacing_m"] = args.spacing
    mode = params.get("mode", "street")
    if args.crown_correction:
        params["crown_correction"] = True

    scen = (norms.get("scenarios") or {}).get(args.scenario)
    if scen is None:
        raise SystemExit(f"Сценарий '{args.scenario}' не найден в norms.yaml. "
                         f"Доступные: "
                         + ", ".join((norms.get("scenarios") or {}).keys()))
    for cls, over in (scen.get("overrides") or {}).items():
        rule = norms["rules"].get(cls)
        if rule is None:
            continue
        for k, v in over.items():
            rule[k] = v
        rule["act"] = "Проектное допущение: " + scen["title"].lower()
        rule["clause"] = scen.get("note", "мероприятие по защите сети")

    if args.territory:
        # Официальный ассортимент ДПиООС: берём только то, что допустимо
        # на территории заданной категории.
        official, cat_names = assortment.load_official(args.official,
                                                       args.territory)
        if not official["trees"] and not official["shrubs"]:
            raise SystemExit(
                f"Категория '{args.territory}' не найдена. Доступные: "
                + ", ".join(cat_names.keys()))
        catalog = official
        print(f"Ассортимент ДПиООС для категории "
              f"«{cat_names.get(args.territory, args.territory)}»: "
              f"деревьев {len(official['trees'])}, "
              f"кустарников {len(official['shrubs'])}")

    tree_list = assortment.parse_selection(catalog, "trees", args.tree)
    shrub_list = assortment.parse_selection(catalog, "shrubs", args.shrub)
    if not tree_list:
        tree_list = catalog["trees"]
    if not shrub_list:
        shrub_list = catalog["shrubs"]
    # интервал посадки считаем по самой крупной кроне в подборе
    tree_sp = max(tree_list, key=lambda s: s["crown_m"])
    shrub_sp = max(shrub_list, key=lambda s: s.get("hedge_interval_m", 0.6))
    sp_index = {s["id"]: s for s in catalog["trees"] + catalog["shrubs"]}
    print(f"Ассортимент: деревья {len(tree_list)} пород, "
          f"кустарники {len(shrub_list)} пород")

    t_start = time.time()
    print(f"Сценарий: {scen['title']}")
    print(f"[1/5] Читаю {args.input} ...", flush=True)
    doc, by_layer, fills = read_dxf(args.input)
    extra_docs = []
    xref_report, xref_texts = [], []
    if not args.no_xrefs:
        import xref_load
        # Ложится ли ссылка на план, проверяется по габариту самого плана;
        # у пустой рамки (генплан Лодочной — 103 объекта) габарита нет.
        n_own = sum(len(v) for v in by_layer.values())
        ext = _extent(by_layer) if n_own >= 2000 else None
        if ext:
            pad = 0.1 * max(ext[2] - ext[0], ext[3] - ext[1])
            ext = (ext[0] - pad, ext[1] - pad, ext[2] + pad, ext[3] + pad)
        xb, xf, xref_texts, xref_report = xref_load.load(doc, args.input,
                                                    extra_dirs=args.xref_dir,
                                                    extent=ext)
        if xref_report:
            found = [r for r in xref_report if r["found"]]
            print(f"      внешние ссылки: подгружено {len(found)} из "
                  f"{len(xref_report)}, объектов "
                  f"{sum(r['objects'] for r in found):,}", flush=True)
            if len(found) < len(xref_report):
                print("      недостающие ссылки ищутся рядом с чертежом и на "
                      "уровень выше. Если чертёж скопирован без папок "
                      "комплекта — запустите по исходной папке или укажите "
                      "её ключом --xref-dir", flush=True)
            for k, gs in xb.items():
                by_layer.setdefault(k, []).extend(gs)
            fills = list(xf) + list(fills)
    if args.extra:
        by_layer, fills, more = merge_extra(by_layer, fills, args.extra)
        extra_docs += more
    print(f"      слоёв с геометрией: {len(by_layer)}, "
          f"объектов: {sum(len(v) for v in by_layer.values()):,}")

    print("[2/5] Строю карту ограничений ...", flush=True)
    from layers import load_overrides
    overrides = load_overrides(args.layer_map)
    from layers import classify_with_source
    layer_class, layer_source = classify_with_source(by_layer.keys(), overrides)
    used_ov = sum(1 for k in by_layer if k in overrides
                  or normalize_safe(k) in overrides)
    if used_ov:
        print(f"      ручное сопоставление слоёв: применено к {used_ov}")

    # Слои с невнятными именами — по форме объектов: колодец это маленький
    # круг, существующее дерево — круг кроны. Работает раньше модели и
    # без неё: имя «Слой_17» не подскажет никому, а форма подскажет.
    refused = []
    import geo_classify
    geo = accept_weak(geo_classify.classify_unknown(by_layer, layer_class),
                      "geometry", layer_class, layer_source, refused)
    if geo:
        print(f"      по форме объектов распознано слоёв: {len(geo)}")

    # Контекст чертежа: типы покрытий, соседство с распознанными линиями,
    # слова в именах, выученные на этом же чертеже, и подписи у линий.
    # Не зависят от того, насколько осмысленно назван слой.
    import context_classify as CC
    texts = CC.collect_texts(doc) + xref_texts
    for d in extra_docs:
        texts += CC.collect_texts(d)
    for fn, tag, ru in (
            (lambda: CC.type_codes(layer_class, layer_source), "types",
             "по номеру типа покрытия"),
            (lambda: CC.colocate(by_layer, layer_class), "colocate",
             "по соседству с распознанными линиями"),
            (lambda: CC.tokens(layer_class), "tokens",
             "по словам в именах, выученным на этом чертеже"),
            (lambda: CC.labels(by_layer, layer_class, texts), "labels",
             "по подписям у линий")):
        try:
            got_ctx = fn()
        except Exception as e:
            print(f"      распознавание {ru}: пропущено ({e})")
            continue
        got_ctx = accept_weak(got_ctx, tag, layer_class, layer_source, refused)
        if got_ctx:
            from collections import Counter as _C2
            by = ", ".join(f"{k} {v}" for k, v in
                           _C2(got_ctx.values()).most_common())
            objs = sum(len(by_layer.get(l, [])) for l in got_ctx)
            print(f"      {ru}: слоёв {len(got_ctx)}, объектов {objs:,} — {by}")
            for l in sorted(got_ctx, key=lambda l: -len(by_layer.get(l, [])))[:5]:
                print(f"        {got_ctx[l]:<12} {len(by_layer.get(l, [])):>8,}  {l[:80]}")

    # Обученная модель имён: ищет среди уже известных имён самое похожее.
    # Ловит вариации — опечатки, сокращения, чужие префиксы, — которые
    # правила пропускают. Неуверенные ответы отбрасываются.
    import layer_model as LM
    root_dir0 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lmodel = LM.load(root_dir0)
    if lmodel is not None:
        # Второе мнение — векторная модель bge-m3, если она есть в Ollama:
        # неуверенный ответ модели имён принимается, когда она согласна.
        emodel = None
        try:
            import embed
            import llm_layers
            if any(m.split(":")[0] == embed.MODEL
                   for m in llm_layers.available()):
                emb = embed.Embedder(root_dir0)
                # Векторы готовит обучение модели имён; считать их здесь —
                # минуты занятой видеокарты посреди расчёта.
                if emb.coverage(lmodel.names) < 0.95:
                    print("      векторы имён не подготовлены — второе мнение "
                          "bge-m3 пропущено (обучите модель имён)")
                else:
                    emodel = embed.EmbedModel(emb).fit(lmodel.names,
                                                       lmodel.labels)
                    if emodel.mat is None:
                        emodel = None
        except Exception:
            emodel = None
        got_m = accept_weak(LM.classify_unknown(lmodel, layer_class,
                                                embed_model=emodel), "model",
                            layer_class, layer_source, refused)
        if got_m:
            print(f"      обученная модель имён: слоёв {len(got_m)}")

    # Правила точнее на знакомых именах, поэтому модель зовётся только
    # для того, что осталось без класса, и её ответ ниже порога
    # уверенности отбрасывается: лучше «неизвестно», чем неверный класс.
    if args.llm:
        unknown_now = [l for l, c in layer_class.items() if c == "unknown"]
        if unknown_now:
            import llm_layers
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            known = {l: c for l, c in layer_class.items()
                     if layer_source.get(l) == "rules"}
            geom = {l: llm_layers.describe(by_layer.get(l, []))
                    for l in unknown_now}
            got = llm_layers.classify(unknown_now, model=args.llm_model,
                                      root=root, verbose=not args.quiet,
                                      examples=known, geometry=geom,
                                      library=llm_layers.Library.load(root))
            got = accept_weak(got, "llm", layer_class, layer_source, refused)
            if got:
                from collections import Counter as _C
                top = ", ".join(f"{k} {v}" for k, v in
                                _C(got.values()).most_common(5))
                print(f"      модель добавила классы: {top}")
    if refused:
        print(f"      отклонено назначений, расширяющих зону посадки: "
              f"{len(refused)}. Газон, границу работ и «не учитывать» "
              f"назначает только человек или правило по имени:")
        for name, cls, tag in refused[:10]:
            print(f"        {cls:<18} ({tag}) {name[:70]}")
    SRC_RU = {"manual": "вручную", "rules": "правила по имени",
              "color": "цвет линии", "geometry": "форма объектов",
              "types": "номер типа покрытия",
              "colocate": "соседство с линиями", "tokens": "слова в именах",
              "labels": "подписи у линий", "model": "обученная модель имён",
              "llm": "локальная модель", "unknown": "не распознано"}
    src_layers = Counter(layer_source.values())
    src_objects = Counter()
    for name, src in layer_source.items():
        src_objects[src] += len(by_layer.get(name, []))
    total_objects = sum(src_objects.values()) or 1
    print("      источник классификации:")
    for src in ("manual", "rules", "color", "geometry", "types", "colocate",
                "tokens", "labels", "model", "llm", "unknown"):
        if src_layers.get(src):
            print(f"        {SRC_RU[src]:<18} слоёв {src_layers[src]:>4}, "
                  f"объектов {src_objects[src]:>8,} "
                  f"({100*src_objects[src]/total_objects:4.1f}%)")
    try:
        added = LM.append_examples(root_dir0, layer_class, layer_source,
                                   args.input)
        if added:
            print(f"      в обучающую выборку добавлено слоёв: {added}")
    except Exception:
        pass
    cnt = Counter(layer_class.values())
    print("      слои по классам: "
          + ", ".join(f"{k} {v}" for k, v in cnt.most_common()))

    # Ранняя проверка: пустой чертёж отсекается до построения сетки.
    # Иначе по горстке объектов строится сетка на десятки миллионов ячеек,
    # и минуты уходят на расчёт, у которого заведомо нет смысла.
    total_objects_all = sum(len(v) for v in by_layer.values())
    meaningful = {c for c in layer_class.values()
                  if c not in ("unknown", "annotation", "ignore",
                               "site_boundary", "reference_planting")}
    # Маленький, но полный участок (есть граница работ и несколько видов
    # объектов) — законный: так устроен учебный data/sample.dxf. Пустая
    # рамка генплана без ссылок ни границы, ни объектов не имеет.
    small_but_whole = ("site_boundary" in layer_class.values()
                       and len(meaningful) >= 3)
    if not args.force and ((total_objects_all < 50 and not small_but_whole)
                           or len(meaningful) < 2):
        raise SystemExit(
            f"\nЧЕРТЁЖ ПРАКТИЧЕСКИ ПУСТ: {total_objects_all} объектов, "
            f"значимых классов {len(meaningful)} "
            f"({', '.join(sorted(meaningful)) or 'нет'}).\n"
            "Чаще всего это значит, что геометрия подключена внешними "
            "ссылками (xref), а сами файлы в комплект не попали.\n"
            "Что делать:\n"
            "  1) возьмите векторный PDF генплана — в нём геометрия "
            "впечатана целиком;\n"
            "  2) либо запросите у заказчика файлы подосновы или чертёж "
            "со связанными ссылками.")

    clip = None
    if args.clip:
        clip = tuple(float(v) for v in args.clip.split(","))

    cons = R.RasterConstraints(by_layer, norms, layer_class,
                               non_obstacle=NON_OBSTACLE, cell=args.cell,
                               clip=clip, verbose=not args.quiet, fills=fills,
                               ignore_boundary=args.ignore_boundary)
    if params.get("crown_correction") and tree_sp["crown_m"] > params.get(
            "crown_base_m", 5.0):
        cons.crown_factor = tree_sp["crown_m"] / params.get("crown_base_m", 5.0)
        print(f"      поправка на крону {tree_sp['crown_m']:g} м: "
              f"отступы для деревьев увеличены в {cons.crown_factor:.2f} раза "
              f"(примечание к таблице 9.1, проектное допущение)")

    # Вес слоя — длина его линий, а не число объектов. Картинка или штамп,
    # разбитые на векторы, дают десятки тысяч отрезков по 4 мм: по числу
    # объектов это треть чертежа «без класса», по длине — ничто. Из-за
    # такого мусора расчёт останавливался, и его приходилось гнать с --force.
    import shapely
    layer_len = {}
    for lay, geoms in by_layer.items():
        try:
            layer_len[lay] = float(shapely.length(
                np.asarray(geoms, dtype=object)).sum())
        except Exception:
            layer_len[lay] = 0.0
    if cons.unknown_layers:
        sizes = sorted(((layer_len.get(l, 0.0), len(by_layer.get(l, [])), l)
                        for l in cons.unknown_layers), reverse=True)
        total = sum(n for _, n, _ in sizes)
        print(f"      нераспознано слоёв: {len(cons.unknown_layers)} "
              f"({total:,} объектов, {sum(s for s, _, _ in sizes)/1000:,.1f} км "
              f"линий). Крупнейшие по длине:")
        for ln, n, lay in sizes[:12]:
            print(f"        {ln:>9,.0f} м {n:>7,} об.  {lay[:60]}")

    surf = args.surface
    if surf == "auto" and cons.lawn_mask is not None:
        frac = float(cons.lawn_mask.sum()) / max(float(cons.site_mask.sum()), 1)
        print(f"      поверхность посадки: "
              + ("газоны" if frac >= 0.05 else
                 f"остаток от покрытий (газоны дали лишь {100*frac:.1f}% участка)"))
    # Главная проверка достоверности: без подземных сетей расчёт озеленения
    # смысла не имеет. Их отсутствие означает, что подоснова в чертёж не
    # попала, а не то, что улица от сетей свободна.
    NETS = ("power_cable", "gas", "water", "sewer", "heating")
    found_nets = [c for c in NETS if c in cons.dist]
    missing_nets = [c for c in NETS if c not in cons.dist]
    net_warning = None
    if not found_nets:
        net_warning = ("В чертеже не найдено ни одной подземной сети. "
                       "Отступы от коммуникаций не учтены, результат "
                       "непригоден для проектирования: подключите геоподоснову "
                       "или возьмите генплан с нанесёнными сетями.")
    elif len(found_nets) < 3:
        net_warning = ("Найдены не все виды сетей (" + ", ".join(found_nets)
                       + "). Отсутствуют: " + ", ".join(missing_nets)
                       + ". Проверьте полноту подосновы.")
    if net_warning:
        print("\n  ВНИМАНИЕ: " + net_warning + "\n", flush=True)

    # Ворота достоверности. Правдоподобный результат на нераспознанном
    # чертеже опаснее отказа: его принимают за проектное решение.
    total_obj = sum(len(v) for v in by_layer.values()) or 1
    unknown_obj = sum(len(by_layer.get(l, [])) for l in cons.unknown_layers)
    total_len = sum(layer_len.values()) or 1.0
    unknown_len = sum(layer_len.get(l, 0.0) for l in cons.unknown_layers)
    share = unknown_len / total_len
    missing = [] if "road" in cons.dist else ["проезжая часть"]
    if "site_boundary" not in cons.dist and "site_boundary_aux" not in cons.dist:
        missing.append("граница работ")
    if not found_nets:
        missing.append("подземные сети")
    problems = []
    if share > 0.25:
        problems.append(f"{100*share:.0f}% длины линий без класса "
                        f"({unknown_len/1000:,.1f} из {total_len/1000:,.1f} км, "
                        f"{unknown_obj:,} из {total_obj:,} объектов)")
    if missing:
        problems.append("не найдено: " + ", ".join(missing))
    if problems and not args.force:
        raise SystemExit(
            "\nЧЕРТЁЖ РАСПОЗНАН НЕНАДЁЖНО, расчёт остановлен.\n  "
            + "\n  ".join(problems)
            + "\n\nЧто делать:\n"
            "  1) откройте «Слои чертежа» в интерфейсе и назначьте классы "
            "вручную;\n"
            "  2) либо возьмите генплан с нанесёнными сетями;\n"
            "  3) либо запустите с ключом --force, если отдаёте себе отчёт, "
            "что результат неполный.")
    if problems:
        print("\n  ВНИМАНИЕ: расчёт продолжен по ключу --force. "
              + "; ".join(problems) + "\n", flush=True)

    # Крона самого крупного дерева в подборе должна помещаться в участке.
    cons.tree_crown_m = min(tree_sp["crown_m"], 6.0)
    cons.crown_in_site = args.crown_in_site
    tree_mask, zones_info = cons.allowed_mask("tree", surface=args.surface)
    shrub_mask, shrub_info = cons.allowed_mask("shrub", surface=args.surface)
    area = cons.cell ** 2
    tree_area = float(tree_mask.sum()) * area
    shrub_area = float(shrub_mask.sum()) * area
    print(f"      расчётная область {cons.site_area:,.0f} м², "
          f"зона деревьев {tree_area:,.0f} м², "
          f"зона кустарников {shrub_area:,.0f} м²")

    ref_pts = ref_layers = ref_skipped = None
    if args.validate:
        ref_pts, ref_layers, ref_skipped = validate.collect_reference(
            by_layer, layer_class)

    print("[3/5] Расставляю деревья ...", flush=True)
    # Задача — засадить допустимую площадь по максимуму, поэтому потолка
    # по умолчанию нет. Ограничение включается явно: плотностью или
    # равенством с эталоном для честного сравнения.
    cap = args.max_trees
    tph = params.get("target_trees_per_ha") or 0
    if cap is None and args.match_reference and ref_pts is not None \
            and len(ref_pts.get("tree", [])):
        cap = len(ref_pts["tree"])
        print(f"      лимит равен числу деревьев эталона: {cap} шт")
    elif cap is None and tph > 0:
        cap = max(1, int(cons.site_area / 10000.0 * tph))
        print(f"      лимит {cap} шт ({tph:g} шт/га)")
    elif cap is None:
        print("      без ограничения числа: засаживается вся допустимая зона")
    spacing = max(params["tree_spacing_m"], tree_sp["crown_m"] * 0.75)
    env = R.env_maps(cons, north_deg=args.north, verbose=not args.quiet)
    # Знания для выбора места — из файла, который правится на странице
    # «Модель посадки». Веса нормируются, так что сумма может быть любой.
    wcfg = {}
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "config",
                "placement_weights.yaml"), encoding="utf-8") as _f:
            wcfg = yaml.safe_load(_f) or {}
    except Exception:
        pass
    weights = {k: float(wcfg.get(k, R.ENV_WEIGHTS[k])) for k in R.ENV_WEIGHTS}
    tot = sum(weights.values()) or 1.0
    weights = {k: v / tot for k, v in weights.items()}
    quality = R.env_score(cons, tree_mask, env, weights=weights, verbose=False)
    placement_source = "ручные веса"

    # Обучение на решениях проектировщиков: эталон этого же чертежа.
    import learn
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if args.train:
        if ref_pts is None:
            ref_pts, ref_layers, ref_skipped = validate.collect_reference(
                by_layer, layer_class)
        trees_ref = ref_pts.get("tree") if ref_pts else None
        if trees_ref is not None and len(trees_ref) >= 10:
            model = learn.train(cons, env, cons.green_mask, trees_ref,
                                verbose=not args.quiet)
            if model:
                old = learn.load(root_dir)
                if old and len(old.get("streets") or {}) >= 3:
                    # общая модель по многим улицам ценнее модели по одному
                    # чертежу: одна галочка в форме затирала её
                    path = learn.save(model, root_dir, name="placement_model_single.json")
                    print(f"      модель по этому чертежу сохранена отдельно: "
                          f"{os.path.relpath(path, root_dir)}; в расчёте остаётся "
                          f"общая модель по {len(old['streets'])} улицам")
                else:
                    path = learn.save(model, root_dir)
                    print(f"      модель сохранена: {os.path.relpath(path, root_dir)}")
        else:
            print("      обучение невозможно: в чертеже нет эталонных посадок")

    if args.learned:
        model = learn.load(root_dir)
        if model is None:
            print("      обученной модели нет, выбор места по ручным весам. "
                  "Сначала обучите на чертеже с эталоном: ключ --train")
        else:
            learned_q = learn.score(model, cons, env, cons.green_mask)
            learned_q[~tree_mask] = -1.0
            quality = learned_q
            placement_source = (f"обученная модель (AUC "
                                f"{model.get('auc_holdout', 0):.3f})")
            print(f"      выбор места: {placement_source}")
    if mode == "model":
        # Модель расставляет сама: по всей допустимой зоне, в порядке
        # убывания её оценки, с соблюдением интервала. Рядов не строит,
        # поэтому широкие газоны засаживаются целиком, а не по осевой.
        sc = np.where(tree_mask, np.maximum(quality, 0.001), -1.0).astype(np.float32)
        raw_trees = R.pick_points(cons, sc, spacing, cap or 10 ** 7)
        print(f"      модель расставила: {len(raw_trees)} деревьев "
              f"({placement_source})")
    elif mode == "row":
        # в узких полосах компактные породы с шагом 6 м
        compact = [t for t in tree_list if t.get("crown_m", 8) <= 5.0]
        narrow_sp = 6.0 if compact else None
        seeds = ([] if args.no_row_extend
                 else R.existing_row_extensions(cons, tree_mask))
        raw_trees = R.place_rows(cons, tree_mask, spacing, cap,
                                 seed=args.seed, verbose=not args.quiet,
                                 quality=quality, narrow_spacing=narrow_sp,
                                 alley=not args.no_alley, seeds=seeds)
    else:
        smap = R.score_map(cons, tree_mask, mode) * 0.5 + 0.5 * quality
        smap[~tree_mask] = -1.0
        raw_trees = R.pick_points(cons, smap, spacing, cap,
                                  jitter=cons.cell, seed=args.seed)
    # Добор: ряд в широкой зоне занимает только осевую линию, остальная
    # ширина оставалась пустой. Досаживаем всё, где соблюдается интервал.
    if wcfg.get("fill_remaining", True) and (cap is None or len(raw_trees) < cap):
        extra = R.fill_remaining(
            cons, tree_mask, raw_trees, spacing, quality=quality,
            count=(cap - len(raw_trees)) if cap else None,
            verbose=not args.quiet)
        raw_trees = list(raw_trees) + list(extra)
    lawn_all = R.lawn_everywhere(cons)
    in_site = float((lawn_all & cons.site_mask).sum()) * cons.cell ** 2
    print(f"      газон на чертеже: {lawn_all.sum()*cons.cell**2:,.0f} м², "
          f"из них в границах работ {in_site:,.0f} м²")
    empty = R.explain_empty(cons, lawn_all, tree_mask, raw_trees, spacing,
                            site=cons.site_mask,
                            pavement=cons.pavement_mask | cons.building_mask)
    under = [z for z in empty if z["underplanted"]]
    print(f"      пустых участков газона: {len(empty)}, из них недосадка: "
          f"{len(under)}")
    for z in empty[:6]:
        tag = " НЕДОСАДКА" if z["underplanted"] else ""
        print(f"        {z['area_m2']:>7,.0f} м², ширина до {z['max_width_m']:>4} м{tag}: "
              f"{z['reasons'][0]['title'] if z['reasons'] else 'причина не ясна'}")
    # Правдоподобие: на какой поверхности стоят деревья. Если большинство
    # на «незакрашенном», а не на газоне, — чертёж, скорее всего, прочитан
    # неверно, и это надо показать, а не выдавать как результат.
    surf_stat = {"газон": 0, "незакрашенное": 0, "прочее": 0}
    for x, y, *_ in raw_trees:
        ix = int((x - cons.x0) / cons.cell)
        iy = int((y - cons.y0) / cons.cell)
        if not (0 <= iy < cons.ny and 0 <= ix < cons.nx):
            continue
        # газон — и по штриховке проекта, и по контурам газонов топоплана:
        # иначе газон без штриховки давал ложную тревогу «не на газоне»
        if (cons.surface_ok and cons.surface[iy, ix] == 3) or (
                cons.lawn_mask is not None and cons.lawn_mask[iy, ix]):
            surf_stat["газон"] += 1
        elif cons.unpainted_mask is not None and cons.unpainted_mask[iy, ix]:
            surf_stat["незакрашенное"] += 1
        else:
            surf_stat["прочее"] += 1
    if raw_trees and cons.surface_ok:
        share_up = surf_stat["незакрашенное"] / len(raw_trees)
        print(f"      деревья по поверхностям: газон {surf_stat['газон']}, "
              f"незакрашенное {surf_stat['незакрашенное']}, "
              f"прочее {surf_stat['прочее']}")
        if share_up > 0.5:
            print("      ВНИМАНИЕ: больше половины деревьев стоит на "
                  "незакрашенной площади, а не на газоне. Проверьте на схеме "
                  "слой «Карта поверхностей»: возможно, проезжая часть "
                  "распознана как пригодная.")
    cov = R.coverage(cons, tree_mask, raw_trees, tree_sp["crown_m"])
    print(f"      деревьев: {len(raw_trees)}; кроны накрывают "
          f"{100*cov:.0f}% допустимой зоны")

    raw_shrubs = []
    if not args.no_shrubs:
        print("[4/5] Расставляю кустарники ...", flush=True)
        interval = shrub_sp.get("hedge_interval_m", params["shrub_spacing_m"])
        raw_shrubs = R.hedge_points(cons, shrub_mask, interval,
                                    args.max_shrubs, verbose=not args.quiet)
        # массивы там, где дереву нельзя, а кустарнику можно
        room = args.max_shrubs - len(raw_shrubs)
        if room > 0:
            raw_shrubs = list(raw_shrubs) + list(R.shrub_massifs(
                cons, shrub_mask & ~tree_mask, raw_shrubs,
                spacing_m=max(interval * 1.5, 1.0), count=room,
                verbose=not args.quiet))
        print(f"      кустарников: {len(raw_shrubs)}")
    else:
        print("[4/5] Кустарники отключены.")

    items = []
    # Каждому дереву — свой список подходящих пород, квоты 10-20-30 общие.
    # В узкой полосе крупная крона не поместится — только компактные;
    # в тени застройки светолюбивая порода не приживётся — только
    # теневыносливые. Узкой считается полоса, отмеченная рядовой посадкой,
    # и любое место, где до края зелёной зоны меньше половины ширины
    # узкой полосы: так учитываются и деревья досадки.
    narrow = set(getattr(R.place_rows, "last_narrow", []) or [])
    compact_list = [t for t in tree_list if t.get("crown_m", 8) <= 5.0]
    shade_map = env.get("shadow")
    from scipy import ndimage as _nd
    half_w = _nd.distance_transform_edt(cons.green_mask, sampling=cons.cell)
    allowed = []
    for idx, (x, y, sc) in enumerate(raw_trees):
        ix = int((x - cons.x0) / cons.cell)
        iy = int((y - cons.y0) / cons.cell)
        inside = 0 <= ix < cons.nx and 0 <= iy < cons.ny
        cand = tree_list
        if compact_list and (idx in narrow or (
                inside and 2 * half_w[iy, ix] < R.NARROW_WIDTH_M)):
            cand = compact_list
        if shade_map is not None and inside and shade_map[iy, ix]:
            tolerant = [t for t in cand
                        if t.get("shade_tolerance") in ("medium", "high")]
            cand = tolerant or cand
        allowed.append(cand)
    tree_assign = assortment.assign_constrained(allowed)
    for i, (x, y, sc) in enumerate(raw_trees, 1):
        sp = tree_assign[i - 1] or tree_sp
        ch = cons.explain_xy(x, y, "tree")
        items.append({"id": f"T-{i:03d}", "type": "дерево",
                      "species_id": sp["id"], "species_name": sp["name"],
                      "crown_m": sp["crown_m"], "x": x, "y": y,
                      "checks": ch, "explanation": explanation(ch, "дерево")})

    # Кустарники идут сплошной изгородью, поэтому породу меняем не поштучно,
    # а участками: иначе получится случайная мешанина вместо массивов.
    shrub_assign = assortment.assign(
        max(1, len(raw_shrubs) // 40 + 1), shrub_list)
    for i, (x, y, sc) in enumerate(raw_shrubs, 1):
        sp = shrub_assign[min((i - 1) // 40, len(shrub_assign) - 1)] \
            if shrub_assign else shrub_sp
        ch = cons.explain_xy(x, y, "shrub")
        items.append({"id": f"S-{i:04d}", "type": "кустарник",
                      "species_id": sp["id"], "species_name": sp["name"],
                      "crown_m": sp["crown_m"], "x": x, "y": y,
                      "checks": ch, "explanation": explanation(ch, "кустарник")})

    bad = [it["id"] for it in items if any(not c["ok"] for c in it["checks"])]
    print(f"      контроль нормативов: нарушений {len(bad)}")

    # Вторая, независимая проверка: посадка обязана лежать внутри маски
    # допустимой зоны своего типа. Первая проверка смотрит расстояния до
    # объектов, эта — итоговую зону целиком, включая покрытия и газоны.
    outside = []
    for it in items:
        mk = tree_mask if it["type"] == "дерево" else shrub_mask
        ix = int((it["x"] - cons.x0) / cons.cell)
        iy = int((it["y"] - cons.y0) / cons.cell)
        if not (0 <= iy < cons.ny and 0 <= ix < cons.nx and mk[iy, ix]):
            outside.append(it["id"])
    print(f"      контроль зоны: вне допустимой зоны {len(outside)} "
          f"из {len(items)}")
    if outside:
        print(f"        примеры: {', '.join(outside[:8])}")
        # страховка на любой способ расстановки: вне зоны не сажаем
        drop = set(outside)
        items = [it for it in items if it["id"] not in drop]
        print(f"        убраны из результата: {len(drop)}")

    # Третья проверка — по исходной геометрии, без растра: ловит то, что
    # растр с грубой ячейкой пропустил бы в обеих проверках выше.
    vec_bad = cons.vector_audit(items)
    vec_ids = sorted({v["id"] for v in vec_bad})
    print(f"      точная проверка по геометрии чертежа: нарушений {len(vec_ids)}")
    for v in sorted(vec_bad, key=lambda v: v["actual_m"] - v["required_m"])[:6]:
        print(f"        {v['id']}: {v['title'][:40]} — {v['actual_m']} м "
              f"при норме {v['required_m']} м")
    if vec_ids:
        # норма важнее числа деревьев: такие посадки в результат не идут
        drop = set(vec_ids)
        items = [it for it in items if it["id"] not in drop]
        print(f"        убраны из результата: {len(drop)}")

    tree_items0 = [i for i in items if i["type"] == "дерево"]
    shrub_items0 = [i for i in items if i["type"] == "кустарник"]
    assort = {
        "trees": assortment.summary(tree_items0, "деревья"),
        "shrubs": assortment.summary(shrub_items0, "кустарники"),
        "rule_trees": assortment.rule_check(tree_items0, catalog_index=sp_index),
        "rule_shrubs": assortment.rule_check(shrub_items0, catalog_index=sp_index),
    }
    rt = assort["rule_trees"]
    if rt:
        ok = all(v["ok"] for k, v in rt.items() if k != "applicable")
        print(f"      правило 10-20-30 (деревья): вид {rt['species']['share_pct']}%, "
              f"род {rt['genus']['share_pct']}%, семейство {rt['family']['share_pct']}% — "
              + ("соблюдено" if ok else "превышено")
              + ("" if rt["applicable"] else " (посадок мало, правило неприменимо)"))

    val = None
    if args.validate:
        tol = args.tolerance if args.tolerance is not None \
            else round(cons.cell + norms["placement"]["boundary_margin_m"], 2)
        val = validate.evaluate(cons, {"tree": tree_mask, "shrub": shrub_mask},
                                ref_pts, items, tolerance=tol)
        print(f"        допуск сверки: {tol} м "
              f"(ячейка сетки {cons.cell} м + запас "
              f"{norms['placement']['boundary_margin_m']} м)")
        print("      сверка с эталоном:")
        for k, lst in ref_layers.items():
            if lst:
                print(f"        слои эталона ({validate.RU[k]}): {len(lst)} шт — "
                      + ", ".join(x[:28] for x in lst[:3])
                      + (" ..." if len(lst) > 3 else ""))
                if ref_skipped.get(k):
                    print(f"          отброшено контуров массивов: "
                          f"{ref_skipped[k]:,} (центр массива не является "
                          f"точкой посадки)")
        for line in validate.report_lines(val):
            print("        " + line)

    print("[5/5] Пишу результат ...", flush=True)
    base = os.path.splitext(os.path.basename(args.input))[0]
    zone_rects = R.mask_to_rects(cons, tree_mask, downsample=4)
    dxf_out = os.path.join(args.outdir, f"{base}_GREEN_AI.dxf")
    tree_items = [i for i in items if i["type"] == "дерево"]
    shrub_items = [i for i in items if i["type"] == "кустарник"]
    write_result(doc, dxf_out, tree_items, shrub_items,
                 zone_rects=zone_rects, source_file=args.input)
    if args.plan_only:
        write_result(None, os.path.join(args.outdir, f"{base}_PLAN_ONLY.dxf"),
                     tree_items, shrub_items, zone_rects=zone_rects,
                     result_only=True)

    meta = {"source": args.input,
            "scenario": args.scenario,
            "scenario_title": scen["title"],
            "site_area_m2": round(cons.site_area, 1),
            "tree_zone_m2": round(tree_area, 1),
            "shrub_zone_m2": round(shrub_area, 1),
            "cell_m": cons.cell,
            "mode": mode,
            "violations": bad,
            "vector_violations": vec_bad[:200],
            "vector_violation_ids": vec_ids,
            "unknown_layers": cons.unknown_layers,
            "networks_found": found_nets,
            "placement_source": placement_source,
            "crown_coverage_pct": round(100 * cov, 1),
            "empty_zones": empty[:40],
            "trees_by_surface": surf_stat,
            "site_source": getattr(cons, "site_source", None),
            "unknown_share_pct": round(100 * share, 1),
            "refused": [{"layer": n, "cls": c, "source": t}
                        for n, c, t in refused[:40]],
            "build": "2026-09-28.1",
            "unknown_top": sorted(
                ({"layer": k, "count": len(by_layer.get(k, [])),
                  "length_m": round(layer_len.get(k, 0.0))}
                 for k, v in layer_class.items() if v == "unknown"),
                key=lambda r: -r["length_m"])[:15],
            "classification": {
                "by_source_layers": dict(src_layers),
                "by_source_objects": dict(src_objects),
                "llm_layers": {k: layer_class[k] for k, v in layer_source.items()
                               if v == "llm"},
                "color_layers": {k: layer_class[k] for k, v in layer_source.items()
                                 if v == "color"},
            },
            "networks_missing": missing_nets,
            "xrefs": xref_report,
            "warning": net_warning,
            "validation": val,
            "tree_species": tree_sp, "shrub_species": shrub_sp,
            "assortment": assort,
            "constraints_top": zones_info}
    if val:
        stats = {k: (v.get("distance_stats") or []) for k, v in val.items() if v}
        svg = charts.write_svg(
            os.path.join(args.outdir, f"{base}_otstupy.svg"), stats,
            title=f"{os.path.basename(args.input)} · сценарий «{scen['title']}»")
        if svg:
            print(f"      график распределения отступов: {os.path.basename(svg)}")
    report.write_json(os.path.join(args.outdir, f"{base}_explain.json"),
                      {"meta": meta, "constraints": zones_info, "plantings": items})
    report.write_csv(os.path.join(args.outdir, f"{base}_explain.csv"), items)
    report.write_markdown(os.path.join(args.outdir, f"{base}_report.md"),
                          items, zones_info, meta)
    report.write_schedule(os.path.join(args.outdir, f"{base}_schedule.csv"), items)
    viewer.write_html(os.path.join(args.outdir, f"{base}_plan.html"),
                      cons, tree_mask, items, meta, shrub_mask=shrub_mask)

    # Маски допустимых зон нужны редактору: когда человек переносит дерево
    # вручную, новое место проверяется на нормы без повторного расчёта.
    import numpy as _np
    # Карта поверхностей — для разбора, почему дерево встало именно там.
    extra = {"surface": cons.surface}
    if cons.unpainted_mask is not None:
        extra["unpainted"] = cons.unpainted_mask
    _np.savez_compressed(
        os.path.join(args.outdir, f"{base}_masks.npz"),
        tree=tree_mask, shrub=shrub_mask,
        x0=cons.x0, y0=cons.y0, cell=cons.cell, nx=cons.nx, ny=cons.ny,
        **extra)

    print(f"\nГотово за {time.time()-t_start:.1f} с. Результат в {args.outdir}:")
    for f in sorted(os.listdir(args.outdir)):
        print("  -", f)


if __name__ == "__main__":
    # вывод в файл или через .bat идёт в cp1251: «м²» и «≥» роняли расчёт
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass
    main()
