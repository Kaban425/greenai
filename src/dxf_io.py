# -*- coding: utf-8 -*-
"""Чтение и запись DXF."""
import os

import ezdxf
import numpy as np
from ezdxf import path as ezpath
from ezdxf.path import make_path
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from layers import ANNOTATION_TYPES

FLATTEN_DISTANCE = 0.2   # точность аппроксимации дуг и сплайнов, м

RESULT_LAYERS = {
    "trees":   ("GREEN_AI_TREES",   3),    # 3 = зелёный
    "shrubs":  ("GREEN_AI_SHRUBS",  82),
    "zones":   ("GREEN_AI_ZONES",   8),
    "notes":   ("GREEN_AI_NOTES",   7),
}

TREE_BLOCK = "GREEN_AI_TREE"
SHRUB_BLOCK = "GREEN_AI_SHRUB"


def _ensure_blocks(doc):
    """Условные знаки посадок: круг кроны, перекрестие ствола, атрибуты.

    Блок строится в единичном радиусе и вставляется с масштабом, равным
    радиусу кроны, — так один знак годится для любой породы.
    """
    if TREE_BLOCK not in doc.blocks:
        b = doc.blocks.new(name=TREE_BLOCK)
        b.add_circle((0, 0), 1.0)
        for dx, dy in ((0.18, 0), (0, 0.18)):
            b.add_line((-dx, -dy), (dx, dy))
        b.add_circle((0, 0), 0.06)
        for tag, y in (("ID", 0.0), ("PORODA", -0.3), ("NORMA", -0.6)):
            a = b.add_attdef(tag=tag, insert=(1.15, y), height=0.18)
            a.dxf.invisible = 1          # данные храним, чертёж не засоряем
    if SHRUB_BLOCK not in doc.blocks:
        b = doc.blocks.new(name=SHRUB_BLOCK)
        b.add_circle((0, 0), 1.0)
        b.add_circle((0, 0), 0.25)
        for tag in ("ID", "PORODA", "NORMA"):
            a = b.add_attdef(tag=tag, insert=(1.15, 0), height=0.18)
            a.dxf.invisible = 1


def _entity_to_geom(e):
    """DXF-примитив -> геометрия shapely. None, если не поддерживается."""
    t = e.dxftype()
    try:
        if t == "POINT":
            p = e.dxf.location
            return Point(p.x, p.y)
        if t in ("LINE", "ARC", "CIRCLE", "ELLIPSE", "LWPOLYLINE", "POLYLINE", "SPLINE"):
            path = make_path(e)
            pts = [(v.x, v.y) for v in path.flattening(FLATTEN_DISTANCE)]
            if len(pts) < 2:
                return Point(pts[0]) if pts else None
            closed = t == "CIRCLE" or (
                hasattr(e, "is_closed") and e.is_closed
            ) or (abs(pts[0][0] - pts[-1][0]) < 1e-6 and abs(pts[0][1] - pts[-1][1]) < 1e-6)
            if closed and len(pts) >= 4:
                poly = Polygon(pts)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if poly.is_valid and poly.area > 0:
                    return poly
            return LineString(pts)
        if t == "HATCH":
            polys = []
            for p in e.paths:
                try:
                    vs = [(v[0], v[1]) for v in p.vertices]
                except AttributeError:
                    continue
                if len(vs) >= 4:
                    polys.append(Polygon(vs).buffer(0))
            return unary_union(polys) if polys else None
    except Exception:
        return None
    return None


