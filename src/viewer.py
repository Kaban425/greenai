# -*- coding: utf-8 -*-
"""Интерактивная схема плана посадок в одном HTML-файле.

Работает офлайн: ни интернета, ни ключей API, ни внешних библиотек.
Слева SVG-схема с зумом и перетаскиванием, справа обоснование по клику.
"""
import json
import os

# Класс объекта -> (подпись, цвет, толщина линии)
STYLE = {
    "site_boundary":      ("Граница работ",          "#d81b60", 2.0),
    "building":           ("Здания",                 "#5d4037", 1.6),
    "school_kindergarten": ("Школы и детсады",       "#4e342e", 1.6),
    "road":               ("Проезжая часть",         "#37474f", 1.8),
    "walkway":            ("Тротуары и дорожки",     "#8d6e63", 1.4),
    "tram":               ("Трамвайные пути",        "#455a64", 1.6),
    "gas":                ("Газопровод",             "#fbc02d", 1.6),
    "water":              ("Водопровод, дренаж",     "#1e88e5", 1.6),
    "sewer":              ("Канализация",            "#6d4c41", 1.6),
    "heating":            ("Теплосеть",              "#e53935", 1.6),
    "power_cable":        ("Кабели силовые и связи", "#8e24aa", 1.6),
    "lighting_pole":      ("Опоры освещения",        "#546e7a", 1.4),
    "retaining_wall":     ("Подпорные стенки",       "#795548", 1.6),
    "slope":              ("Откосы",                 "#a1887f", 1.4),
    "existing_tree":      ("Существующие деревья",   "#2e7d32", 1.4),
    "lawn":               ("Газоны",                 "#9ccc65", 1.2),
}


def _ring(coords):
    pts = ["{:.3f},{:.3f}".format(x, y) for x, y in coords]
    return "M" + " L".join(pts) + " Z"


def _geom_path(geom):
    """Геометрия shapely -> строка d для <path>. Точки возвращаются отдельно."""
    parts = []
    for g in getattr(geom, "geoms", [geom]):
        t = g.geom_type
        if t == "Polygon":
            parts.append(_ring(g.exterior.coords))
            for ring in g.interiors:
                parts.append(_ring(ring.coords))
        elif t in ("LineString", "LinearRing"):
            pts = ["{:.3f},{:.3f}".format(x, y) for x, y in g.coords]
            if pts:
                parts.append("M" + " L".join(pts))
    return " ".join(parts)


def _geom_points(geom):
    return [(g.x, g.y) for g in getattr(geom, "geoms", [geom]) if g.geom_type == "Point"]


