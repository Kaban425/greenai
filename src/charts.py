# -*- coding: utf-8 -*-
"""График распределения фактических отступов эталонного проекта.

По каждому классу сетей строится гистограмма расстояний от эталонных посадок
до ближайшего объекта этого класса, поверх которой проводится вертикальная
линия норматива. Наглядно видно, работает ли проект в границах нормы, по её
краю или за ней.

SVG собирается без внешних библиотек: файл открывается в браузере и
вставляется в презентацию как векторное изображение.
"""
import numpy as np

W, H = 330, 182          # размер одной панели
PAD_L, PAD_B, PAD_T = 44, 36, 34
COLS = 3
MAX_PANELS = 6           # на слайд больше не влезает


def _hist(vals, hi, bins=26):
    h, edges = np.histogram(vals, bins=bins, range=(0, hi))
    return h, edges


def _panel(x0, y0, row):
    """Одна панель: гистограмма + линия норматива.

    Медиана и доля ниже нормы приходят из полной выборки; values содержат
    только обрезанный хвост и используются исключительно для формы столбцов.
    """
    vals = np.asarray(row.get("values") or [], dtype=float)
    req = float(row["required_m"])
    if len(vals) == 0:
        return ""
    hi = float(row.get("plot_hi") or max(req * 3.0, 1.0))
    h, edges = _hist(vals, hi)
    if h.max() == 0:
        return ""

    pw, ph = W - PAD_L - 14, H - PAD_B - PAD_T
    sx, sy = pw / hi, ph / h.max()

    below = row.get("below_norm_pct")
    med = row.get("median")
    n = row.get("count", len(vals))
    tail = row.get("tail_pct", 0.0)

    out = [f'<g transform="translate({x0},{y0})">']
    out.append(f'<text class="ttl" x="{PAD_L}" y="13">{row["title"]}</text>')
    out.append(f'<text class="sub" x="{PAD_L}" y="26">'
               f'норма {req:g} м · медиана {med:.2f} м · '
               f'ниже нормы {below:.0f}% · выборка {n:,}</text>')

    for i, cnt in enumerate(h):
        if cnt == 0:
            continue
        bx = PAD_L + edges[i] * sx
        bw = max(1.2, (edges[i + 1] - edges[i]) * sx - 0.8)
        bh = cnt * sy
        under = edges[i + 1] <= req
        out.append(f'<rect class="{"bar under" if under else "bar"}" '
                   f'x="{bx:.1f}" y="{PAD_T + ph - bh:.1f}" '
                   f'width="{bw:.1f}" height="{bh:.1f}"/>')

    out.append(f'<line class="ax" x1="{PAD_L}" y1="{PAD_T + ph}" '
               f'x2="{PAD_L + pw}" y2="{PAD_T + ph}"/>')
    nx = PAD_L + req * sx
    out.append(f'<line class="norm" x1="{nx:.1f}" y1="{PAD_T - 4}" '
               f'x2="{nx:.1f}" y2="{PAD_T + ph}"/>')
    out.append(f'<text class="normlbl" x="{nx + 3:.1f}" y="{PAD_T + 6}">норма</text>')

    for t in (0, hi / 2, hi):
        out.append(f'<text class="tick" x="{PAD_L + t * sx:.1f}" '
                   f'y="{PAD_T + ph + 13}" text-anchor="middle">{t:.1f}</text>')
    xlab = "расстояние, м"
    if tail:
        xlab += f" (дальше {hi:.1f} м — ещё {tail:.0f}% посадок)"
    out.append(f'<text class="axlbl" x="{PAD_L + pw / 2}" '
               f'y="{PAD_T + ph + 28}" text-anchor="middle">{xlab}</text>')
    out.append('</g>')
    return "".join(out)


def write_svg(path, stats, title="", max_panels=MAX_PANELS):
    """stats = {'tree': [строки distance_stats], 'shrub': [...]}

    На график идут классы, которые реально ограничивают: сортировка по доле
    посадок ниже нормы. Остальные только зашумляют слайд.
    """
    RU = {"tree": "Деревья", "shrub": "Кустарники"}
    groups = []
    for kind in ("tree", "shrub"):
        rows = [r for r in (stats.get(kind) or []) if r.get("values")]
        rows.sort(key=lambda r: -(r.get("below_norm_pct") or 0))
        if rows:
            groups.append((RU[kind], rows[:max_panels]))
    if not groups:
        return None

    total_w = COLS * W + 20
    y = 52
    body = []
    for gname, rows in groups:
        body.append(f'<text class="grp" x="14" y="{y}">{gname}</text>')
        y += 12
        for i, row in enumerate(rows):
            body.append(_panel(10 + (i % COLS) * W, y + (i // COLS) * H, row))
        y += ((len(rows) + COLS - 1) // COLS) * H + 16

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}"
 height="{y}" viewBox="0 0 {total_w} {y}">
<style>
  text {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
  .h1 {{ font-size: 17px; font-weight: 600; fill: #1a1a1a; }}
  .h2 {{ font-size: 11.5px; fill: #6b7280; }}
  .grp {{ font-size: 13px; font-weight: 600; fill: #37474f; }}
  .ttl {{ font-size: 11px; font-weight: 600; fill: #1a1a1a; }}
  .sub {{ font-size: 9.5px; fill: #6b7280; }}
  .tick, .axlbl {{ font-size: 8.5px; fill: #9097a1; }}
  .normlbl {{ font-size: 9px; fill: #c62828; }}
  .bar {{ fill: #66bb6a; }}
  .bar.under {{ fill: #ef9a9a; }}
  .ax {{ stroke: #d0d3d8; stroke-width: 1; }}
  .norm {{ stroke: #c62828; stroke-width: 1.4; stroke-dasharray: 4 3; }}
</style>
<rect width="100%" height="100%" fill="#ffffff"/>
<text class="h1" x="14" y="24">Фактические отступы эталонного проекта</text>
<text class="h2" x="14" y="40">{title} · розовым — посадки ближе норматива</text>
{"".join(body)}
</svg>'''
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    return path
