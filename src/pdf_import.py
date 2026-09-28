# -*- coding: utf-8 -*-
"""Импорт векторного PDF, сохранившего слои CAD (OCG), в DXF.

Чертежи, напечатанные из AutoCAD драйвером "DWG To PDF", содержат
векторную геометрию, размеченную по слоям исходного чертежа. Если
геоподоснова подключена внешними ссылками и сами файлы утеряны,
PDF остаётся единственным носителем сетей — этот модуль их достаёт.

    python src/pdf_import.py "data/05_Генплан.pdf" -o data/podosnova.dxf

Масштаб: чертежи печатаются в 1:500, единица PDF — типографский пункт
(1/72 дюйма), поэтому 1 пункт = 0,0254/72 * 500 = 0,17639 м на местности.
Значение задаётся ключом --scale, если печать была в другом масштабе.
"""
import argparse
import os
import re
import sys
import zlib
from collections import defaultdict

import pypdf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from layers import classify_layer  # noqa: E402

# метров на единицу PDF при печати в 1:500
DEFAULT_SCALE = 0.0254 / 72.0 * 500.0

PAINT_OPS = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
COLOR_OPS = {"RG", "rg", "G", "g", "K", "k", "SC", "SCN", "sc", "scn"}
# Операторы, которые закрашивают область. Такие контуры — это площадь
# покрытия или газона, а не осевая линия, и их можно использовать напрямую,
# не восстанавливая площадь из пунктира.
FILL_OPS = {"f", "F", "f*", "B", "B*", "b", "b*"}
FILL_SUFFIX = "__FILL"
COLOR_SEP = "@@"
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

NUM = re.compile(rb"[-+]?(?:\d+\.?\d*|\.\d+)")
TOKEN = re.compile(rb"/[^\s/\[\]<>(){}]+|<<|>>|\[|\]|\([^)]*\)|<[0-9A-Fa-f\s]*>"
                   rb"|[-+]?(?:\d+\.?\d*|\.\d+)|[A-Za-z*'\"]+")