def _stats_html(meta, items):
    """Сводка: что и сколько посажено, площади, ограничения, сверка."""
    out = []
    a = meta.get("assortment") or {}

    def table(sec, title):
        if not sec or not sec.get("rows"):
            return
        out.append(f'<h2>{title} — {sec["total"]} шт</h2>')
        out.append('<table class="vd"><tr><th>Порода</th><th>шт</th>'
                   '<th>доля</th></tr>')
        for r in sec["rows"]:
            out.append(f'<tr><td>{r["name"]}</td><td class="n">{r["count"]}</td>'
                       f'<td class="n">{r["share_pct"]}%</td></tr>')
        out.append("</table>")

    table(a.get("trees"), "Ведомость деревьев")
    table(a.get("shrubs"), "Ведомость кустарников")

    for key, label in (("rule_trees", "деревья"), ("rule_shrubs", "кустарники")):
        r = a.get(key)
        if not r:
            continue
        ok = all(v["ok"] for k, v in r.items() if k != "applicable")
        out.append(f'<h2>Правило 10-20-30 · {label}</h2>')
        out.append('<table class="vd"><tr><th>Уровень</th><th>Максимум</th>'
                   '<th>Доля</th><th>Лимит</th></tr>')
        for k, ru in (("species", "вид"), ("genus", "род"), ("family", "семейство")):
            v = r[k]
            mark = "ok" if v["ok"] else "bad"
            out.append(f'<tr><td>{ru}</td><td>{v["name"]}</td>'
                       f'<td class="n {mark}">{v["share_pct"]}%</td>'
                       f'<td class="n">{v["limit_pct"]:.0f}%</td></tr>')
        out.append("</table>")
        note = "соблюдено" if ok else "превышено"
        if not r.get("applicable", True):
            note = "посадок мало, правило неприменимо"
        out.append(f'<div class="hint">{note}</div>')

    cons_rows = meta.get("constraints_top") or []
    cons_rows = [c for c in cons_rows if c.get("excluded_area_m2")]
    cons_rows.sort(key=lambda c: -c["excluded_area_m2"])
    if cons_rows:
        out.append('<h2>Что ограничивает посадку</h2>')
        out.append('<table class="vd"><tr><th>Объект</th><th>Норма, м</th>'
                   '<th>Исключено, м²</th></tr>')
        for c in cons_rows[:8]:
            d = "запрет" if c.get("hard_ban") else (c.get("distance_m") or "—")
            out.append(f'<tr><td title="{c.get("act","")}, {c.get("clause","")}">'
                       f'{c["title"]}</td><td class="n">{d}</td>'
                       f'<td class="n">{c["excluded_area_m2"]:,.0f}</td></tr>')
        out.append("</table>")

    v = meta.get("validation") or {}
    rows = [(ru, v.get(k)) for k, ru in (("tree", "Деревья"), ("shrub", "Кустарники"))
            if v.get(k)]
    if rows:
        out.append('<h2>Сверка с эталонным проектом</h2>')
        out.append('<table class="vd"><tr><th>Тип</th><th>Эталон</th>'
                   '<th>В зелёной зоне</th><th>По норме</th></tr>')
        for ru, r in rows:
            out.append(f'<tr><td>{ru}</td><td class="n">{r["count"]}</td>'
                       f'<td class="n">{r["on_surface_pct"]}%</td>'
                       f'<td class="n">{r["inside_tol_pct"]}%</td></tr>')
        out.append("</table>")
        out.append('<div class="hint">Доля решений проектировщиков, попадающих '
                   'в зону, рассчитанную сервисом. Проверка модели ограничений '
                   'по реальному проекту.</div>')
    return "".join(out).replace(",", "&#8239;") if False else "".join(out)


def _rings_path(rings, limit=12000):
    """Полилинии -> строка d для <path>."""
    out = []
    for ring in rings[:limit]:
        if len(ring) < 2:
            continue
        pts = ["{:.2f},{:.2f}".format(float(x), float(y)) for x, y in ring]
        out.append("M" + " L".join(pts))
    return " ".join(out)


def _rects_path(rects):
    return " ".join(
        "M{:.2f},{:.2f} L{:.2f},{:.2f} L{:.2f},{:.2f} L{:.2f},{:.2f} Z".format(
            x0, y0, x1, y0, x1, y1, x0, y1)
        for x0, y0, x1, y1 in rects)


