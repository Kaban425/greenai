# -*- coding: utf-8 -*-
"""Подбор ассортимента по правилу 10-20-30.

Правило ограничивает долю одного вида десятью процентами посадок, рода —
двадцатью, семейства — тридцатью. Смысл простой: вспышка вредителя или
болезни бьёт по родственным растениям, и однородная посадка гибнет целиком.
Именно так московские улицы потеряли вязы от голландской болезни, а сейчас
теряют ясени от изумрудной златки.

Породы назначаются точкам по убыванию оценки места: лучшие места получают
породы из начала списка, но как только доля вида, рода или семейства
упирается в потолок, берётся следующая допустимая порода.
"""
import math
import re

DEFAULT_CAPS = (0.10, 0.20, 0.30)      # вид, род, семейство


# Типовые размеры по группам официального списка: сам список размеров
# не содержит, а интервал посадки от них зависит.
GROUP_DEFAULTS = {
    "хвойное":    {"crown_m": 6.0},
    "лиственное": {"crown_m": 8.0},
    "хвойный":    {"crown_m": 1.2, "hedge_interval_m": 0.8},
    "лиственный": {"crown_m": 1.5, "hedge_interval_m": 0.8},
    "лиана":      {"crown_m": 0.6, "hedge_interval_m": 0.5},
}


def load_official(path, category=None, kinds=("tree", "shrub")):
    """Официальный ассортимент ДПиООС -> породы в формате каталога.

    category — код территории (highway, yard, park и т.д.): оставляем только
    то, что на такой территории допустимо. Род и семейство в списке не
    указаны, поэтому род берём по первому слову названия: для правила
    10-20-30 этого достаточно, семейства считаем по роду.
    """
    import yaml
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    out = {"trees": [], "shrubs": []}
    for it in doc.get("items", []):
        if it["kind"] not in kinds:
            continue
        if category and not it.get("allowed", {}).get(category, False):
            continue
        genus = re.split(r"[ /(]", it["name"].strip())[0]
        base = GROUP_DEFAULTS.get(it["group"], {"crown_m": 4.0})
        sp = {
            "id": re.sub(r"[^a-zа-я0-9]+", "_", it["name"].lower())[:48],
            "name": it["name"],
            "genus": genus,
            "family": genus,          # семейства в списке нет
            "group": it["group"],
            "notes": it.get("notes", []),
            "source": it.get("source", ""),
        }
        sp.update(base)
        out["trees" if it["kind"] == "tree" else "shrubs"].append(sp)
    return out, doc.get("categories", {})


def parse_selection(catalog, kind, spec):
    """Строка вида 'all', 'tilia_cordata' или списка через запятую -> породы."""
    items = catalog[kind]
    if not spec or spec == "all":
        return list(items)
    wanted = [w.strip() for w in spec.split(",") if w.strip()]
    out, missing = [], []
    for w in wanted:
        found = next((s for s in items if s["id"] == w or s["name"] == w), None)
        (out if found else missing).append(found or w)
    if missing:
        raise SystemExit(
            f"Породы не найдены: {', '.join(missing)}. Доступные {kind}: "
            + ", ".join(s["id"] for s in items))
    return out


def assign(count, species, caps=DEFAULT_CAPS):
    """Распределяет count посадок между породами по правилу 10-20-30.

    Потолки — жёсткие: не более 10% одного вида, 20% рода, 30% семейства.
    Среди допустимых пород берётся наименее использованная, поэтому подбор
    выходит равномерным. Если потолки исчерпаны (пород слишком мало для
    заданного объёма), они ослабляются по очереди: сначала семейство, затем
    род, затем вид — но ни одна посадка не остаётся без породы.
    """
    if not species or count <= 0:
        return []

    sp_cap, gen_cap, fam_cap = caps
    lim_sp = max(1, math.floor(count * sp_cap))
    lim_gen = max(1, math.floor(count * gen_cap))
    lim_fam = max(1, math.floor(count * fam_cap))

    used_sp, used_gen, used_fam = {}, {}, {}
    relaxed = 0
    out = []

    def gf(s):
        return s.get("genus", s["id"]), s.get("family", s["id"])

    for _ in range(count):
        for level in (0, 1, 2, 3):
            pool = []
            for pos, s in enumerate(species):
                g, f = gf(s)
                if level <= 2 and used_sp.get(s["id"], 0) >= lim_sp:
                    continue
                if level <= 1 and used_gen.get(g, 0) >= lim_gen:
                    continue
                if level == 0 and used_fam.get(f, 0) >= lim_fam:
                    continue
                pool.append((used_sp.get(s["id"], 0), pos, s))
            if pool:
                if level:
                    relaxed = max(relaxed, level)
                pool.sort(key=lambda t: (t[0], t[1]))
                s = pool[0][2]
                break
        else:
            s = species[len(out) % len(species)]
        out.append(s)
        g, f = gf(s)
        used_sp[s["id"]] = used_sp.get(s["id"], 0) + 1
        used_gen[g] = used_gen.get(g, 0) + 1
        used_fam[f] = used_fam.get(f, 0) + 1

    assign.relaxed_level = relaxed
    return out