def _find_odafc():
    """Ищет ODA File Converter: переменная окружения, конфиг, типовые папки."""
    import glob
    import os as _os

    exe = _os.environ.get("ODAFC_PATH")
    if exe and _os.path.isfile(exe):
        return exe

    # Путь можно один раз прописать в config/local.yaml: odafc_path: "C:\..."
    cfg = _os.path.join(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))), "config", "local.yaml")
    if _os.path.isfile(cfg):
        try:
            import yaml
            with open(cfg, encoding="utf-8") as f:
                val = (yaml.safe_load(f) or {}).get("odafc_path")
            if val and _os.path.isfile(val):
                return val
        except Exception:
            pass

    names = ("ODAFileConverter.exe", "ODAFileConverter")
    roots = [_os.environ.get("ProgramFiles", r"C:\Program Files"),
             _os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             _os.environ.get("LOCALAPPDATA", ""),
             _os.environ.get("ProgramW6432", ""),
             "/usr/bin", "/usr/local/bin", "/opt"]
    pats = []
    for root in filter(None, roots):
        for n in names:
            pats += [_os.path.join(root, n),
                     _os.path.join(root, "*", n),
                     _os.path.join(root, "*", "*", n),
                     _os.path.join(root, "ODA", "*", n),
                     _os.path.join(root, "Programs", "*", n)]
    for pat in pats:
        found = glob.glob(pat)
        if found:
            return found[0]

    # последний шанс — обычный PATH
    from shutil import which
    for n in names:
        p = which(n)
        if p:
            return p
    return None


def _read_any(path):
    """Читает DXF, при структурных ошибках — в режиме восстановления.

    Чертежи, собранные из внешних ссылок, после конвертации нередко теряют
    служебные теги. Штатное чтение на таком файле падает, а режим
    восстановления вытаскивает всю уцелевшую геометрию.
    """
    try:
        return ezdxf.readfile(path)
    except ezdxf.DXFStructureError as e:
        print(f"      структура чертежа повреждена ({e}), "
              f"читаю в режиме восстановления ...", flush=True)
        from ezdxf import recover
        doc, auditor = recover.readfile(path)
        if auditor.has_errors:
            print(f"      восстановлено: ошибок {len(auditor.errors)}, "
                  f"исправлений {len(auditor.fixes)}", flush=True)
        return doc


def pdf_cache_path(path):
    """Куда кладётся DXF, извлечённый из PDF: cache/pdf/<хеш пути и версии>.dxf."""
    import hashlib
    here = os.path.dirname(os.path.abspath(__file__))
    st = os.stat(path)
    key = f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime}"
    return os.path.join(os.path.dirname(here), "cache", "pdf",
                        hashlib.sha1(key.encode()).hexdigest()[:12] + ".dxf")


def _pdf_to_dxf(path):
    """Векторный PDF со слоями CAD → DXF в кэше проекта (src/pdf_import.py)."""
    import subprocess
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    dest = pdf_cache_path(path)
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print("      PDF: извлекаю векторную геометрию по слоям ...", flush=True)
    code = subprocess.run([sys.executable, os.path.join(here, "pdf_import.py"),
                           path, "-o", dest]).returncode
    if code != 0 or not os.path.isfile(dest):
        raise SystemExit("Не удалось извлечь геометрию из PDF: нужен векторный "
                         "PDF со слоями CAD (печать из AutoCAD/nanoCAD).")
    return dest