def write_html(path, cons, tree_mask, items, meta, shrub_mask=None):
    """Схема плана: слои чертежа, зоны и посадки с обоснованием по клику."""
    import raster_engine as R

    minx, miny = cons.x0, cons.y0
    maxx, maxy = cons.x1, cons.y1
    pad = max(maxx - minx, maxy - miny) * 0.03
    minx, miny, maxx, maxy = minx - pad, miny - pad, maxx + pad, maxy + pad
    w, h = maxx - minx, maxy - miny
    view = "{:.2f} {:.2f} {:.2f} {:.2f}".format(minx, -maxy, w, h)

    svg, legend = [], []

    # Карта поверхностей: что алгоритм считает дорогой, тротуаром, газоном.
    # Выключена по умолчанию, включается в легенде. По ней сразу видно,
    # если проезжая часть прочитана как газон — раньше это обнаруживалось
    # только по деревьям посреди дороги.
    surf = getattr(cons, "surface", None)
    if surf is not None and surf.any():
        for code, label, color in ((1, "Покрытия: дорога и тротуар", "#9aa3a8"),
                                   (2, "Застройка", "#6d4c41"),
                                   (3, "Газон по заливкам", "#7cb342")):
            rects = R.mask_to_rects(cons, surf == code, downsample=2, limit=30000)
            if rects:
                lid = f"lay_surf{code}"
                svg.append('<g id="{}" class="lay" style="display:none">'
                           '<path d="{}" fill="{}" fill-opacity="0.35" '
                           'stroke="none"/></g>'.format(lid, _rects_path(rects), color))
                legend.append((lid, "Карта поверхностей: " + label.lower(),
                               color, False))
    up = getattr(cons, "unpainted_mask", None)
    if up is not None and up.any():
        rects = R.mask_to_rects(cons, up, downsample=2, limit=30000)
        if rects:
            svg.append('<g id="lay_unpainted" class="lay" style="display:none">'
                       '<path d="{}" fill="#fbc02d" fill-opacity="0.35" '
                       'stroke="none"/></g>'.format(_rects_path(rects)))
            legend.append(("lay_unpainted", "Карта поверхностей: незакрашенное, "
                           "считается пригодным", "#fbc02d", False))

    # Зоны рисуются мелко: допустимые полосы бывают шириной в метр, и при
    # грубых блоках их не видно. Запретная — только полностью запретные
    # блоки, допустимые — с шагом в две ячейки.
    forb = R.mask_to_rects(cons, ~tree_mask, downsample=4, reduce="all")
    if forb:
        svg.append('<g id="lay_forbidden" class="lay"><path d="{}" fill="#ef5350" '
                   'fill-opacity="0.10" stroke="none"/></g>'.format(_rects_path(forb)))
        legend.append(("lay_forbidden", "Запрещено для деревьев", "#ef5350", True))

    if shrub_mask is not None:
        sh_only = shrub_mask & ~tree_mask
        sh = R.mask_to_rects(cons, sh_only, downsample=2)
        if sh:
            svg.append('<g id="lay_allowed_shrub" class="lay"><path d="{}" '
                       'fill="#fbc02d" fill-opacity="0.30" stroke="none"/></g>'
                       .format(_rects_path(sh)))
            legend.append(("lay_allowed_shrub", "Можно только кустарники",
                           "#fbc02d", True))

    allow = R.mask_to_rects(cons, tree_mask, downsample=2)
    if allow:
        svg.append('<g id="lay_allowed" class="lay"><path d="{}" fill="#43a047" '
                   'fill-opacity="0.32" stroke="none"/></g>'.format(_rects_path(allow)))
        legend.append(("lay_allowed", "Можно деревья и кустарники", "#43a047", True))

    # исходные слои чертежа: настоящие линии подосновы
    for cls, rings in cons.rings.items():
        label, color, lw = STYLE.get(cls, (cls, "#9e9e9e", 1.2))
        d = _rings_path(rings)
        if not d:
            continue
        fill = color if cls in ("building", "school_kindergarten") else "none"
        svg.append('<g id="lay_{}" class="lay"><path d="{}" fill="{}" '
                   'fill-opacity="0.22" stroke="{}" stroke-width="{}" '
                   'stroke-linecap="round" vector-effect="non-scaling-stroke"/>'
                   '</g>'.format(cls, d, fill, color, lw))
        legend.append(("lay_" + cls, label, color, True))

    # Пустые участки газона с причиной: видно на схеме, почему там пусто.
    zones = meta.get("empty_zones") or []
    zpayload = "[]"
    if zones:
        body = []
        for k, z in enumerate(zones[:60], 1):
            x0, y0, x1, y1 = z["bbox"]
            reason = z["reasons"][0]["title"] if z.get("reasons") else ""
            col = "#e65100" if z.get("underplanted") else "#6d4c41"
            dash = "" if z.get("underplanted") else ' stroke-dasharray="3 2"'
            body.append(
                '<rect class="zn" data-zone="{}" x="{:.2f}" y="{:.2f}" '
                'width="{:.2f}" height="{:.2f}" fill="{}" fill-opacity="0.06" '
                'stroke="{}" stroke-width="1.2" '
                'vector-effect="non-scaling-stroke"{}><title>{}. {:.0f} м²: {}'
                '</title></rect>'.format(k - 1, x0, y0, x1 - x0, y1 - y0, col,
                                         col, dash, k, z["area_m2"],
                                         reason.replace("<", "")))
        svg.append('<g id="lay_empty" class="lay">{}</g>'.format("".join(body)))
        n_bug = sum(1 for z in zones if z.get("underplanted"))
        legend.append(("lay_empty",
                       "Пустые участки газона ({}, недосадок {}) — наведите "
                       "курсор".format(len(zones), n_bug),
                       "#e65100", True))

    trees = [i for i in items if i["type"] == "дерево"]
    shrubs = [i for i in items if i["type"] == "кустарник"]

    if shrubs:
        body = "".join(
            '<circle class="pl" data-id="{}" cx="{:.2f}" cy="{:.2f}" r="{:.2f}" '
            'fill="#7cb342" stroke="#33691e" stroke-width="0.1"/>'.format(
                i["id"], i["x"], i["y"], max(i.get("crown_m", 0.6) / 2, 0.3))
            for i in shrubs)
        svg.append('<g id="lay_shrubs" class="lay">{}</g>'.format(body))
        legend.append(("lay_shrubs", "Кустарники ({} шт)".format(len(shrubs)),
                       "#7cb342", True))

    if trees:
        body = []
        for i in trees:
            r = max(i.get("crown_m", 6.0) / 2.0, 1.0)
            body.append(
                '<circle class="pl crown" data-id="{}" cx="{:.2f}" cy="{:.2f}" '
                'r="{:.2f}" fill="#2e7d32" fill-opacity="0.30" stroke="#1b5e20" '
                'stroke-width="0.2"/>'.format(i["id"], i["x"], i["y"], r))
            body.append(
                '<circle class="pl" data-id="{}" cx="{:.2f}" cy="{:.2f}" r="0.6" '
                'fill="#1b5e20"/>'.format(i["id"], i["x"], i["y"]))
        svg.append('<g id="lay_trees" class="lay">{}</g>'.format("".join(body)))
        legend.append(("lay_trees", "Деревья ({} шт)".format(len(trees)),
                       "#1b5e20", True))

    data = {i["id"]: {"t": i["type"], "s": i["species_name"],
                      "x": round(i["x"], 2), "y": round(i["y"], 2),
                      "e": i["explanation"], "c": i["checks"][:6]}
            for i in items}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    zpayload = json.dumps(
        [{"n": i + 1, "a": z["area_m2"], "w": z["max_width_m"],
          "s": round(100 * z.get("allowed_share", 0)),
          "u": bool(z.get("underplanted")),
          "r": [{"t": q["title"], "p": round(100 * q.get("share", 0))}
                for q in (z.get("reasons") or [])]}
         for i, z in enumerate(zones[:60])],
        ensure_ascii=False).replace("</", "<\\/")

    legend_html = "".join(
        '<label><input type="checkbox"{} data-target="{}">'
        '<span class="sw" style="background:{}"></span>{}</label>'.format(
            " checked" if on else "", lid, color, label)
        for lid, label, color, on in legend)

    html = TEMPLATE.format(
        title=os.path.basename(meta.get("source", "план")),
        view=view, svg="".join(svg), legend=legend_html, payload=payload,
        zpayload=zpayload,
        site=meta.get("site_area_m2", 0), tzone=meta.get("tree_zone_m2", 0),
        szone=meta.get("shrub_zone_m2", 0),
        ntree=len(trees), nshrub=len(shrubs),
        stats=_stats_html(meta, items),
        density=round(len(trees) / max(meta.get("site_area_m2", 1) / 10000.0, 0.01), 1),
        scenario=meta.get("scenario_title", "—"))

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