def assign_constrained(allowed, caps=DEFAULT_CAPS):
    """Породы для посадок, у каждой из которых свой список допустимых пород.

    allowed[i] — породы, подходящие i-й посадке: в узкой полосе только
    компактные, в тени только теневыносливые. Квоты 10-20-30 считаются
    на весь объект сразу. Раньше узким полосам и тени породы назначались
    поверх общего подбора отдельно, и итог нарушал правило, хотя каждый
    подбор по отдельности его соблюдал.

    Сначала обслуживаются посадки с самым коротким списком: у них меньше
    выбора, и квоты им нужнее. Внутри списка берётся наименее
    использованная порода, при равенстве — стоящая в списке раньше.
    """
    n = len(allowed)
    if n == 0:
        return []
    lim = [max(1, math.floor(n * c)) for c in caps]
    used = ({}, {}, {})                  # вид, род, семейство
    rank = {}
    for lst in allowed:
        for pos, s in enumerate(lst):
            rank.setdefault(s["id"], pos)

    def keys(s):
        return (s["id"], s.get("genus", s["id"]), s.get("family", s["id"]))

    def fits(s, level):
        # level 0 — все потолки; 1 — без семейства; 2 — без рода; 3 — без всех
        k = keys(s)
        return all(used[j].get(k[j], 0) < lim[j] for j in range(3 - level))

    out = [None] * n
    relaxed = 0
    for i in sorted(range(n), key=lambda i: (len(allowed[i]), i)):
        cands = allowed[i]
        if not cands:
            continue
        for level in range(4):
            pool = [s for s in cands if fits(s, level)]
            if pool:
                relaxed = max(relaxed, level)
                break
        s = min(pool, key=lambda s: (used[0].get(s["id"], 0),
                                     used[1].get(keys(s)[1], 0),
                                     rank.get(s["id"], 0)))
        out[i] = s
        for j, k in enumerate(keys(s)):
            used[j][k] = used[j].get(k, 0) + 1
    assign_constrained.relaxed_level = relaxed
    return out


def summary(items, kind_ru):
    """Сводка по породам: количество и доля."""
    counts = {}
    for it in items:
        counts[it["species_name"]] = counts.get(it["species_name"], 0) + 1
    total = sum(counts.values()) or 1
    rows = [{"name": n, "count": c, "share_pct": round(100.0 * c / total, 1)}
            for n, c in sorted(counts.items(), key=lambda kv: -kv[1])]
    return {"kind": kind_ru, "total": sum(counts.values()), "rows": rows}


def rule_check(items, caps=DEFAULT_CAPS, catalog_index=None):
    """Проверка соблюдения правила 10-20-30 по факту.

    При количестве посадок меньше десяти правило арифметически недостижимо
    (одна посадка — это уже больше 10%), поэтому оно помечается как
    неприменимое, а не как нарушенное.
    """
    if not items:
        return None
    applicable = len(items) >= 10
    idx = catalog_index or {}
    total = len(items)
    sp, gen, fam = {}, {}, {}
    for it in items:
        s = idx.get(it.get("species_id"), {})
        sp[it["species_name"]] = sp.get(it["species_name"], 0) + 1
        g = s.get("genus", it["species_name"])
        f = s.get("family", it["species_name"])
        gen[g] = gen.get(g, 0) + 1
        fam[f] = fam.get(f, 0) + 1

    def top(d):
        k = max(d, key=d.get)
        return k, round(100.0 * d[k] / total, 1)

    sp_k, sp_v = top(sp)
    g_k, g_v = top(gen)
    f_k, f_v = top(fam)
    tol = 100.0 / len(items) + 0.05      # допуск в одну посадку
    return {
        "applicable": applicable,
        "species": {"name": sp_k, "share_pct": sp_v, "limit_pct": caps[0] * 100,
                    "ok": (not applicable) or sp_v <= caps[0] * 100 + tol},
        "genus": {"name": g_k, "share_pct": g_v, "limit_pct": caps[1] * 100,
                  "ok": (not applicable) or g_v <= caps[1] * 100 + tol},
        "family": {"name": f_k, "share_pct": f_v, "limit_pct": caps[2] * 100,
                   "ok": (not applicable) or f_v <= caps[2] * 100 + tol},
    }