def _open(path, convert_dir=None):
    """Открывает DXF, DWG — через ODA File Converter, PDF — через pdf_import."""
    if path.lower().endswith(".pdf"):
        return _read_any(_pdf_to_dxf(path))
    if not path.lower().endswith(".dwg"):
        return _read_any(path)

    from ezdxf.addons import odafc

    exe = _find_odafc()
    if exe:
        # В разных версиях ezdxf путь задаётся по-разному: раньше переменной
        # модуля, теперь через настройки библиотеки. Ставим всеми способами.
        ok = []
        try:
            odafc.win_exec_path = exe
            ok.append("модуль")
        except Exception:
            pass
        for section, key in (("odafc-addon", "win_exec_path"),
                             ("odafc-addon", "unix_exec_path"),
                             ("odafc-addon", "exec_path")):
            try:
                ezdxf.options.set(section, key, exe)
                ok.append(key)
            except Exception:
                pass
        os.environ.setdefault("ODAFC_PATH", exe)
        print(f"      ODA File Converter: {exe} "
              f"(настроено: {', '.join(ok) or 'нет'})", flush=True)

    # Конвертируем в файл и читаем с восстановлением: прямое чтение падает,
    # если конвертер отдал чертёж с битой структурой.
    # convert_dir — куда класть результат конвертации. По умолчанию рядом
    # с исходником (папка input); чужие папки с проектами так не засоряются.
    dest = os.path.splitext(path)[0] + "_oda.dxf"
    fresh = (os.path.isfile(dest) and os.path.getsize(dest) > 0
             and os.path.getmtime(dest) >= os.path.getmtime(path))
    if (not convert_dir and not fresh
            and not os.access(os.path.dirname(os.path.abspath(path)), os.W_OK)):
        # папка только для чтения (том Docker с «:ro», диск заказчика):
        # результат конвертации — в кэш проекта
        import hashlib
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        h = hashlib.sha1(os.path.dirname(os.path.abspath(path)).encode()).hexdigest()[:10]
        convert_dir = os.path.join(root, "cache", "dwg", h)
    if convert_dir:
        os.makedirs(convert_dir, exist_ok=True)
        dest = os.path.join(convert_dir, os.path.basename(dest))
    if (os.path.isfile(dest) and os.path.getsize(dest) > 0
            and os.path.getmtime(dest) >= os.path.getmtime(path)):
        # уже сконвертирован: подоснова с десятком ссылок иначе
        # конвертировалась бы заново при каждом расчёте
        print(f"      беру готовый {os.path.basename(dest)}", flush=True)
        try:
            return _read_any(dest)
        except Exception:
            pass
    src = path
    tmpdir = None
    if any(c in os.path.basename(path) for c in "[]*?"):
        # Конвертер понимает имя как шаблон: «output[1-7]_up.dwg» у ДЖКХ
        # не находился, и подоснова с сетями не читалась. Копия под
        # безопасным именем.
        import shutil
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="greenai_dwg_")
        src = os.path.join(tmpdir, "drawing.dwg")
        shutil.copyfile(path, src)
    try:
        print("      конвертирую DWG ...", flush=True)
        # Конвертер сыплет в консоль сотни строк про ACAD_PROXY_OBJECT —
        # это служебные объекты чужих приложений, на геометрию они не влияют.
        import contextlib
        import io as _io
        noise = _io.StringIO()
        with contextlib.redirect_stdout(noise), contextlib.redirect_stderr(noise):
            odafc.convert(src, dest, version="R2013", replace=True)
        skipped = noise.getvalue().count("ACAD_PROXY_OBJECT")
        if skipped:
            print(f"      пропущено служебных объектов конвертером: {skipped}",
                  flush=True)
    except Exception as e:
        raise SystemExit(
            "Не удалось запустить конвертацию DWG: " + str(e) + "\n"
            "Укажите путь к конвертеру в config/local.yaml (odafc_path) "
            "или сконвертируйте файл в DXF 2013 ASCII вручную."
        )
    finally:
        if tmpdir:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
    if not os.path.isfile(dest):
        raise SystemExit(f"Конвертер не создал файл {dest}. "
                         f"Сконвертируйте DWG в DXF 2013 ASCII вручную.")
    print(f"      получен {os.path.basename(dest)}", flush=True)
    try:
        return _read_any(dest)
    except Exception as e:
        raise SystemExit(
            f"Чертёж не читается даже в режиме восстановления: {e}\n"
            "Скорее всего, исходный DWG собран из внешних ссылок, файлы "
            "которых отсутствуют. Возьмите векторный PDF генплана: в нём "
            "геометрия впечатана целиком."
        )