TEMPLATE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>План озеленения — {title}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin:0; font:14px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;
       color:#1a1a1a; background:#f4f5f7; display:flex; height:100vh; }}
#map {{ flex:1; position:relative; background:#fff; }}
svg {{ width:100%; height:100%; cursor:grab; display:block; }}
svg.drag {{ cursor:grabbing; }}
#side {{ width:380px; background:#fff; border-left:1px solid #dcdfe4;
        overflow-y:auto; padding:18px 20px; }}
h1 {{ font-size:17px; margin:0 0 4px; }}
h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.04em;
     color:#6b7280; margin:22px 0 8px; }}
.stat {{ display:flex; justify-content:space-between; padding:4px 0;
        border-bottom:1px solid #f0f1f3; }}
.stat b {{ font-weight:600; }}
label {{ display:flex; align-items:center; gap:8px; padding:4px 0; cursor:pointer; }}
.sw {{ width:14px; height:14px; border-radius:3px; flex:none; }}
#info {{ background:#f7f8fa; border:1px solid #e3e5e9; border-radius:8px;
        padding:14px; min-height:120px; }}
#info .ttl {{ font-weight:600; font-size:15px; margin-bottom:2px; }}
#info .sub {{ color:#6b7280; font-size:13px; margin-bottom:10px; }}
#info .exp {{ margin-bottom:12px; }}
table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
th {{ text-align:left; color:#6b7280; font-weight:500; padding:3px 0; }}
td {{ padding:3px 0; border-top:1px solid #eceef1; }}
td.ok {{ color:#2e7d32; }}
.hint {{ color:#6b7280; font-size:12.5px; margin-top:6px; }}
table.vd {{ width:100%; border-collapse:collapse; font-size:12.5px;
           margin-top:4px; }}
table.vd th {{ text-align:left; color:#6b7280; font-weight:500;
              border-bottom:1px solid #e3e5e9; padding:4px 0; }}
table.vd td {{ padding:4px 0; border-bottom:1px solid #f2f3f5; }}
table.vd td.n {{ text-align:right; font-variant-numeric:tabular-nums; }}
table.vd td.ok {{ color:#2e7d32; }}
table.vd td.bad {{ color:#c62828; font-weight:600; }}
.pl {{ cursor:pointer; }}
.zn {{ cursor:pointer; }}
.zn:hover {{ fill-opacity:0.18; }}
.zn.sel {{ fill-opacity:0.22; stroke-width:2.4 !important; }}
#info .why {{ margin-top:8px; }}
#info .why div {{ display:flex; justify-content:space-between; gap:10px;
  padding:3px 0; border-top:1px solid #eceef1; font-size:12.5px; }}
#info .bug {{ color:#e65100; font-weight:600; }}
.pl.sel {{ stroke:#e65100 !important; stroke-width:0.6 !important; }}
#zoom {{ position:absolute; left:14px; bottom:14px; display:flex; gap:6px; }}
#zoom button {{ width:32px; height:32px; font-size:17px; background:#fff;
   border:1px solid #d0d3d8; border-radius:6px; cursor:pointer; }}
</style></head><body>

<div id="map">
  <svg id="s" viewBox="{view}" xmlns="http://www.w3.org/2000/svg">
    <g transform="scale(1,-1)">{svg}</g>
  </svg>
  <div id="zoom">
    <button id="zin">+</button><button id="zout">&minus;</button><button id="zfit">⤢</button>
  </div>
</div>

<div id="side">
  <h1>План озеленения</h1>
  <div class="hint">{title}</div>

  <h2>Показатели</h2>
  <div class="stat"><span>Площадь участка</span><b>{site} м²</b></div>
  <div class="stat"><span>Зона для деревьев</span><b>{tzone} м²</b></div>
  <div class="stat"><span>Зона для кустарников</span><b>{szone} м²</b></div>
  <div class="stat"><span>Деревьев</span><b>{ntree} шт</b></div>
  <div class="stat"><span>Кустарников</span><b>{nshrub} шт</b></div>
  <div class="stat"><span>Плотность деревьев</span><b>{density} шт/га</b></div>
  <div class="stat"><span>Сценарий</span><b>{scenario}</b></div>

  <h2>Слои</h2>
  {legend}

  <h2>Обоснование посадки</h2>
  <div id="info"><span class="hint">Кликните на дерево или куст на схеме.</span></div>

  {stats}
</div>

<script>
var DATA = {payload};
var ZONES = {zpayload};
var svg = document.getElementById('s'), box = "{view}".split(' ').map(Number), cur = box.slice();

function apply() {{ svg.setAttribute('viewBox', cur.join(' ')); }}

document.querySelectorAll('#side input[type=checkbox]').forEach(function(cb) {{
  cb.addEventListener('change', function() {{
    var g = document.getElementById(cb.dataset.target);
    if (g) g.style.display = cb.checked ? '' : 'none';
  }});
}});

function zoom(k, cx, cy) {{
  var nw = cur[2] * k, nh = cur[3] * k;
  cur[0] = cx - (cx - cur[0]) * k;
  cur[1] = cy - (cy - cur[1]) * k;
  cur[2] = nw; cur[3] = nh; apply();
}}
document.getElementById('zin').onclick = function() {{
  zoom(0.8, cur[0] + cur[2] / 2, cur[1] + cur[3] / 2); }};
document.getElementById('zout').onclick = function() {{
  zoom(1.25, cur[0] + cur[2] / 2, cur[1] + cur[3] / 2); }};
document.getElementById('zfit').onclick = function() {{ cur = box.slice(); apply(); }};

svg.addEventListener('wheel', function(e) {{
  e.preventDefault();
  var r = svg.getBoundingClientRect();
  var cx = cur[0] + (e.clientX - r.left) / r.width * cur[2];
  var cy = cur[1] + (e.clientY - r.top) / r.height * cur[3];
  zoom(e.deltaY > 0 ? 1.15 : 0.87, cx, cy);
}}, {{passive: false}});

var pan = null;
svg.addEventListener('mousedown', function(e) {{
  pan = {{x: e.clientX, y: e.clientY, vx: cur[0], vy: cur[1]}};
  svg.classList.add('drag');
}});
window.addEventListener('mouseup', function() {{ pan = null; svg.classList.remove('drag'); }});
window.addEventListener('mousemove', function(e) {{
  if (!pan) return;
  var r = svg.getBoundingClientRect();
  cur[0] = pan.vx - (e.clientX - pan.x) / r.width * cur[2];
  cur[1] = pan.vy - (e.clientY - pan.y) / r.height * cur[3];
  apply();
}});

var sel = null;

function showZone(t) {{
  var z = ZONES[parseInt(t.dataset.zone, 10)];
  if (!z) return;
  if (sel) sel.classList.remove('sel');
  sel = t; t.classList.add('sel');
  var rows = z.r.map(function(q) {{
    return '<div><span>' + q.t + '</span><span>' + (q.p ? q.p + '%' : '') +
           '</span></div>';
  }}).join('');
  document.getElementById('info').innerHTML =
    '<div class="ttl">Участок газона без посадок</div>' +
    '<div class="sub">площадь ' + z.a.toLocaleString('ru-RU') +
    ' м², ширина до ' + z.w + ' м, допустимо для деревьев ' + z.s + '% площади</div>' +
    (z.u ? '<div class="bug">Место есть, а посадок нет — это недосадка.</div>' : '') +
    '<div class="why">' + rows + '</div>';
}}

svg.addEventListener('click', function(e) {{
  var t = e.target;
  if (t.classList.contains('zn')) {{ showZone(t); return; }}
  if (!t.classList.contains('pl')) return;
  var d = DATA[t.dataset.id];
  if (!d) return;
  if (sel) sel.classList.remove('sel');
  sel = t; t.classList.add('sel');
  var rows = d.c.map(function(c) {{
    return '<tr><td>' + c.title + '</td><td>' + c.required_m + '</td>' +
           '<td class="' + (c.ok ? 'ok' : '') + '">' + c.actual_m + '</td></tr>';
  }}).join('');
  document.getElementById('info').innerHTML =
    '<div class="ttl">' + t.dataset.id + ' — ' + d.s + '</div>' +
    '<div class="sub">' + d.t + ', координаты ' + d.x + ' / ' + d.y + '</div>' +
    '<div class="exp">' + d.e + '</div>' +
    '<table><tr><th>Объект</th><th>Норма, м</th><th>Факт, м</th></tr>' + rows + '</table>';
}});
</script></body></html>
"""