def mul(m, n):
    """Композиция матриц: сначала m, затем n."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (a1 * a2 + b1 * c2,
            a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2,
            c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2,
            e1 * b2 + f1 * d2 + f2)


def apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def bezier(p0, p1, p2, p3, steps=6):
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        pts.append((
            u**3 * p0[0] + 3 * u*u*t * p1[0] + 3 * u*t*t * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u*u*t * p1[1] + 3 * u*t*t * p2[1] + t**3 * p3[1],
        ))
    return pts


def ocg_names(reader):
    """Карта: объект OCG -> человекочитаемое имя слоя."""
    names = {}
    try:
        root = reader.trailer["/Root"]
        ocp = root.get("/OCProperties")
        if not ocp:
            return names
        for ref in ocp.get("/OCGs", []):
            try:
                obj = ref.get_object()
                nm = str(obj.get("/Name", "")).lstrip("\ufeff")
                names[id_of(ref)] = nm
            except Exception:
                continue
    except Exception:
        pass
    return names


def id_of(ref):
    """Уникальный ключ ссылки на объект PDF."""
    try:
        return (ref.idnum, ref.generation)
    except AttributeError:
        return id(ref)


def page_layer_map(page, names):
    """Карта: имя ресурса (/oc2) -> имя слоя чертежа."""
    out = {}
    try:
        props = page["/Resources"].get("/Properties")
        if not props:
            return out
        for key, ref in props.items():
            nm = names.get(id_of(ref))
            if nm is None:
                try:
                    nm = str(ref.get_object().get("/Name", "")).lstrip("\ufeff")
                except Exception:
                    nm = None
            if nm:
                out[str(key).lstrip("/")] = nm
    except Exception:
        pass
    return out


def content_bytes(page):
    data = b""
    c = page.get("/Contents")
    items = c if isinstance(c, list) else [c]
    for it in items:
        try:
            obj = it.get_object()
            raw = obj._data if hasattr(obj, "_data") else obj.get_data()
            try:
                data += zlib.decompress(raw)
            except Exception:
                data += obj.get_data()
        except Exception:
            continue
        data += b"\n"
    return data


def parse_page(page, lay_map, scale):
    """Разбирает поток страницы -> {слой: [ломаная, ...]} в метрах."""
    data = content_bytes(page)
    # Список, а не словарь: порядок отрисовки определяет, какая заливка
    # окажется сверху. Проектное покрытие рисуется поверх топоплана.
    result = []

    ctm = IDENTITY
    stroke = (0.0, 0.0, 0.0)
    fill = (0.0, 0.0, 0.0)
    stack = []
    oc_stack = []          # текущая вложенность слоёв
    mc_depth = 0           # глубина BDC без слоя
    operands = []
    cur = []               # текущая подтраектория в координатах страницы
    subpaths = []
    start = None
    in_text = False

    def cmyk(c, m, y, k):
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))

    def flush(paint, filled=False):
        nonlocal subpaths, cur
        if cur:
            subpaths.append(cur)
        if paint and subpaths:
            layer = oc_stack[-1] if oc_stack else None
            if layer:
                # Цвет линии — второй признак после имени слоя. На топоплане
                # он стандартизован: газ жёлтый, вода синяя, теплосеть
                # красная, кабели фиолетовые. Имена слоёв у каждой
                # организации свои, а цвет держится.
                col = fill if filled else stroke
                rgb = "#%02x%02x%02x" % tuple(
                    int(255 * max(0.0, min(1.0, v))) for v in col)
                need = 3 if filled else 2
                polys = [[(x * scale, y * scale) for x, y in sp]
                         for sp in subpaths if len(sp) >= need]
                if polys:
                    tag = f"{layer}{COLOR_SEP}{rgb}"
                    if filled:
                        # все контуры одной заливки идут вместе: внутренние
                        # становятся дырками по правилу чётности
                        result.append((tag + FILL_SUFFIX, polys))
                    else:
                        result.extend((tag, [pl]) for pl in polys)
        subpaths, cur = [], []

    def nums(k):
        vals = operands[-k:] if len(operands) >= k else []
        try:
            return [float(v) for v in vals]
        except (TypeError, ValueError):
            return []

    for m in TOKEN.finditer(data):
        t = m.group()
        if NUM.fullmatch(t):
            operands.append(t)
            continue
        if t.startswith(b"/") or t in (b"<<", b">>", b"[", b"]") or t.startswith(b"(") \
                or t.startswith(b"<"):
            operands.append(t)
            continue

        op = t.decode("latin1")

        if in_text:
            if op == "ET":
                in_text = False
            operands = []
            continue

        if op == "BT":
            in_text = True
        elif op == "q":
            stack.append((ctm, stroke, fill))
        elif op == "Q":
            if stack:
                ctm, stroke, fill = stack.pop()
        elif op in COLOR_OPS:
            vals = []
            for t in operands:
                if NUM.fullmatch(t):
                    try:
                        vals.append(float(t))
                    except ValueError:
                        pass
            c = None
            if len(vals) >= 4 and op in ("K", "k"):
                c = cmyk(*vals[-4:])
            elif len(vals) >= 3:
                c = tuple(vals[-3:])
            elif len(vals) == 1:
                c = (vals[0],) * 3
            if c:
                if op.isupper():
                    stroke = c
                else:
                    fill = c
        elif op == "cm":
            v = nums(6)
            if len(v) == 6:
                ctm = mul(tuple(v), ctm)
        elif op == "m":
            v = nums(2)
            if len(v) == 2:
                if cur:
                    subpaths.append(cur)
                start = apply(ctm, v[0], v[1])
                cur = [start]
        elif op == "l":
            v = nums(2)
            if len(v) == 2 and cur:
                cur.append(apply(ctm, v[0], v[1]))
        elif op in ("c", "v", "y"):
            v = nums(6 if op == "c" else 4)
            if cur and v:
                p0 = cur[-1]
                if op == "c":
                    p1 = apply(ctm, v[0], v[1])
                    p2 = apply(ctm, v[2], v[3])
                    p3 = apply(ctm, v[4], v[5])
                elif op == "v":
                    p1 = p0
                    p2 = apply(ctm, v[0], v[1])
                    p3 = apply(ctm, v[2], v[3])
                else:
                    p1 = apply(ctm, v[0], v[1])
                    p3 = apply(ctm, v[2], v[3])
                    p2 = p3
                cur.extend(bezier(p0, p1, p2, p3))
        elif op == "re":
            v = nums(4)
            if len(v) == 4:
                x, y, w, h = v
                if cur:
                    subpaths.append(cur)
                pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
                cur = [apply(ctm, px, py) for px, py in pts]
                subpaths.append(cur)
                cur = []
        elif op == "h":
            if cur and start:
                cur.append(start)
        elif op in PAINT_OPS:
            flush(True, filled=op in FILL_OPS)
        elif op == "n":
            flush(False)                      # обрезка или сброс, не рисуем
        elif op == "BDC":
            layer = None
            if len(operands) >= 2 and operands[-2] == b"/OC":
                key = operands[-1].decode("latin1").lstrip("/")
                layer = lay_map.get(key)
            if layer:
                oc_stack.append(layer)
            else:
                mc_depth += 1
        elif op == "BMC":
            mc_depth += 1
        elif op == "EMC":
            if mc_depth > 0:
                mc_depth -= 1
            elif oc_stack:
                oc_stack.pop()

        operands = []

    flush(False)
    return result


def main():
    ap = argparse.ArgumentParser(description="PDF со слоями CAD -> DXF")
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", default=None, help="выходной DXF")
    ap.add_argument("--scale", type=float, default=DEFAULT_SCALE,
                    help=f"метров на единицу PDF (по умолчанию {DEFAULT_SCALE:.5f}, печать 1:500)")
    ap.add_argument("--pages", default="", help="номера страниц через запятую, с 1")
    ap.add_argument("--all-layers", action="store_true",
                    help="выгружать все слои, а не только распознанные ограничения")
    ap.add_argument("--dry-run", action="store_true", help="только показать статистику")
    args = ap.parse_args()

    # Файл можно указывать коротким именем: ищем в input и data.
    if not os.path.exists(args.pdf):
        for folder in ("input", "data"):
            cand = os.path.join(folder, os.path.basename(args.pdf))
            if os.path.exists(cand):
                args.pdf = cand
                break
        else:
            raise SystemExit(f"Файл не найден: {args.pdf}. "
                             f"Положите PDF в папку input.")
    if args.out is None:
        args.out = os.path.join(
            "input", os.path.splitext(os.path.basename(args.pdf))[0] + ".dxf")

    reader = pypdf.PdfReader(args.pdf)
    names = ocg_names(reader)
    print(f"Файл: {args.pdf}")
    print(f"Страниц: {len(reader.pages)}, слоёв OCG: {len(names)}")

    want = None
    if args.pages:
        want = {int(x) - 1 for x in args.pages.split(",")}

    keep = {"site_boundary", "building", "school_kindergarten", "road", "walkway",
            "tram", "gas", "water", "sewer", "heating", "power_cable",
            "lighting_pole", "retaining_wall", "slope", "existing_tree",
            "lawn", "reference_planting"}

    pages = []
    for i, page in enumerate(reader.pages):
        if want is not None and i not in want:
            continue
        lay_map = page_layer_map(page, names)
        print(f"  страница {i+1}: слоёв на странице {len(lay_map)} ...", flush=True)
        pages.append((i, parse_page(page, lay_map, args.scale)))

    # Листы одного PDF лежат в одной системе координат страницы, и наложение
    # разных листов друг на друга даёт мусор. Без --pages берём лист, где
    # больше всего распознанной геометрии.
    if want is None and len(pages) > 1:
        def useful(p):
            return sum(len(polys) for lay, polys in p[1]
                       if classify_layer(lay) in keep)
        best = max(pages, key=useful)
        print(f"  листов {len(pages)}: взят лист {best[0] + 1} "
              f"(больше всего распознанной геометрии); другой лист — "
              f"ключом --pages", flush=True)
        pages = [best]
    total = [row for _, rows_ in pages for row in rows_]

    if not total:
        print("Геометрии со слоями не найдено.")
        return

    from collections import Counter
    cnt = Counter()
    for lay, polys in total:
        cnt[lay] += len(polys)
    rows = sorted(((lay, classify_layer(lay), n) for lay, n in cnt.items()),
                  key=lambda r: -r[2])

    print("\n%-58s %-18s %8s" % ("СЛОЙ PDF", "КЛАСС", "ЛИНИЙ"))
    print("-" * 90)
    shown = 0
    for lay, cls, n in rows:
        if not args.all_layers and cls not in keep:
            continue
        print("%-58.58s %-18s %8d" % (lay, cls, n))
        shown += 1
        if shown > 60:
            print("  ...")
            break

    xs = [p[0] for _, polys in total for pl in polys for p in pl]
    ys = [p[1] for _, polys in total for pl in polys for p in pl]
    if xs:
        print(f"\nГабариты извлечённой геометрии: "
              f"{max(xs)-min(xs):.1f} x {max(ys)-min(ys):.1f} м "
              f"(проверьте по известной длине объекта)")

    if args.dry_run:
        return

    import ezdxf
    out = args.out
    doc = ezdxf.new("R2013", setup=False)
    msp = doc.modelspace()
    written = 0
    for lay, polys in total:                      # порядок отрисовки сохраняется
        cls = classify_layer(lay)
        if not args.all_layers and cls not in keep:
            continue
        base, _, rgb = lay.partition(COLOR_SEP)
        rgb = rgb.replace(FILL_SUFFIX, "")
        safe = re.sub(r'[<>/\\":;?*|=`,]', "_", lay)[:250] or "PDF_LAYER"
        if safe not in doc.layers:
            doc.layers.add(safe)
        attribs = {"layer": safe}
        if rgb.startswith("#") and len(rgb) == 7:
            try:
                attribs["true_color"] = ezdxf.rgb2int(
                    (int(rgb[1:3], 16), int(rgb[3:5], 16), int(rgb[5:7], 16)))
            except Exception:
                pass
        if lay.endswith(FILL_SUFFIX):
            h = msp.add_hatch(dxfattribs=attribs)
            h.dxf.hatch_style = 0                 # правило чётности: дырки работают
            for pl in polys:
                h.paths.add_polyline_path(pl, is_closed=True)
            written += 1
        else:
            msp.add_lwpolyline(polys[0], dxfattribs=attribs)
            written += 1
    doc.saveas(out)
    print(f"\nЗаписано линий: {written}\nDXF: {out}")


if __name__ == "__main__":
    main()