def _hatch_rings(e):  # noqa: D401
    """Контуры штриховки, включая дуги и внутренние границы (дырки).

    Заливка покрытия — это штриховка: внешний контур плюс внутренние,
    которые вырезают островки газона. Разбор через ezdxf.path берёт и
    дуговые границы, которые прямой обход вершин теряет.
    """
    out, err = [], None
    try:
        for p in ezpath.from_hatch(e):
            pts = np.array([(v.x, v.y) for v in p.flattening(FLATTEN_DISTANCE)])
            if len(pts) >= 3:
                out.append(pts)
    except Exception as ex:
        err = f"{type(ex).__name__}: {ex}"

    if not out:
        # Запасной разбор: у штриховки берём границы напрямую. Нужен,
        # когда ezdxf.path не справляется с составными краевыми путями.
        try:
            for bp in e.paths:
                vs = getattr(bp, "vertices", None)
                if vs:
                    pts = np.array([(v[0], v[1]) for v in vs])
                    if len(pts) >= 3:
                        out.append(pts)
        except Exception as ex:
            if err is None:
                err = f"{type(ex).__name__}: {ex}"
    return out, err


COLOR_SEP = "@@"


def _layer_colors(doc):
    """Цвет каждого слоя чертежа в RGB."""
    from ezdxf import colors as _c
    out = {}
    for lay in doc.layers:
        try:
            tc = lay.dxf.get("true_color")
            if tc is not None:
                out[lay.dxf.name] = _c.int2rgb(tc)
                continue
            aci = abs(int(lay.dxf.get("color", 7)))
            out[lay.dxf.name] = _c.aci2rgb(aci)
        except Exception:
            continue
    return out


def _entity_rgb(e, layer_rgb, block_rgb=None):
    """Фактический цвет объекта: собственный либо унаследованный от слоя.

    block_rgb — цвет вставки блока: его берут объекты с цветом «по блоку».
    """
    from ezdxf import colors as _c
    try:
        tc = e.dxf.get("true_color")
        if tc is not None:
            return _c.int2rgb(tc)
        aci = int(e.dxf.get("color", 256))
        if aci == 0:                     # по блоку
            return block_rgb if block_rgb is not None else layer_rgb
        if aci == 256:                   # по слою
            return layer_rgb
        return _c.aci2rgb(abs(aci))
    except Exception:
        return layer_rgb


MAX_BLOCK_DEPTH = 8      # вложенность блоков: глубже в чертежах не бывает


def read_dxf(path, explode_blocks=True, convert_dir=None):
    """Читает DXF или DWG.

    Возвращает (doc, {слой: [геометрии]}, [(слой, [контуры])]).
    Третий элемент — заливки в порядке отрисовки: по ним строится карта
    поверхностей, где каждая следующая заливка перекрывает предыдущую.
    """
    doc = _open(path, convert_dir)
    msp = doc.modelspace()
    by_layer, fills = {}, []
    stat = {"hatch": 0, "hatch_ok": 0, "hatch_err": None, "split": 0}
    lcol = _layer_colors(doc)

    def keyed(layer, e, block_rgb=None):
        """Если объект раскрашен не цветом своего слоя — выделяем его в
        виртуальный подслой «слой@@#rrggbb». На слое «0» проектировщики
        часто рисуют всё подряд, различая сети только цветом: без этого
        цвет терялся, и слой целиком оставался нераспознанным."""
        base = lcol.get(layer)
        rgb = _entity_rgb(e, base, block_rgb)
        if rgb is None or base is None or tuple(rgb) == tuple(base):
            return layer
        stat["split"] += 1
        return "%s%s#%02x%02x%02x" % (layer, COLOR_SEP, *[int(v) for v in rgb])

    def push(layer, geom):
        if geom is None or geom.is_empty:
            return
        by_layer.setdefault(layer, []).append(geom)

    def handle(e, layer, block_rgb=None):
        t = e.dxftype()
        if t in ANNOTATION_TYPES:
            return                        # размеры, выноски, тексты — не геометрия
        layer = keyed(layer, e, block_rgb)
        if t == "HATCH":
            stat["hatch"] += 1
            rings, err = _hatch_rings(e)
            if err and stat["hatch_err"] is None:
                stat["hatch_err"] = err
            if rings:
                stat["hatch_ok"] += 1
                fills.append((layer, rings))
                for r in rings:           # контуры идут и в карту расстояний
                    push(layer, LineString(np.vstack([r, r[:1]])))
            return
        push(layer, _entity_to_geom(e))

    def explode(ins, layer, block_rgb=None, depth=0):
        """Содержимое вставки блока, со слоем и цветом по правилам AutoCAD.

        Объект на слое «0» внутри блока принимает слой вставки, объект
        с цветом «по блоку» — её цвет. Раньше всё содержимое блоков
        оставалось на слое «0»: опоры, колодцы, знаки деревьев — обычно
        блоки, и они целиком попадали в нераспознанное. Вложенные блоки
        раскрываются рекурсивно, иначе их геометрия терялась совсем.
        """
        rgb = _entity_rgb(ins, lcol.get(layer), block_rgb)
        try:
            if ins.mcount > 1:           # MINSERT: вставка сеткой
                subs = []
                for v in ins.multi_insert():
                    subs.extend(v.virtual_entities())
            else:
                subs = ins.virtual_entities()
            for sub in subs:
                lay = sub.dxf.get("layer") or "0"
                if lay == "0":
                    lay = layer
                if sub.dxftype() == "INSERT":
                    if depth < MAX_BLOCK_DEPTH:
                        explode(sub, lay, rgb, depth + 1)
                    continue
                handle(sub, lay, rgb)
            stat["blocks"] += 1
        except Exception:
            push(layer, Point(ins.dxf.insert.x, ins.dxf.insert.y))

    # Внешние ссылки: блок пуст, геометрия в другом файле. Вставку не
    # раскрываем (раньше на её месте появлялась ложная точка), а запоминаем,
    # куда и как вставлен файл, — его подгружает xref_load.
    xref_blocks = {}
    for blk in doc.blocks:
        try:
            xp = blk.block.dxf.get("xref_path", "") if blk.block else ""
        except Exception:
            xp = ""
        # Ссылка, геометрия которой уже лежит в самом файле (так бывает
        # после конвертации), раскрывается как обычный блок: на Берзарина
        # в таких ссылках все сети, и пропуск их обнулял сети чертежа.
        try:
            empty = len(blk) == 0
        except Exception:
            empty = True
        if xp and empty:
            xref_blocks[blk.name] = xp
    xrefs = []
    stat["blocks"] = 0
    for e in msp:
        if e.dxftype() == "INSERT" and e.dxf.name in xref_blocks:
            try:
                base = doc.blocks[e.dxf.name].block.dxf.get("base_point", (0, 0, 0))
            except Exception:
                base = (0, 0, 0)
            xrefs.append({"name": e.dxf.name, "path": xref_blocks[e.dxf.name],
                          "insert": (e.dxf.insert.x, e.dxf.insert.y),
                          "base": (base[0], base[1]),
                          "scale": (e.dxf.get("xscale", 1.0), e.dxf.get("yscale", 1.0)),
                          "rotation": e.dxf.get("rotation", 0.0)})
            continue
        if e.dxftype() == "INSERT" and explode_blocks:
            explode(e, e.dxf.layer)
            continue
        handle(e, e.dxf.layer)

    if stat["blocks"]:
        print(f"      вставок блоков раскрыто: {stat['blocks']:,} "
              f"(объекты слоя «0» внутри блока отнесены к слою вставки)",
              flush=True)
    if stat["split"]:
        print(f"      объектов со своим цветом, отличным от слоя: "
              f"{stat['split']:,} — выделены в подслои по цвету", flush=True)
    if stat["hatch"]:
        print(f"      штриховок в чертеже: {stat['hatch']:,}, "
              f"разобрано {stat['hatch_ok']:,}"
              + (f", ошибка: {stat['hatch_err']}" if stat["hatch_err"] else ""),
              flush=True)
    else:
        print("      штриховок в чертеже нет: карта поверхностей будет "
              "построена по контурам", flush=True)
    if xrefs:
        print(f"      внешних ссылок: {len(xrefs)} (подгружаются отдельно)",
              flush=True)
    XREFS[id(doc)] = xrefs
    return doc, by_layer, fills


# Вставки внешних ссылок по чертежам: id(doc) -> [{name, path, insert, ...}]
XREFS = {}


def _ensure_layers(doc):
    for key, (name, color) in RESULT_LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=color)
    if "GREENAI" not in doc.appids:
        doc.appids.add("GREENAI")


def _norma_text(item):
    """Короткая ссылка на норматив для атрибута блока."""
    ch = item.get("checks") or []
    if not ch:
        return "ограничений в радиусе 30 м нет"
    c = ch[0]
    # в атрибуте — краткая форма ссылки: длинный текст старые просмотрщики
    # DXF обрезают на 255 символах; полная — в JSON, CSV и Markdown
    clause = (c["clause"]
              .replace("ППМ № 743-ПП, прил. 1, п. 3.6.3, табл. 3.6.1 (МГСН 1.01-99)",
                       "743-ПП п. 3.6.3")
              .replace("ППМ № 623-ПП (МГСН 1.02-02), п. 4.2.4", "623-ПП п. 4.2.4"))
    txt = (f"{c['title']}: норма {c['required_m']} м, факт {c['actual_m']} м "
           f"({c['act']}, {clause})")
    return txt if len(txt) <= 255 else txt[:252] + "..."


def write_result(doc, out_path, trees, shrubs, zone_rects=None, source_file="",
                 result_only=False):
    """Дописывает посадки в отдельные слои и сохраняет новый DXF.

    Исходные слои и геометрия не изменяются: результат живёт на слоях
    GREEN_AI_* и включается/выключается в CAD независимо.
    result_only — выгрузить только слои результата, без подосновы.
    """
    if result_only:
        import ezdxf as _ez
        doc = _ez.new("R2013", setup=False)

    doc.header["$INSUNITS"] = 6          # единицы чертежа — метры
    _ensure_layers(doc)
    _ensure_blocks(doc)
    msp = doc.modelspace()

    for it in trees:
        r = max(it.get("crown_m", 6.0) / 2.0, 0.5)
        ref = msp.add_blockref(
            TREE_BLOCK, (it["x"], it["y"]),
            dxfattribs={"layer": RESULT_LAYERS["trees"][0],
                        "xscale": r, "yscale": r, "zscale": r})
        try:
            ref.add_auto_attribs({"ID": it["id"],
                                  "PORODA": it["species_name"],
                                  "NORMA": _norma_text(it)[:250]})
        except Exception:
            pass
        msp.add_text(it["id"], height=0.5,
                     dxfattribs={"layer": RESULT_LAYERS["notes"][0]}
                     ).set_placement((it["x"] + r * 0.75, it["y"] + r * 0.75))

    for it in shrubs:
        r = max(it.get("crown_m", 0.6) / 2.0, 0.2)
        ref = msp.add_blockref(
            SHRUB_BLOCK, (it["x"], it["y"]),
            dxfattribs={"layer": RESULT_LAYERS["shrubs"][0],
                        "xscale": r, "yscale": r, "zscale": r})
        try:
            ref.add_auto_attribs({"ID": it["id"],
                                  "PORODA": it["species_name"],
                                  "NORMA": _norma_text(it)[:250]})
        except Exception:
            pass

    for rect in (zone_rects or []):
        x0, y0, x1, y1 = rect
        msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True,
                           dxfattribs={"layer": RESULT_LAYERS["zones"][0]})

    try:
        doc.saveas(out_path)
    except PermissionError:
        raise SystemExit(
            f"Файл {out_path} занят другой программой. "
            "Закройте чертёж в CAD и запустите ещё раз."
        )
    return out_path
