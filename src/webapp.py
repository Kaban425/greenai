# -*- coding: utf-8 -*-
"""Веб-интерфейс сервиса: запуск расчёта из браузера.

    python src/webapp.py

Открывается на http://127.0.0.1:8000, документация API — на /docs.

Расчёт выполняется тем же самым src/main.py, что и из командной строки:
веб-интерфейс только собирает аргументы и показывает журнал. Так результат
из браузера и из консоли всегда совпадает, а демонстрацию можно провести
любым способом.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse,
                               RedirectResponse)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
IN_DIR = ROOT / "input"
OUT_DIR = ROOT / "output"
IN_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(exist_ok=True)

ALLOWED = {".dxf", ".dwg", ".pdf"}
BUILD = "2026-09-28.1"          # метка сборки: видна в шапке страницы

app = FastAPI(
    title="GreenAI",
    description="Генеративный дизайн городского озеленения: "
                "DXF или векторный PDF на входе, план посадок с обоснованием "
                "по СП 42.13330.2016 на выходе.",
    version="1.0",
)

JOBS = {}
LOCK = threading.Lock()


# --------------------------------------------------------------------- #
#  Запуск расчёта
# --------------------------------------------------------------------- #

def _pipeline(job_id, src_name, opts):
    """Прогон: при необходимости PDF -> DXF, затем основной расчёт."""
    job = JOBS[job_id]

    def log(line):
        with LOCK:
            job["log"].append(line.rstrip())

    def run(cmd):
        log("$ " + " ".join(cmd[1:]))
        # Дочерний процесс по умолчанию пишет в кодировке консоли Windows,
        # из-за чего русский текст в журнале превращался в мусор.
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", bufsize=1,
                             env=env)
        for line in p.stdout:
            log(line)
        return p.wait()

    try:
        src = IN_DIR / src_name
        stem = Path(src_name).stem
        outdir = OUT_DIR / f"{stem}_{opts['scenario']}"

        dxf = src
        if src.suffix.lower() == ".pdf":
            dxf = IN_DIR / f"{stem}.dxf"
            code = run([sys.executable, str(SRC / "pdf_import.py"), str(src),
                        "--pages", str(opts["page"]), "-o", str(dxf)])
            if code != 0:
                raise RuntimeError("не удалось извлечь геометрию из PDF")

        cmd = [sys.executable, str(SRC / "main.py"),
               "--input", str(dxf), "--outdir", str(outdir),
               "--scenario", opts["scenario"], "--mode", opts["mode"],
               "--surface", opts["surface"], "--cell", str(opts["cell"]),
               "--spacing", str(opts["spacing"]),
               "--tree", opts.get("trees", "all"),
               "--shrub", opts.get("shrubs", "all")]
        if opts.get("north"):
            cmd += ["--north", str(opts["north"])]
        if opts.get("territory"):
            cmd += ["--territory", opts["territory"]]
        # 0 передаём явно: это «без ограничения», а не «не задано»
        cmd += ["--trees-per-ha", str(opts.get("trees_per_ha") or 0)]
        if opts.get("max_trees"):
            cmd += ["--max-trees", str(opts["max_trees"])]
        if opts.get("validate"):
            cmd.append("--validate")
        if opts.get("plan_only"):
            cmd.append("--plan-only")
        if opts.get("force"):
            cmd.append("--force")
        if opts.get("llm"):
            cmd.append("--llm")
        if opts.get("train"):
            cmd.append("--train")
        if opts.get("ignore_boundary"):
            cmd.append("--ignore-boundary")
        if opts.get("learned"):
            cmd.append("--learned")
        if opts.get("no_shrubs"):
            cmd.append("--no-shrubs")
        if opts.get("crown_correction"):
            cmd.append("--crown-correction")
        if opts.get("crown_in_site"):
            cmd.append("--crown-in-site")

        code = run(cmd)
        with LOCK:
            job["status"] = "done" if code == 0 else "error"
            job["outdir"] = outdir.name
    except Exception as e:
        log(f"ОШИБКА: {e}")
        with LOCK:
            job["status"] = "error"
    finally:
        with LOCK:
            job["finished"] = time.time()


# --------------------------------------------------------------------- #
#  API
# --------------------------------------------------------------------- #

@app.get("/api/version", summary="Версия сборки и пути")
def api_version():
    """Позволяет убедиться, что работает свежий код, а не старый из памяти."""
    return {"build": BUILD, "root": str(ROOT),
            "webapp_mtime": time.strftime(
                "%d.%m %H:%M",
                time.localtime((SRC / "webapp.py").stat().st_mtime))}


@app.get("/api/inputs", summary="Файлы, готовые к анализу")
def api_inputs():
    """Файлы из папки input. Найденное в data подхватывается тоже:
    так не приходится перекладывать исходники вручную."""
    # Только папка input: всё, что сервис анализирует, лежит в одном месте.
    # Служебные файлы конвертации (*_oda.dxf) в список не попадают.
    files = []
    for p in sorted(IN_DIR.iterdir(), key=lambda q: -q.stat().st_mtime):
        if (p.is_file() and p.suffix.lower() in ALLOWED
                and not p.stem.endswith("_oda")):
            files.append({"name": p.name,
                          "size_mb": round(p.stat().st_size / 1e6, 1),
                          "folder": "input",
                          "when": time.strftime("%d.%m %H:%M",
                                                time.localtime(p.stat().st_mtime))})
    return files


@app.get("/api/species", summary="Каталог пород")
def api_species(territory: str = ""):
    """Породы для выбора в интерфейсе.

    Источников два: официальный ассортимент ДПиООС (с допустимостью по
    категориям территорий) и собственный каталог config/species.yaml.
    Пустым ответ не бывает: если один источник не читается, берётся другой,
    а причина возвращается в поле diag — иначе на экране просто пустота
    и непонятно, что чинить.
    """
    import sys as _sys
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    import yaml

    diag = []
    cat = {"trees": [], "shrubs": []}
    cats = {}
    official_path = ROOT / "config" / "assortment_moscow.yaml"
    own_path = ROOT / "config" / "species.yaml"

    def try_official(terr):
        nonlocal cats
        import assortment
        c, names = assortment.load_official(str(official_path), terr or None)
        cats = names or cats
        return c

    if territory:
        try:
            cat = try_official(territory)
            diag.append(f"официальный ассортимент, категория {territory}")
        except Exception as e:
            diag.append(f"официальный список не прочитан: {e}")
    else:
        try:
            with own_path.open(encoding="utf-8") as f:
                own = yaml.safe_load(f) or {}
            if own.get("trees") or own.get("shrubs"):
                cat = own
                diag.append(f"свой каталог {own_path.name}")
            else:
                diag.append(f"{own_path.name} пуст")
        except Exception as e:
            diag.append(f"{own_path.name} не прочитан: {e}")
        try:
            import assortment
            _, cats = assortment.load_official(str(official_path), None)
        except Exception as e:
            diag.append(f"категории не прочитаны: {e}")

    if not cat.get("trees") and not cat.get("shrubs"):
        # Последний рубеж: полный официальный список без фильтра.
        try:
            cat = try_official(None)
            diag.append("подставлен полный официальный список")
        except Exception as e:
            diag.append(f"резервный источник не сработал: {e}")

    def rows(kind):
        out = []
        for x in cat.get(kind, []) or []:
            out.append({
                "id": x.get("id", ""), "name": x.get("name", ""),
                "latin": x.get("latin", ""), "genus": x.get("genus", ""),
                "family": x.get("family", ""), "group": x.get("group", ""),
                "crown_m": x.get("crown_m"),
                "interval_m": x.get("hedge_interval_m"),
                "shade": x.get("shade_tolerance", ""),
                "salt": x.get("salt_tolerance", ""),
                "notes": x.get("notes", []) or ([x["note"]] if x.get("note") else []),
            })
        return out

    res = {"trees": rows("trees"), "shrubs": rows("shrubs"),
           "categories": cats, "diag": "; ".join(diag),
           "root": str(ROOT)}
    print(f"[species] территория={territory or '-'} "
          f"деревьев={len(res['trees'])} кустарников={len(res['shrubs'])} "
          f"| {res['diag']}", flush=True)
    return res


@app.post("/api/upload", summary="Загрузить файл в папку input")
async def api_upload(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Формат {ext} не поддерживается. "
                                 f"Нужен DXF, DWG или PDF.")
    dest = IN_DIR / Path(file.filename).name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"name": dest.name, "size_mb": round(dest.stat().st_size / 1e6, 1)}


@app.post("/api/run", summary="Запустить расчёт")
def api_run(
    name: str = Form(...),
    scenario: str = Form("strict"),
    mode: str = Form("row"),
    surface: str = Form("auto"),
    cell: float = Form(0.25),
    spacing: float = Form(6.0),
    territory: str = Form(""),
    trees: str = Form("all"),
    shrubs: str = Form("all"),
    trees_per_ha: float = Form(0),
    max_trees: int = Form(0),
    north: float = Form(0),
    page: int = Form(1),
    do_validate: bool = Form(False),
    force: bool = Form(False),
    llm: bool = Form(False),
    train: bool = Form(False),
    learned: bool = Form(False),
    ignore_boundary: bool = Form(False),
    plan_only: bool = Form(False),
    no_shrubs: bool = Form(False),
    crown_correction: bool = Form(False),
    crown_in_site: bool = Form(False),
):
    if not (IN_DIR / name).exists():
        raise HTTPException(404, "Файл не найден в папке input")
    job_id = uuid.uuid4().hex[:8]
    opts = {"scenario": scenario, "mode": mode, "surface": surface,
            "territory": territory, "trees": trees or "all",
            "shrubs": shrubs or "all",
            "cell": cell, "spacing": spacing,
            "trees_per_ha": trees_per_ha or 0,
            "max_trees": max_trees or None, "page": page, "north": north,
            "validate": do_validate, "force": force, "llm": llm,
            "train": train, "learned": learned,
            "ignore_boundary": ignore_boundary,
            "plan_only": plan_only,
            "no_shrubs": no_shrubs, "crown_correction": crown_correction}
    JOBS[job_id] = {"status": "running", "log": [], "started": time.time(),
                    "name": name, "outdir": None}
    threading.Thread(target=_pipeline, args=(job_id, name, opts),
                     daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}", summary="Состояние и журнал расчёта")
def api_job(job_id: str, since: int = 0):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Задача не найдена")
    with LOCK:
        return {"status": job["status"], "outdir": job["outdir"],
                "lines": job["log"][since:], "total": len(job["log"])}


@app.get("/api/summary/{folder}", summary="Ключевые показатели расчёта")
def api_summary(folder: str):
    """Достаёт из explain.json то, что нужно видеть сразу: сколько посадок,
    какие площади, что ограничивает, как прошла сверка."""
    import json
    d = (OUT_DIR / folder).resolve()
    if not str(d).startswith(str(OUT_DIR.resolve())) or not d.is_dir():
        raise HTTPException(404, "Расчёт не найден")
    js = next((f for f in d.iterdir() if f.name.endswith("_explain.json")), None)
    if js is None:
        raise HTTPException(404, "Отчёт не найден")
    data = json.loads(js.read_text(encoding="utf-8"))
    meta = data.get("meta", {})
    a = meta.get("assortment") or {}
    plan = next((f.name for f in d.iterdir() if f.name.endswith("_plan.html")), None)
    svg = next((f.name for f in d.iterdir() if f.name.endswith("_otstupy.svg")), None)
    dxf = next((f.name for f in d.iterdir() if f.name.endswith("_GREEN_AI.dxf")), None)
    cons = sorted((meta.get("constraints_top") or []),
                  key=lambda c: -(c.get("excluded_area_m2") or 0))[:6]
    val = meta.get("validation") or {}
    return {
        "scenario": meta.get("scenario_title"),
        "site_area_m2": meta.get("site_area_m2"),
        "tree_zone_m2": meta.get("tree_zone_m2"),
        "shrub_zone_m2": meta.get("shrub_zone_m2"),
        "trees": (a.get("trees") or {}).get("total", 0),
        "shrubs": (a.get("shrubs") or {}).get("total", 0),
        "violations": len(meta.get("violations") or []),
        "species_trees": (a.get("trees") or {}).get("rows", [])[:6],
        "rule": a.get("rule_trees"),
        "constraints": [{"title": c["title"],
                         "area": c.get("excluded_area_m2"),
                         "share": c.get("share_pct")} for c in cons],
        "validation": {k: {"count": v.get("count"),
                           "on_surface_pct": v.get("on_surface_pct"),
                           "inside_tol_pct": v.get("inside_tol_pct"),
                           "match": v.get("match")}
                       for k, v in val.items() if v},
        "plan_html": plan, "chart_svg": svg, "dxf": dxf,
        "warning": meta.get("warning"),
        "classification": meta.get("classification") or {},
        "placement_source": meta.get("placement_source"),
        "unknown_top": meta.get("unknown_top") or [],
        "build": meta.get("build") or "старая сборка",
        "empty_zones": meta.get("empty_zones") or [],
        "coverage": meta.get("crown_coverage_pct"),
        "networks_found": meta.get("networks_found") or [],
        "networks_missing": meta.get("networks_missing") or [],
        "trees_by_surface": meta.get("trees_by_surface"),
        "site_source": meta.get("site_source"),
        "unknown_share_pct": meta.get("unknown_share_pct"),
        "refused": meta.get("refused") or [],
        "source": meta.get("source"),
    }


@app.post("/api/delete", summary="Удалить файл из input")
def api_delete(name: str = Form(...)):
    p = (IN_DIR / name).resolve()
    if not str(p).startswith(str(IN_DIR.resolve())) or not p.is_file():
        raise HTTPException(404, "Файл не найден в папке input")
    p.unlink()
    return {"deleted": name}


@app.post("/api/delete_result", summary="Удалить папку результата")
def api_delete_result(folder: str = Form(...)):
    import shutil as _sh
    d = (OUT_DIR / folder).resolve()
    if not str(d).startswith(str(OUT_DIR.resolve())) or not d.is_dir():
        raise HTTPException(404, "Расчёт не найден")
    _sh.rmtree(d)
    return {"deleted": folder}


@app.get("/api/layers", summary="Слои файла и их классы")
def api_layers(name: str):
    """Слои чертежа с текущим классом и числом объектов.

    Нужно, когда проектировщик сложил всё на слой «0» или дал слоям имена,
    по которым назначение не угадать: человек сопоставляет их вручную.
    """
    import sys as _sys
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    import preview
    from layers import classify_all, load_overrides

    src = IN_DIR / name
    if not src.exists():
        raise HTTPException(404, "Файл не найден в папке input")
    if src.suffix.lower() == ".pdf":
        raise HTTPException(400, "Для PDF слои задаются при импорте")

    by_layer, _ = preview.load(str(src))       # разбор из кеша, если был
    ov = load_overrides(str(ROOT / "config" / "layer_map.yaml"))
    cls = classify_all(by_layer.keys(), ov)
    lens = preview.layer_lengths(by_layer)
    rows = [{"layer": k, "count": len(v), "length_m": round(lens.get(k, 0.0)),
             "cls": cls[k], "manual": k in ov} for k, v in by_layer.items()]
    rows.sort(key=lambda r: -r["length_m"])
    return {"file": name, "layers": rows}


@app.post("/api/layers", summary="Сохранить сопоставление слоёв")
def api_layers_save(mapping: dict):
    import sys as _sys
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    from layers import save_overrides, load_overrides
    cur = load_overrides(str(ROOT / "config" / "layer_map.yaml"))
    cur.update({k: v for k, v in mapping.items() if v})
    n = save_overrides(str(ROOT / "config" / "layer_map.yaml"), cur)
    return {"saved": n}


def _plan_files(folder):
    d = (OUT_DIR / folder).resolve()
    if not str(d).startswith(str(OUT_DIR.resolve())) or not d.is_dir():
        raise HTTPException(404, "Расчёт не найден")
    pick = lambda suf: next((f for f in d.iterdir() if f.name.endswith(suf)), None)
    return d, pick("_explain.json"), pick("_plan.html"), pick("_masks.npz")


@app.get("/api/plan/{folder}", summary="План посадок для редактора")
def api_plan_get(folder: str):
    import json as _json
    import re as _re
    d, js, html, _ = _plan_files(folder)
    if js is None or html is None:
        raise HTTPException(404, "В расчёте нет плана или отчёта")
    data = _json.loads(js.read_text(encoding="utf-8"))
    edited = d / "edits.json"
    if edited.exists():
        items = _json.loads(edited.read_text(encoding="utf-8")).get("items", [])
    else:
        items = [{"id": p["id"], "type": p["type"],
                  "species_name": p.get("species_name", ""),
                  "crown_m": p.get("crown_m"), "x": p["x"], "y": p["y"],
                  "explanation": p.get("explanation", "")}
                 for p in data.get("plantings", [])]
    text = html.read_text(encoding="utf-8")
    m = _re.search(r"<svg\b.*?</svg>", text, _re.S)
    if not m:
        raise HTTPException(500, "Схема плана не найдена в HTML")
    return {"svg": m.group(0), "items": items}


@app.post("/api/plan/{folder}", summary="Сохранить правки плана")
def api_plan_save(folder: str, payload: dict):
    """Проверяет каждую посадку по маске допустимой зоны и пишет DXF.

    Правки ложатся в отдельный файл, исходный расчёт не перезаписывается:
    так всегда можно вернуться к варианту, выданному сервисом.
    """
    import json as _json
    import numpy as _np
    import sys as _sys
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    from dxf_io import write_result

    d, js, _, npz = _plan_files(folder)
    items = [it for it in (payload.get("items") or []) if not it.get("deleted")]

    violations = []
    if npz is not None:
        mk = _np.load(npz)
        x0, y0, cell = float(mk["x0"]), float(mk["y0"]), float(mk["cell"])
        for it in items:
            mask = mk["tree"] if it.get("type") == "дерево" else mk["shrub"]
            ix = int((float(it["x"]) - x0) / cell)
            iy = int((float(it["y"]) - y0) / cell)
            ok = 0 <= iy < mask.shape[0] and 0 <= ix < mask.shape[1] \
                and bool(mask[iy, ix])
            if not ok:
                kind = "деревьев" if it.get("type") == "дерево" else "кустарников"
                violations.append({"id": it.get("id"),
                                   "reason": f"вне допустимой зоны {kind}: "
                                             f"нарушен норматив отступа или "
                                             f"место на покрытии"})

    bad_ids = {v["id"] for v in violations}
    for it in items:
        if it.get("moved") or it.get("added"):
            it["explanation"] = ("Перенесено вручную" if it.get("moved")
                                 else "Добавлено вручную") + (
                "; ВНЕ ДОПУСТИМОЙ ЗОНЫ" if it.get("id") in bad_ids
                else "; место в допустимой зоне")
        it.setdefault("checks", [])
        it["x"], it["y"] = float(it["x"]), float(it["y"])

    (d / "edits.json").write_text(
        _json.dumps({"items": payload.get("items") or []}, ensure_ascii=False),
        encoding="utf-8")

    base = js.name[:-len("_explain.json")] if js else folder
    out = d / f"{base}_EDITED.dxf"
    write_result(None, str(out),
                 [i for i in items if i.get("type") == "дерево"],
                 [i for i in items if i.get("type") != "дерево"],
                 result_only=True)
    return {"violations": violations, "file": out.name,
            "url": f"/file/{folder}/{out.name}",
            "total": len(items)}


@app.get("/static/editor.js", include_in_schema=False)
def editor_js():
    return FileResponse(SRC / "static" / "editor.js",
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/results", summary="Результаты расчётов")
def api_results():
    import json as _j
    out = []
    for d in sorted(OUT_DIR.iterdir(), key=lambda p: -p.stat().st_mtime):
        if not d.is_dir():
            continue
        files = sorted(f.name for f in d.iterdir() if f.is_file())
        info = {}
        js = next((d / f for f in files if f.endswith("_explain.json")), None)
        if js is not None:
            try:
                meta = _j.loads(js.read_text(encoding="utf-8")).get("meta", {})
                a = meta.get("assortment") or {}
                info = {"trees": (a.get("trees") or {}).get("total"),
                        "shrubs": (a.get("shrubs") or {}).get("total"),
                        "warn": bool(meta.get("warning"))}
            except Exception:
                info = {}
        out.append({"dir": d.name, "files": files, **info,
                    "when": time.strftime("%d.%m %H:%M",
                                          time.localtime(d.stat().st_mtime))})
    return out


@app.get("/file/{folder}/{name}", summary="Скачать или открыть файл результата")
def api_file(folder: str, name: str):
    p = (OUT_DIR / folder / name).resolve()
    if not str(p).startswith(str(OUT_DIR.resolve())) or not p.is_file():
        raise HTTPException(404, "Файл не найден")
    media = "text/html" if p.suffix == ".html" else (
        "image/svg+xml" if p.suffix == ".svg" else "application/octet-stream")
    inline = p.suffix in (".html", ".svg", ".md")
    return FileResponse(p, media_type=media,
                        filename=None if inline else p.name)



# --------------------------------------------------------------------- #
#  Интерфейс
#
#  Сделан на обычных HTML-формах, без JavaScript. Так кнопка нажимается
#  всегда: ошибка в скрипте больше не может заблокировать всю страницу.
#  Страница расчёта обновляется мета-обновлением, тоже без скриптов.
# --------------------------------------------------------------------- #

CSS = """
/* Язык чертежа: тонкие линии, спокойная бумага, цвета условных обозначений
   топоплана. Никаких внешних шрифтов — в закрытом контуре они не загрузятся. */
:root {
  --paper:#f2f3ef; --card:#ffffff; --ink:#16202b; --ink-2:#5b6875;
  --rule:#d8dcd6; --rule-2:#eceee9;
  --leaf:#1f6f3f; --leaf-2:#e7f1e9;
  --water:#1565c0; --heat:#c62828; --gas:#b58900; --cable:#7b1fa2;
  --warn:#8a6100; --warn-bg:#fdf6e3;
}
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
/* Переход между страницами: браузер сам сшивает старую и новую,
   скриптов не требуется. Где не поддерживается — просто нет анимации. */
@view-transition { navigation: auto; }
::view-transition-old(root), ::view-transition-new(root) {
  animation-duration:.22s; }

body { margin:0; color:var(--ink);
  /* миллиметровка: язык чертежа, а не декоративная текстура */
  background-color:var(--paper);
  background-image:
    linear-gradient(0deg, rgba(31,111,63,.05) 1px, transparent 1px),
    linear-gradient(90deg, rgba(31,111,63,.05) 1px, transparent 1px),
    linear-gradient(0deg, rgba(31,111,63,.09) 1px, transparent 1px),
    linear-gradient(90deg, rgba(31,111,63,.09) 1px, transparent 1px);
  background-size:8px 8px, 8px 8px, 80px 80px, 80px 80px;
  font:15px/1.55 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  font-variant-numeric:tabular-nums; }
a { color:var(--water); }
header { background:var(--ink); color:#eef1ee;
  padding:14px clamp(16px, 2.2vw, 40px) 0; view-transition-name:site-header; }
header .inner, main > * { max-width:none; }
header .top { display:flex; gap:16px; align-items:baseline; flex-wrap:wrap; }
header h1 { margin:0; font-size:17px; font-weight:600; letter-spacing:.01em; }
header .meta { color:#9fb0a6; font-size:12.5px; }
nav { display:flex; gap:2px; margin-top:12px; flex-wrap:wrap; }
nav a { color:#c9d4cc; text-decoration:none; font-size:13.5px;
  padding:8px 14px; border-radius:7px 7px 0 0; }
nav a:hover { background:#22303d; color:#fff; }
nav a.on { background:var(--paper); color:var(--ink); font-weight:600; }
main { padding:22px clamp(16px, 2.2vw, 40px) 48px; max-width:1720px;
  margin:0 auto; }

/* На широком экране страница расчёта идёт в две колонки: слева работа,
   справа справочное. На узком колонки складываются одна под другую. */
.grid2 { display:grid; gap:16px; grid-template-columns:1fr; align-items:start; }
@media (min-width:1120px) { .grid2 { grid-template-columns:minmax(0,1.55fr)
  minmax(320px,.85fr); } }
.grid2 > * { min-width:0; }
.lede { color:var(--ink-2); font-size:14px; max-width:78ch; margin:0 0 18px; }
.card p, .card .hint { max-width:88ch; }
.card { background:var(--card); border:1px solid var(--rule); border-radius:10px;
  padding:18px 20px; margin-bottom:16px; }

/* Первый экран: фрагмент плана — то, ради чего сервис существует.
   Единственное место, где есть движение. */
.hero { position:relative; overflow:hidden; padding:0;
  border-color:#cfd6cd; background:#fff; }
.hero .cap { padding:18px 20px 4px; }
.hero h2 { font-size:19px; margin:0 0 6px; }
.hero p { margin:0; max-width:64ch; color:var(--ink-2); font-size:14px; }
.hero svg { display:block; width:100%; height:auto; }
.hero .legend { display:flex; gap:14px; flex-wrap:wrap; padding:0 20px 16px;
  font-size:12.5px; color:var(--ink-2); }
.hero .legend i { display:inline-block; width:10px; height:10px;
  border-radius:2px; margin-right:6px; vertical-align:baseline; }
.tree-mark { opacity:0; transform-box:fill-box; transform-origin:center;
  transform:scale(.6); }
@media (prefers-reduced-motion:no-preference) {
  .tree-mark { animation:plant .5s cubic-bezier(.2,.8,.3,1) forwards; }
  .cable-run { stroke-dasharray:7 5; animation:run 9s linear infinite; }
}
@media (prefers-reduced-motion:reduce) {
  .tree-mark { opacity:1; transform:none; }
}
@keyframes plant { to { opacity:1; transform:scale(1); } }
@keyframes run { to { stroke-dashoffset:-48; } }
.card.accent { border-left:3px solid var(--leaf); }
.card.warn { background:var(--warn-bg); border-color:#e8d9a8; color:var(--warn); }
.card.bad { background:#fdf1f0; border-color:#f0c9c6; }
h2 { font-size:15px; font-weight:600; margin:20px 0 10px; letter-spacing:0; }
h2:first-child { margin-top:0; }
.cols { display:flex; gap:20px; flex-wrap:wrap; }
.col { flex:1; min-width:270px; }
.splist { columns:1; }
@media (min-width:1500px) { .splist { columns:2; column-gap:22px; } }
label.f { display:block; margin-bottom:12px; font-size:13.5px; color:var(--ink); }
label.f .hint { display:block; }
label.f select, label.f input { width:100%; padding:8px 10px; margin-top:5px;
  border:1px solid var(--rule); border-radius:7px; font:inherit;
  background:#fff; color:var(--ink); }
label.f select:focus, label.f input:focus, button:focus-visible, a:focus-visible {
  outline:2px solid var(--leaf); outline-offset:2px; }
label.c { display:flex; gap:9px; margin-bottom:8px; font-size:13.5px;
  align-items:flex-start; line-height:1.4; }
label.c input { margin-top:3px; }
button, .btn { background:var(--leaf); color:#fff; border:0; border-radius:8px;
  padding:10px 18px; font:inherit; font-weight:600; cursor:pointer;
  text-decoration:none; display:inline-block; }
button:hover, .btn:hover { background:#17532f; }
.btn.w { background:#fff; color:var(--leaf); border:1px solid var(--leaf); }
.btn.w:hover { background:var(--leaf-2); }
.btn.b { background:var(--water); } .btn.b:hover { background:#0e4f96; }
.btn.s { padding:4px 11px; font-weight:400; font-size:13px; }
pre.log { background:#101820; color:#d4ded6; border-radius:9px; padding:14px 16px;
  font:12.5px/1.6 Consolas,Menlo,monospace; max-height:460px; overflow:auto;
  white-space:pre-wrap; margin:0; }
table { width:100%; border-collapse:collapse; font-size:13.5px; }
th { text-align:left; font-weight:500; color:var(--ink-2); padding:7px 8px 7px 0;
  border-bottom:1px solid var(--rule); }
td { padding:7px 8px 7px 0; border-bottom:1px solid var(--rule-2);
  vertical-align:top; }
td.n, th.n { text-align:right; padding-right:0; }
.hint { color:var(--ink-2); font-size:12.5px; }
.splist { max-height:240px; overflow:auto; border:1px solid var(--rule);
  border-radius:8px; padding:8px 12px; background:#fcfdfc; }
.splist label { display:block; padding:3px 0; font-size:13px; }
.meta { color:var(--ink-2); font-size:11.5px; }
.kpi { display:flex; gap:12px; flex-wrap:wrap; margin:4px 0 14px; }
.kpi div { background:#fbfcfa; border:1px solid var(--rule); border-radius:9px;
  padding:11px 15px; min-width:118px; border-top:2px solid var(--leaf); }
.kpi b { display:block; font-size:22px; line-height:1.2; font-weight:600; }
.kpi span { font-size:12px; color:var(--ink-2); }
.chip { font-size:12.5px; padding:3px 10px; border-radius:20px;
  border:1px solid transparent; }
.chip.run { background:#fdf6e3; color:var(--warn); border-color:#e8d9a8; }
.chip.done { background:var(--leaf-2); color:var(--leaf); border-color:#bfdcc6; }
.chip.err { background:#fdf1f0; color:var(--heat); border-color:#f0c9c6; }
.steps { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 14px; }
.steps div { flex:1; min-width:104px; font-size:12.5px; text-align:center;
  padding:8px 6px; border-radius:8px; background:#eef0ec; color:var(--ink-2);
  border:1px solid var(--rule); }
.steps div.on { background:var(--leaf); color:#fff; border-color:var(--leaf); }
.steps div.ok { background:var(--leaf-2); color:var(--leaf);
  border-color:#bfdcc6; }
details { border:1px solid var(--rule); border-radius:9px; padding:10px 14px;
  margin-bottom:12px; background:#fcfdfc; }
details > summary { cursor:pointer; font-weight:600; font-size:13.5px; }
details[open] > summary { margin-bottom:10px; }
.frame { width:100%; height:520px; border:1px solid var(--rule);
  border-radius:9px; background:#fff; }
.files li { padding:5px 0; }
/* Проверка результата: светофор по признакам достоверности */
.checks { list-style:none; margin:0; padding:0; }
.checks li { display:grid; grid-template-columns:14px minmax(150px,.9fr) 2fr;
  gap:10px; align-items:baseline; padding:8px 0;
  border-bottom:1px solid var(--rule-2); font-size:13.5px; }
.checks li:last-child { border-bottom:0; }
.checks i { width:10px; height:10px; border-radius:50%; display:inline-block;
  background:var(--leaf); }
.checks .warn i { background:#e0a800; } .checks .bad i { background:var(--heat); }
.checks b { font-weight:600; }
.checks span { color:var(--ink-2); }
@media (max-width:720px) {
  .checks li { grid-template-columns:14px 1fr; }
  .checks span { grid-column:2; }
}
@media (max-width:720px) {
  main { padding:16px; } header { padding:12px 16px 0; }
  .kpi div { flex:1; }
}
@media (prefers-reduced-motion:no-preference) {
  .chip.run { animation:pulse 1.8s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity:.55; } }
}
"""



NAV = [("/", "Расчёт"), ("/preview", "Просмотр чертежа"),
       ("/layers", "Слои чертежа"),
       ("/ai", "Разметка моделью"), ("/model", "Модель посадки"),
       ("/quality", "Проверка качества"), ("/streets", "Улицы"),
       ("/docs", "API")]


def _page(title, body, active="/", lede=""):
    tabs = "".join(
        f'<a href="{href}" class="{"on" if href == active else ""}">{name}</a>'
        for href, name in NAV)
    lede_html = f'<p class="lede">{lede}</p>' if lede else ""
    return HTMLResponse(
        f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — GreenAI</title><style>{CSS}</style></head><body>
<header>
  <div class="top">
    <h1>GreenAI · проектирование озеленения</h1>
    <span class="meta">СП 42.13330.2016 табл. 9.1 · ППМ 743-ПП · ППМ 623-ПП ·
    сборка {BUILD}</span>
  </div>
  <nav>{tabs}</nav>
</header>
<main>{lede_html}{body}</main></body></html>""",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


LAST = ROOT / "config" / "last_run.json"


def _last_params():
    import json as _j
    try:
        return _j.loads(LAST.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _prefill(html, last):
    """Подставляет в форму параметры прошлого расчёта: не нужно выставлять
    одно и то же заново на каждой улице."""
    import re as _re
    if not last:
        return html
    for name in ("scenario", "mode", "surface"):
        v = last.get(name)
        if not v:
            continue
        block = _re.search(rf'<select name="{name}">(.*?)</select>', html, _re.S)
        if block:
            inner = block.group(1).replace(" selected", "")
            inner = inner.replace(f'value="{v}"', f'value="{v}" selected', 1)
            html = html.replace(block.group(1), inner, 1)
    for name in ("spacing", "cell", "north", "trees_per_ha", "page"):
        if name in last:
            html = _re.sub(rf'(name="{name}" value=")[^"]*(")',
                           lambda m: m.group(1) + str(last[name]) + m.group(2),
                           html, count=1)
    for name in ("do_validate", "plan_only", "no_shrubs", "crown_correction",
                 "crown_in_site", "ignore_boundary", "llm", "train", "learned",
                 "force"):
        if name not in last:
            continue
        pat = _re.compile(rf'(<input type="checkbox" name="{name}" value="1")'
                          rf'( checked)?')
        html = pat.sub(lambda m: m.group(1) + (" checked" if last[name] else ""),
                       html, count=1)
    return html


def _hero():
    """Фрагмент плана: газонная полоса вдоль проезжей части, кабель под ней,
    дерево с проекцией кроны и отступом. Ровно то, что сервис считает.
    Рисуется разметкой, без внешних файлов — в закрытом контуре картинки
    неоткуда взять."""
    trees = ""
    for i, x in enumerate((150, 300, 450, 600, 750)):
        d = 0.12 * i
        trees += (
            f'<g class="tree-mark" style="animation-delay:{d:.2f}s">'
            f'<circle cx="{x}" cy="78" r="34" fill="#1f6f3f" fill-opacity=".13"/>'
            f'<circle cx="{x}" cy="78" r="34" fill="none" stroke="#1f6f3f" '
            f'stroke-width="1.2"/>'
            f'<circle cx="{x}" cy="78" r="3.4" fill="#17532f"/>'
            f'<line x1="{x}" y1="78" x2="{x}" y2="126" stroke="#7b1fa2" '
            f'stroke-width="1" stroke-dasharray="3 3"/>'
            f'</g>')
    return f"""
<div class="card hero">
  <div class="cap">
    <h2>План посадок, посчитанный по нормативам</h2>
    <p>Сервис читает чертёж, строит зону, где посадка допустима, и
    расставляет деревья и кустарники. У каждой точки — расстояние до сети
    и пункт норматива, по которому она разрешена.</p>
  </div>
  <svg viewBox="0 0 900 200" role="img"
       aria-label="Фрагмент плана: газон вдоль проезжей части, кабель под
       газоном, деревья с проекцией кроны и отступом до кабеля">
    <rect x="0" y="126" width="900" height="74" fill="#e9ebe7"/>
    <rect x="0" y="122" width="900" height="5" fill="#8a9490"/>
    <rect x="0" y="18" width="900" height="104" fill="#eaf3ec"/>
    <line class="cable-run" x1="0" y1="126" x2="900" y2="126"
          stroke="#7b1fa2" stroke-width="2.2"/>
    <line x1="0" y1="18" x2="900" y2="18" stroke="#c8d2c9" stroke-width="1.4"/>
    {trees}
    <text x="14" y="150" font-size="12" fill="#5b6875">проезжая часть</text>
    <text x="14" y="40" font-size="12" fill="#5b6875">газон</text>
    <text x="640" y="118" font-size="11" fill="#7b1fa2">кабель · отступ 2 м</text>
  </svg>
  <div class="legend">
    <span><i style="background:#1f6f3f"></i>проекция кроны</span>
    <span><i style="background:#7b1fa2"></i>кабель связи и силовой</span>
    <span><i style="background:#8a9490"></i>бортовой камень</span>
    <span><i style="background:#eaf3ec;border:1px solid #c8d2c9"></i>зона посадки</span>
  </div>
</div>"""


def _species_lists(territory):
    """Списки пород для формы. Возвращает (html_деревья, html_кусты, подпись)."""
    data = api_species(territory or "")
    def block(kind):
        rows = data.get(kind) or []
        if not rows:
            return '<div class="hint">каталог пуст: %s</div>' % data.get("diag", "")
        out = []
        for x in rows:
            meta = x.get("group") or x.get("family") or ""
            if x.get("crown_m"):
                meta += f" · крона {x['crown_m']} м"
            note = ""
            if x.get("notes"):
                note = f' <span class="meta" title="{" • ".join(x["notes"])[:400]}">· ограничения</span>'
            out.append(
                f'<label><input type="checkbox" name="{"trees" if kind=="trees" else "shrubs"}"'
                f' value="{x["id"]}"> {x["name"]} <span class="meta">{meta}</span>{note}</label>')
        return '<div class="splist">' + "".join(out) + "</div>"
    return block("trees"), block("shrubs"), data.get("diag", ""), data.get("categories", {})


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(territory: str = "", selected: str = ""):
    files = api_inputs()
    if files:
        last = _last_params()
        pick = selected or last.get("name", "")
        opts = "".join(
            f'<option value="{f["name"]}"{" selected" if f["name"] == pick else ""}>'
            f'{f["name"]} — {f["size_mb"]} МБ, {f["when"]}</option>' for f in files)
        file_field = f'<label class="f">Файл для анализа<select name="name">{opts}</select></label>'
    else:
        file_field = ('<div class="hint">Папка input пуста. Загрузите файл '
                      'ниже или положите DXF, DWG либо PDF в папку input.</div>')

    own = [f for f in files if f["folder"] == "input"]
    files_html = "".join(
        f'<div style="display:flex;justify-content:space-between;'
        f'align-items:center;padding:4px 0;border-bottom:1px solid #f0f1f3">'
        f'<span>{f["name"]} <span class="hint">{f["size_mb"]} МБ · {f["when"]}</span></span>'
        f'<span style="display:flex;gap:6px">'
        + (f'<a class="btn b s" href="/preview?name={quote(f["name"])}">просмотр</a>'
           if not f["name"].lower().endswith(".pdf") else "")
        + f'<form method="post" action="/delete" style="margin:0">'
        f'<input type="hidden" name="name" value="{f["name"]}">'
        f'<button class="btn w" style="padding:3px 10px;font-weight:400">'
        f'удалить</button></form></span></div>' for f in own
    ) or '<div class="hint">папка input пуста</div>'

    trees_html, shrubs_html, diag, cats = _species_lists(territory)
    cat_opts = '<option value="">свой каталог (config/species.yaml)</option>'
    for code, name in cats.items():
        sel = " selected" if code == territory else ""
        cat_opts += f'<option value="{code}"{sel}>{name}</option>'

    results = api_results()
    res_html = "".join(
        f'<div style="padding:6px 0;border-bottom:1px solid #f0f1f3">'
        f'<a href="/summary/{d["dir"]}">{d["dir"]}</a> '
        f'<span class="hint">{d["when"]}'
        + (f' · деревьев {d["trees"]}, кустарников {d["shrubs"]}'
           if d.get("trees") is not None else "")
        + (' · <b style="color:#8a6100">неполная подоснова</b>'
           if d.get("warn") else "")
        + '</span> '
        f'<form method="post" action="/delete_result" style="display:inline">'
        f'<input type="hidden" name="folder" value="{d["dir"]}">'
        f'<button class="btn w" style="padding:2px 9px;font-weight:400">'
        f'удалить</button></form></div>' for d in results[:12]
    ) or '<div class="hint">пока пусто</div>'

    return _page("Расчёт", _prefill(f"""
{_hero()}
<div class="card accent">
  <h2>Загрузить файл</h2>
  <form method="post" action="/upload" enctype="multipart/form-data">
    <input type="file" name="file" accept=".dxf,.dwg,.pdf" required>
    <button type="submit">Загрузить</button>
  </form>
  <div class="hint">DXF, DWG или векторный PDF генплана.</div>
  <h2>Файлы в папке input</h2>
  {files_html}
</div>

<div class="grid2">
<div>
<form method="post" action="/run">
<div class="card">
  <h2>1. Что считаем</h2>
  {file_field}
  <div class="cols">
    <div class="col">
      <label class="f">Сценарий нормирования<select name="scenario">
        <option value="strict">Строгое соблюдение таблицы 9.1</option>
        <option value="root_barrier">С корнезащитным экраном</option>
        <option value="practice">Практика проектов пилота (не норматив)</option>
      </select></label>
      <label class="f">Схема размещения<select name="mode">
        <option value="row">По форме зоны: полоса — ряд, пятно — группа</option>
        <option value="model">Моделью по всей допустимой зоне</option>
        <option value="street">Вдоль проезжей части</option>
        <option value="grove">Равномерно по зоне</option>
        <option value="mixed">Компромисс</option>
      </select></label>
      <label class="f">Поверхность посадки — где растение может расти
        <select name="surface">
        <option value="auto">Автоматически: газоны, иначе остаток</option>
        <option value="lawn">Только внутри контуров газонов</option>
        <option value="green">Всё, что не асфальт, плитка и здания</option>
      </select></label>
    </div>
    <div class="col">
      <label class="f">Шаг деревьев, м — расстояние между стволами в ряду;
        крона взрослой липы 8 м, поэтому в практике 8–12
        <input type="number" name="spacing" value="{float(_load_weights().get("spacing_m", 6)):.1f}"
         step="0.5" min="3"></label>
      <label class="f">Ячейка сетки, м — разрешение расчёта: мельче точнее,
        но дольше
        <input type="number" name="cell" value="0.25" step="0.05" min="0.1"></label>
      <label class="f">Плотность, шт/га — потолок числа деревьев;
        0 снимает ограничение и сажает по максимуму
        <input type="number" name="trees_per_ha" value="0" step="10" min="0"></label>
      <label class="f">Север, ° по часовой от оси Y — для расчёта теней
        <input type="number" name="north" value="0" step="5" min="-180"
         max="360"></label>
      <label class="f">Страница PDF — только для PDF: у каждого листа своя
        система координат
        <input type="number" name="page" value="1" min="1"></label>
    </div>
  </div>

  <h2>2. Дополнительно</h2>
  <label class="c"><input type="checkbox" name="do_validate" value="1" checked>
    сверить с проектными посадками из этого же чертежа, если они в нём есть
    (отдельный файл эталона не нужен)</label>
  <label class="c"><input type="checkbox" name="plan_only" value="1" checked>
    отдельный DXF только с посадками</label>
  <label class="c"><input type="checkbox" name="no_shrubs" value="1">
    без кустарников</label>
  <label class="c"><input type="checkbox" name="ignore_boundary" value="1">
    не ограничивать посадку границей работ (сажать на всех газонах чертежа)</label>
  <label class="c"><input type="checkbox" name="train" value="1">
    обучить модель выбора места на проектных посадках этого чертежа
    (общую модель по многим улицам это не затрёт — сохранится отдельно)</label>
  <label class="c"><input type="checkbox" name="learned" value="1">
    выбирать места по обученной модели (вместо ручных весов)</label>
  <label class="c"><input type="checkbox" name="llm" value="1">
    экспериментально: дораспознать незнакомые слои локальной моделью
    (нужна Ollama; на невнятных именах слоёв помогает мало)</label>
  <label class="c"><input type="checkbox" name="force" value="1">
    считать даже при ненадёжном распознавании чертежа (результат неполный)</label>
  <label class="c"><input type="checkbox" name="crown_in_site" value="1">
    вписывать всю крону в границу работ (строже практики: обычно крона
    нависает над тротуаром)</label>
  <label class="c"><input type="checkbox" name="crown_correction" value="1">
    увеличить отступы под крупную крону</label>

  <details><summary>Ассортимент пород — необязательно</summary>
  <input type="hidden" name="territory" value="{territory}">
  <div class="hint">Ничего не отмечено — берётся весь каталог.
  Отмеченные распределяются по правилу 10-20-30: не более 10% вида,
  20% рода, 30% семейства. {diag}</div>
  <div class="cols">
    <div class="col"><h2>Деревья</h2>{trees_html}</div>
    <div class="col"><h2>Кустарники</h2>{shrubs_html}</div>
  </div>
  </details>

  <p><button type="submit">Рассчитать план</button></p>
</div>
</form>
</div>

<div>
<div class="card">
  <h2>Категория территории</h2>
  <form method="get" action="/">
    <label class="f">Официальный ассортимент ДПиООС
      <select name="territory">{cat_opts}</select></label>
    <button class="btn w" type="submit">Применить</button>
  </form>
</div>

<div class="card">
  <h2>Прошлые расчёты</h2>{res_html}
</div>
</div>
</div>
""", _last_params()), active="/")


@app.post("/delete", include_in_schema=False)
def delete_form(name: str = Form(...)):
    try:
        api_delete(name=name)
    except HTTPException:
        pass
    return RedirectResponse("/", status_code=303)


@app.post("/delete_result", include_in_schema=False)
def delete_result_form(folder: str = Form(...)):
    try:
        api_delete_result(folder=folder)
    except HTTPException:
        pass
    return RedirectResponse("/", status_code=303)


@app.get("/layers", response_class=HTMLResponse, include_in_schema=False)
def layers_page(name: str = ""):
    import sys as _sys
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    from layers import ASSIGNABLE

    files = [f for f in api_inputs() if not f["name"].lower().endswith(".pdf")]
    if not name:
        opts = "".join(f'<option value="{f["name"]}">{f["name"]}</option>'
                       for f in files)
        return _page("Слои чертежа", f"""<div class="card accent">
          <h2>Разбор слоёв чертежа</h2>
          <div class="hint">Показывает, как распознан каждый слой, и позволяет
          назначить класс вручную. Нужно, когда вся геометрия лежит на слое
          «0» или имена слоёв ничего не говорят.</div>
          <form method="get" action="/layers">
            <label class="f">Чертёж<select name="name">{opts}</select></label>
            <button type="submit">Показать слои</button>
          </form></div>""", active="/layers",
          lede="Показывает, как распознан каждый слой, и позволяет назначить "
               "класс вручную. Назначенное запоминается и работает в других "
               "чертежах с такими же именами слоёв.")

    try:
        data = api_layers(name)
    except HTTPException as e:
        return _page("Слои", f'<div class="card">{e.detail} '
                             f'<a href="/layers">назад</a></div>')

    # Путь к цели «не больше 10% без класса». Нераспознанное почти всегда
    # сосредоточено в нескольких крупных слоях: достаточно назначить их,
    # а сотню мелких можно не трогать. Считаем, какие именно. Вес слоя —
    # длина линий, как в расчёте: штамп из 49 тысяч штрихов по 4 мм по
    # числу объектов был «третью чертежа», а по длине он ничто.
    total = sum(r["length_m"] for r in data["layers"]) or 1
    unk = sorted((r for r in data["layers"] if r["cls"] == "unknown"),
                 key=lambda r: -r["length_m"])
    unk_obj = sum(r["length_m"] for r in unk)
    target = 0.10 * total
    need, left = [], unk_obj
    for r in unk:
        if left <= target:
            break
        need.append(r["layer"])
        left -= r["length_m"]
    need_set = set(need)
    if unk_obj <= target:
        goal = (f'<div class="card" style="background:#e8f5e9">Без класса '
                f'{100*unk_obj/total:.1f}% длины линий — цель «не больше 10%» '
                f'достигнута.</div>')
    else:
        goal = (f'<div class="card" style="background:#fff8e1"><b>Сейчас без '
                f'класса {100*unk_obj/total:.1f}% длины линий.</b> Назначьте класс '
                f'<b>{len(need)}</b> слоям, отмеченным оранжевым, — и '
                f'нераспознанного станет {100*left/total:.1f}%. Остальные '
                f'{len(unk) - len(need)} нераспознанных слоёв мелкие, их можно '
                f'не трогать.</div>')

    rows = ""
    for r in sorted(data["layers"], key=lambda r: (r["layer"] not in need_set,
                                                    r["cls"] != "unknown",
                                                    -r["length_m"])):
        opts = "".join(
            f'<option value="{code}"'
            f'{" selected" if code == r["cls"] else ""}>{label}</option>'
            for code, label in ASSIGNABLE)
        auto = "" if r["manual"] else f'<option value="">авто: {r["cls"]}</option>'
        if r["layer"] in need_set:
            mark = ' style="background:#ffe0b2"'
        elif r["cls"] == "unknown":
            mark = ' style="background:#fffde7"'
        else:
            mark = ""
        rows += (f'<tr{mark}><td>{r["layer"][:70]}</td>'
                 f'<td class="n">{r["length_m"]:,}</td>'
                 f'<td class="n">{r["count"]:,}</td>'
                 f'<td><select name="cls__{r["layer"]}">{auto}{opts}</select></td>'
                 f'</tr>')

    return _page("Слои чертежа", f"""{goal}
<div class="card">
  <h2>{data["file"]} — слоёв {len(data["layers"])}</h2>
  <div class="hint">Жёлтым отмечены нераспознанные. Выберите класс и
  сохраните: назначение запомнится и будет применяться к слоям с такими
  же именами в любых чертежах.</div>
  <form method="post" action="/layers">
    <input type="hidden" name="name" value="{data["file"]}">
    <table><tr><th>Слой</th><th class="n">длина, м</th><th class="n">объектов</th>
    <th>класс</th></tr>{rows}</table>
    <p><button type="submit">Сохранить сопоставление</button>
    <a class="btn b" href="/preview?name={quote(data["file"])}">Посмотреть на схеме</a>
    <a class="btn w" href="/">К расчёту</a></p>
  </form>
</div>""", active="/layers")


PREVIEW_JS = """
(function(){
  var svg = document.getElementById('pv'); if (!svg) return;
  var vb = svg.viewBox.baseVal, full = [vb.x, vb.y, vb.width, vb.height];
  function set(x, y, w, h){ svg.setAttribute('viewBox', x+' '+y+' '+w+' '+h); }
  // колесо — масштаб вокруг курсора, перетаскивание — сдвиг
  svg.addEventListener('wheel', function(e){
    e.preventDefault();
    var r = svg.getBoundingClientRect(), k = e.deltaY > 0 ? 1.25 : 0.8;
    var s = Math.max(vb.width / r.width, vb.height / r.height);
    var cx = vb.x + (e.clientX - r.left - (r.width - vb.width / s) / 2) * s;
    var cy = vb.y + (e.clientY - r.top - (r.height - vb.height / s) / 2) * s;
    set(cx - (cx - vb.x) * k, cy - (cy - vb.y) * k, vb.width * k, vb.height * k);
  }, {passive: false});
  var drag = null;
  svg.addEventListener('pointerdown', function(e){
    drag = [e.clientX, e.clientY, vb.x, vb.y]; svg.setPointerCapture(e.pointerId); });
  svg.addEventListener('pointermove', function(e){
    if (!drag) return;
    var r = svg.getBoundingClientRect();
    var s = Math.max(vb.width / r.width, vb.height / r.height);
    set(drag[2] - (e.clientX - drag[0]) * s, drag[3] - (e.clientY - drag[1]) * s,
        vb.width, vb.height);
  });
  svg.addEventListener('pointerup', function(){ drag = null; });
  document.getElementById('pv_reset').onclick = function(){ set.apply(null, full); };
  document.querySelectorAll('.pv-leg input').forEach(function(cb){
    cb.onchange = function(){
      var g = document.getElementById('pv_' + cb.value);
      if (g) g.style.display = cb.checked ? '' : 'none';
    };
  });
})();
"""


@app.get("/preview", response_class=HTMLResponse, include_in_schema=False)
def preview_page(name: str = ""):
    """Чертёж, раскрашенный по классам слоёв, — до расчёта.

    Здесь сразу видно то, что раньше обнаруживалось по деревьям посреди
    дороги: слой проезжей части без класса, лишняя граница работ в
    чертеже профиля, сети, распознанные не тем классом.
    """
    import sys as _sys
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    import preview
    from layers import load_overrides
    from viewer import STYLE

    files = [f for f in api_inputs() if not f["name"].lower().endswith(".pdf")]
    opts = "".join(f'<option value="{f["name"]}"'
                   f'{" selected" if f["name"] == name else ""}>{f["name"]}</option>'
                   for f in files)
    head = f"""<div class="card">
  <form method="get" action="/preview" style="display:flex;gap:10px;
    align-items:flex-end;flex-wrap:wrap">
    <label class="f" style="flex:1;min-width:260px;margin:0">Чертёж
      <select name="name">{opts}</select></label>
    <button type="submit">Показать</button>
  </form>
  <div class="hint" style="margin-top:8px">Первый просмотр большого чертежа
  занимает до минуты — он разбирается и запоминается; дальше страницы
  «Просмотр», «Слои» и «Разметка моделью» открываются сразу.</div>
</div>"""
    lede = ("Как распознан чертёж — до расчёта. Нераспознанные слои "
            "красные: назначьте им класс, прежде чем считать.")
    if not name:
        return _page("Просмотр чертежа", head, active="/preview", lede=lede)
    src = IN_DIR / name
    if not src.exists():
        return _page("Просмотр чертежа", head + '<div class="card bad">Файл не '
                     'найден в папке input</div>', active="/preview", lede=lede)
    try:
        by_layer, _ = preview.load(str(src))
    except Exception as e:
        return _page("Просмотр чертежа", head + f'<div class="card bad">Чертёж '
                     f'не читается: {_esc(str(e))}</div>', active="/preview",
                     lede=lede)
    ov = load_overrides(str(ROOT / "config" / "layer_map.yaml"))
    cls, _src = preview.classify(by_layer, ov)
    drawing, stats, capped = preview.svg(by_layer, cls, STYLE)
    lens = preview.layer_lengths(by_layer)

    total = sum(s["len"] for s in stats.values()) or 1.0
    legend = []
    for c in list(STYLE) + ["unknown"]:
        st = stats.get(c)
        if not st:
            continue
        label = "Не распознано" if c == "unknown" else STYLE[c][0]
        color = "#e53935" if c == "unknown" else STYLE[c][1]
        legend.append(
            f'<label class="c"><input type="checkbox" value="{c}" checked>'
            f'<i style="background:{color};width:12px;height:12px;'
            f'border-radius:2px;display:inline-block;margin-top:3px"></i>'
            f'<span>{label} <span class="hint">{st["layers"]} сл. · '
            f'{st["len"]/1000:,.1f} км · {100*st["len"]/total:.0f}%</span>'
            f'</span></label>')
    unk = sorted(((lens.get(l, 0.0), l) for l, c in cls.items()
                  if c == "unknown"), reverse=True)[:12]
    unk_rows = "".join(f'<tr><td>{_esc(l[:70])}</td>'
                       f'<td class="n">{ln:,.0f}</td></tr>' for ln, l in unk)
    share = 100 * (stats.get("unknown", {}).get("len", 0.0)) / total
    lvl = "ok" if share < 10 else ("warn" if share < 25 else "bad")
    verdict = {"ok": "распознан хорошо", "warn": "распознан частично",
               "bad": "распознан плохо — расчёт остановится"}[lvl]
    return _page("Просмотр чертежа", head + f"""
<div class="card">
  <h2>{_esc(name)}: {verdict}</h2>
  <ul class="checks"><li class="{lvl}"><i></i><b>Без класса</b>
    <span>{share:.1f}% длины линий на схеме{' (схема упрощена — чертёж очень большой)' if capped else ''}</span></li></ul>
  <div style="display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:16px;
    margin-top:12px" class="pv-wrap">
    <div class="pv-map">{drawing}</div>
    <div class="pv-leg">{''.join(legend)}
      <p><button type="button" class="btn w s" id="pv_reset">Весь чертёж</button></p>
      <div class="hint">Колесо — масштаб, перетаскивание — сдвиг.
      Снимите галочку, чтобы скрыть класс.</div>
    </div>
  </div>
  <p><a class="btn" href="/layers?name={quote(name)}">Назначить классы слоям</a>
  <a class="btn w" href="/?selected={quote(name)}">К расчёту</a></p>
</div>
{'<div class="card"><h2>Крупнейшие нераспознанные слои</h2><table><tr><th>Слой</th><th class="n">длина линий, м</th></tr>' + unk_rows + '</table></div>' if unk_rows else ''}
<style>#pv{{width:100%;height:100%;cursor:grab}}
.pv-map{{border:1px solid var(--rule);border-radius:9px;background:#fff;
  height:min(620px,70vh);overflow:hidden;touch-action:none}}
@media (max-width:900px){{.pv-wrap{{grid-template-columns:1fr !important}}
  .pv-map{{height:340px}}}}</style>
<script>{PREVIEW_JS}</script>""", active="/preview", lede=lede)


@app.post("/layers", include_in_schema=False)
async def layers_save_form(request: Request):
    form = await request.form()
    mapping = {k[5:]: v for k, v in form.items()
               if k.startswith("cls__") and v}
    if mapping:
        api_layers_save(mapping)
    return RedirectResponse(f"/layers?name={form.get('name', '')}",
                            status_code=303)


@app.get("/edit/{folder}", response_class=HTMLResponse, include_in_schema=False)
def edit_page(folder: str):
    return HTMLResponse(f"""<!DOCTYPE html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport"
 content="width=device-width, initial-scale=1">
<title>Правка плана — GreenAI</title>
<style>{CSS}
body {{ display:flex; flex-direction:column; height:100vh; }}
.ed {{ display:flex; flex:1; min-height:0; }}
#canvas {{ flex:1; background:#fff; overflow:hidden; }}
#canvas svg {{ width:100%; height:100%; cursor:crosshair; }}
.side {{ width:340px; padding:16px 18px; overflow:auto; background:#fff;
         border-left:1px solid var(--rule); }}
.tool {{ display:block; width:100%; margin:0 0 6px; padding:9px 12px;
         background:#fff; color:var(--leaf); border:1px solid var(--leaf);
         border-radius:8px; font:inherit; cursor:pointer; text-align:left; }}
.tool:hover {{ background:var(--leaf-2); }}
.tool.on {{ background:var(--leaf); color:#fff; }}
.tool:focus-visible {{ outline:2px solid var(--leaf); outline-offset:2px; }}
.pt.tree {{ fill:#2e7d32; fill-opacity:.35; stroke:#1b5e20; stroke-width:.25; }}
.pt.shrub {{ fill:#7cb342; stroke:#33691e; stroke-width:.1; }}
.pt.edited {{ stroke:#1565c0; stroke-width:.5; }}
.pt.sel {{ stroke:#e65100; stroke-width:.7; }}
.pt.bad {{ fill:#e53935; fill-opacity:.6; stroke:#b71c1c; stroke-width:.6; }}
.st {{ font-size:13px; margin:10px 0; color:var(--leaf); }}
.st.bad {{ color:var(--heat); font-weight:600; }}
.badtxt {{ color:#c62828; margin-top:6px; font-size:12.5px; }}
.exp {{ margin-top:6px; font-size:12.5px; }}
#info {{ background:#fbfcfa; border:1px solid var(--rule); border-radius:8px;
         padding:10px; min-height:70px; font-size:13px; }}
</style></head><body data-folder="{folder}">
<header>
  <div class="top">
    <h1>Правка плана · {folder}</h1>
    <span class="meta">правки сохраняются отдельным файлом, исходный расчёт
    не меняется · сборка {BUILD}</span>
  </div>
  <nav><a href="/summary/{folder}">К результату</a>
  <a href="/">К расчёту</a></nav>
</header>
<div class="ed">
  <div id="canvas"><div class="hint" style="padding:20px">загрузка схемы ...</div></div>
  <div class="side">
    <h2>Инструмент</h2>
    <button class="tool" data-mode="move">Переносить мышью</button>
    <button class="tool" data-mode="add-tree">Добавить дерево кликом</button>
    <button class="tool" data-mode="add-shrub">Добавить кустарник кликом</button>
    <button class="tool" data-mode="delete">Удалять кликом</button>
    <h2>Масштаб</h2>
    <div style="display:flex;gap:6px">
      <button class="tool" id="zin" style="text-align:center">приблизить</button>
      <button class="tool" id="zout" style="text-align:center">отдалить</button>
      <button class="tool" id="zfit" style="text-align:center">весь план</button>
    </div>
    <div class="hint">Колесо мыши — приближение к курсору. В режиме
    переноса перетаскивание пустого места двигает схему.</div>
    <h2>Выбрано</h2>
    <div id="info"></div>
    <h2>Итог</h2>
    <div id="counts" class="hint"></div>
    <p><button id="save">Сохранить и проверить нормы</button></p>
    <div id="status" class="st"></div>
    <a id="dl" class="btn w" style="display:none" href="#">Скачать DXF с правками</a>
    <h2>Обозначения</h2>
    <div class="hint">Синяя обводка — перенесено или добавлено вручную.
    Красным — после проверки место вне допустимой зоны: нарушен отступ
    от сети, здания или посадка на покрытии. Правки сохраняются в отдельный
    файл, исходный расчёт остаётся нетронутым.</div>
  </div>
</div>
<script src="/static/editor.js"></script>
</body></html>""", headers={"Cache-Control": "no-store"})


WEIGHT_LABELS = [
    ("root_space", "Объём для корней",
     "Чем выше, тем дальше от асфальта и плитки ставятся деревья."),
    ("insolation", "Освещённость",
     "Чем выше, тем сильнее модель избегает тени от зданий."),
    ("comfort", "Польза людям",
     "Чем выше, тем ближе к тротуарам: крона даёт тень пешеходам."),
    ("drainage", "Водный режим",
     "Чем выше, тем ближе к ливневой сети, где влага доступнее."),
]


def _weights_path():
    return ROOT / "config" / "placement_weights.yaml"


def _load_weights():
    import yaml
    try:
        return yaml.safe_load(_weights_path().read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


@app.get("/model", response_class=HTMLResponse, include_in_schema=False)
def model_page(msg: str = ""):
    import json as _json
    w = _load_weights()

    model = None
    mp = ROOT / "config" / "placement_model.json"
    if mp.exists():
        try:
            model = _json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            model = None

    if model:
        auc = model.get("auc_holdout") or 0
        verdict = ("уловила закономерность" if auc >= 0.75 else
                   "слабая, но лучше случайной" if auc >= 0.6 else
                   "не лучше угадывания — не используйте")
        imp = "".join(
            f'<tr><td>{r["feature"]}</td><td class="n">{r["weight"]:+.2f}</td>'
            f'<td>{"дальше или больше — лучше" if r["weight"] > 0 else "ближе или меньше — лучше"}</td></tr>'
            for r in model.get("importance", []))
        model_html = f"""
        <div class="kpi">
          <div><b>{auc:.3f}</b><span>AUC на отложенной выборке</span></div>
          <div><b>{model.get("positives", 0)}</b><span>посадок эталона</span></div>
          <div><b>{model.get("negatives", 0)}</b><span>пустых мест</span></div>
        </div>
        <div class="hint">Оценка: модель {verdict}. AUC — доля пар, где место
        посадки оценено выше пустого места. 0,5 — как монетка, 1,0 — без ошибок.</div>
        {_streets_html(model)}
        <h2>Чему модель научилась</h2>
        <table><tr><th>Условие</th><th>вес</th><th>как влияет</th></tr>{imp}</table>"""
    else:
        model_html = ('<div class="hint">Модель ещё не обучена. Выберите ниже '
                      'чертёж с проектными посадками и нажмите «Обучить».</div>')

    rows = "".join(
        f'<label class="f">{label}<span class="hint"> — {hint}</span>'
        f'<input type="number" name="{key}" step="0.05" min="0" max="5" '
        f'value="{float(w.get(key, 0.25)):.2f}"></label>'
        for key, label, hint in WEIGHT_LABELS)

    files = [f for f in api_inputs()]
    fopts = "".join(f'<option value="{f["name"]}">{f["name"]}</option>'
                    for f in files)
    note = f'<div class="card accent">{msg}</div>' if msg else ""

    return _page("Модель посадки", f"""{note}
{_layer_model_card()}

<div class="card">
  <h2>Как это работает</h2>
  <div class="hint">Модель выбирает, в какой точке допустимой зоны посадить
  дерево. Нормативы она не нарушает никогда: запретные места отсекаются до
  неё. Её дело — среди разрешённых мест выбрать лучшие. Работает двумя
  способами. <b>По знаниям</b> — по весам, которые задаёте вы ниже.
  <b>По обучению</b> — по закономерностям, выученным на решениях
  проектировщиков: чертёж с эталоном показывает, где живой человек посадил
  дерево, и модель учится отличать такие места от пустых.</div>
</div>

<div class="card">
  <h2>Обученная модель</h2>
  {model_html}
  <h2>Обучить на чертеже</h2>
  <div class="hint">Нужен чертёж с проектными посадками — слои вида
  «(ГП)Посадочное место», «!Посадки». Например, генплан Берзарина.</div>
  <form method="post" action="/model/train">
    <label class="f">Чертёж<select name="name">{fopts}</select></label>
    <button type="submit">Обучить</button>
  </form>
</div>

<div class="card">
  <h2>Знания модели</h2>
  <div class="hint">Используются, когда обученная модель выключена.
  Веса относительные: сумма может быть любой, они нормируются.</div>
  <form method="post" action="/model/weights">
    {rows}
    <label class="f">Шаг между стволами, м
      <input type="number" name="spacing_m" step="0.5" min="3" max="20"
       value="{float(w.get("spacing_m", 6)):.1f}"></label>
    <label class="c"><input type="checkbox" name="fill_remaining" value="1"
      {"checked" if w.get("fill_remaining", True) else ""}>
      досаживать всю оставшуюся допустимую площадь</label>
    <p><button type="submit">Сохранить знания</button></p>
  </form>
</div>
""", active="/model",
     lede="Модель выбирает лучшее место среди разрешённых. Нормативы она "
          "не меняет: запретные места отсекаются до неё.")


def _streets_html(model):
    """Обучение на многих улицах: как модель угадывает каждую, не видя её."""
    by = model.get("auc_by_street") or {}
    if not by:
        return ""
    files = model.get("streets") or {}
    rows = "".join(
        f'<tr><td>{_esc(s)}</td><td class="hint">{_esc(", ".join(files.get(s, []))[:90])}</td>'
        f'<td class="n" style="color:{"#2e7d32" if a >= 0.7 else "#8a6100" if a >= 0.6 else "#c62828"}">'
        f'{a:.3f}</td></tr>'
        for s, a in sorted(by.items(), key=lambda kv: -kv[1]))
    return ('<h2>Проверка по улицам</h2><div class="hint">Модель учится на всех '
            'улицах, кроме одной, и оценивается на ней — так видно, как она '
            f'поведёт себя на новой улице. Обучена {model.get("trained", "")}.</div>'
            '<table><tr><th>Улица</th><th>посадочные планы</th>'
            f'<th class="n">AUC</th></tr>{rows}</table>'
            '<div class="hint">Переобучить: <code>python src/train_placement.py '
            '&lt;папка с проектами&gt;</code></div>')


@app.get("/streets", response_class=HTMLResponse, include_in_schema=False)
def streets_page():
    """Сводка прогона по всем улицам: что посчиталось и что сломалось."""
    import json as _json
    p = ROOT / "config" / "batch_report.json"
    if not p.exists():
        body = ('<div class="card"><p>Прогона ещё не было. Запустите в папке '
                'проекта:</p><pre>python src/batch_streets.py "&lt;папка '
                '«Пилотный проект 20 улиц»&gt;"</pre></div>')
        return _page("Улицы", body, "/streets")
    rep = _json.loads(p.read_text(encoding="utf-8"))
    rows = []
    good = 0
    for r in rep.get("rows", []):
        if r.get("ok"):
            # vector_violations — посадки, которые точная проверка нашла и
            # убрала: в готовом плане их нет, это не нарушения результата
            bad = r.get("violations") or 0
            removed = r.get("vector_violations") or 0
            u = r.get("unknown_share_pct")
            lvl = "#c62828" if bad else ("#8a6100" if (u or 0) >= 10 else "#2e7d32")
            good += not bad
            status = f'<b style="color:{lvl}">{"нарушения" if bad else "посчитана"}</b>'
            detail = (f'сети: {", ".join(_cls_ru(c).lower() for c in r.get("networks") or []) or "нет"}'
                      + (f'; ссылок {r["xrefs"]}' if r.get("xrefs") else "")
                      + (f'; + файлы сетей {len(r["nets"])}' if r.get("nets") else "")
                      + (f'; точная проверка убрала посадок: {removed}' if removed else ""))
            nums = (f'<td class="n">{r.get("trees", 0)}</td>'
                    f'<td class="n">{r.get("shrubs", 0)}</td>'
                    f'<td class="n">{bad}</td>'
                    f'<td class="n">{"" if u is None else f"{u:g}%"}</td>')
        else:
            status = '<b style="color:#c62828">остановлена</b>'
            detail = _esc("; ".join(r.get("why") or [])[:220])
            nums = '<td></td><td></td><td></td><td></td>'
        rows.append(f'<tr><td>{_esc(r["street"])}<div class="hint">'
                    f'{_esc(os.path.basename(r.get("main") or ""))}</div></td>'
                    f'<td>{status}<div class="hint">{detail}</div></td>{nums}'
                    f'<td class="n">{r.get("seconds", "")}</td></tr>')
    body = (f'<div class="kpi"><div><b>{good}</b><span>улиц без нарушений</span></div>'
            f'<div><b>{len(rep.get("rows", []))}</b><span>улиц в прогоне</span></div></div>'
            f'<div class="hint">Прогон от {rep.get("updated", "")}. Нарушения — в готовом '
            'плане; посадки, которые точная проверка по исходной геометрии нашла ближе '
            'нормы, убираются из результата и указаны в итоге улицы; нераспознано — доля длины линий без '
            'класса. Повторить: <code>python src/batch_streets.py &lt;папка&gt;</code>, '
            'одну улицу — с ключом <code>--only</code>.</div>'
            '<table><tr><th>Улица и чертёж</th><th>итог</th><th class="n">деревьев</th>'
            '<th class="n">кустарников</th><th class="n">нарушений</th>'
            '<th class="n">нераспознано</th><th class="n">с</th></tr>'
            + "".join(rows) + '</table>')
    return _page("Улицы", body, "/streets",
                 lede="Расчёт по всем улицам пилота: видно, не сломала ли правка другие улицы.")


def _layer_model_card():
    import json as _j
    mp = ROOT / "config" / "layer_model.json"
    ds = ROOT / "config" / "layer_dataset.jsonl"
    n_ds = 0
    if ds.exists():
        n_ds = sum(1 for _ in ds.open(encoding="utf-8"))
    rep = {}
    if mp.exists():
        try:
            rep = _j.loads(mp.read_text(encoding="utf-8")).get("report", {})
        except Exception:
            rep = {}
    import sys as _sys
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    import layer_model as _LM
    # точность — на том пороге, с которым модель работает в расчёте
    cv = (rep.get("cv") or {}).get(str(_LM.THRESHOLD)) or {}
    n_unk = len(_unknown_names())
    status = (f'<div class="kpi">'
              f'<div><b>{n_ds:,}</b><span>примеров в выборке</span></div>'
              f'<div><b>{rep.get("examples", "—"):,}</b>'
              f'<span>разных имён</span></div>'
              f'<div><b>{cv.get("accuracy_pct", "—")}%</b>'
              f'<span>точность на незнакомых именах</span></div>'
              f'<div><b>{cv.get("coverage_pct", "—")}%</b>'
              f'<span>доля имён, на которые отвечает</span></div>'
              f'<div><b>{n_unk:,}</b><span>имён ждут ручной разметки</span></div>'
              f'</div>'
              if cv else '<div class="hint">Модель имён ещё не обучалась.</div>')
    try:
        ev = _j.loads((ROOT / "config" / "llm_eval.json").read_text(encoding="utf-8"))
    except Exception:
        ev = {}
    if ev:
        rows = "".join(
            f'<tr><td>{_esc(k)}</td><td class="n">{v["accuracy_pct"]}%</td>'
            f'<td class="n">{v["coverage_pct"]}%</td>'
            f'<td class="n">{v["dangerous"]}</td><td class="n">{v["lost"]}</td>'
            f'<td class="n">{v["n"]}</td><td class="hint">{v["date"]}</td></tr>'
            for k, v in sorted(ev.items(), key=lambda kv: -kv[1]["accuracy_pct"]))
        status += (
            '<h2>Локальные модели (Ollama)</h2>'
            '<table><tr><th>Модель</th><th class="n">точность</th>'
            '<th class="n">охват</th><th class="n">опасных</th>'
            '<th class="n">потеряно препятствий</th><th class="n">имён</th>'
            '<th>проверена</th></tr>' + rows + '</table>'
            '<div class="hint">Проверка: <code>python src/eval_llm.py --model '
            '&lt;модель&gt; --holdout</code>. Опасный ответ — газон или граница '
            'работ там, где их нет; в расчёте такие ответы отбрасываются.</div>')
    return f"""
<div class="card accent">
  <h2>Модель имён слоёв</h2>
  <div class="hint">Учится на слоях, которые уже получили класс правилом
  или вручную, и узнаёт их вариации: опечатки, сокращения, чужие префиксы.
  Выборка пополняется сама при каждом расчёте. Точность проверяется на
  именах, которых модель не видела; неуверенные ответы отбрасываются.</div>
  {status}
  <a class="btn b" href="/label">Разметить имена без класса</a>
  <form method="post" action="/model/train_layers" style="display:inline">
    <button type="submit">Обучить модель имён</button></form>
  <form method="post" action="/model/ollama" style="display:inline">
    <button class="btn w" type="submit">Собрать модель Ollama со знаниями</button>
  </form>
  <div class="hint" style="margin-top:8px">Сборка Ollama зашивает в модель
  словарь Мосгоргеотреста и размеченные примеры, веса не меняются.
  Настоящее дообучение (LoRA) — <code>run_lora.bat</code> в папке проекта,
  около часа на видеокарте; результат — модель greenai-layers-lora.</div>
</div>"""


def _unknown_names():
    """Нераспознанные имена из сбора по проектам: [(сколько раз, имя)]."""
    import sys as _sys
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    from layers import is_anonymous, classify_layer
    p = ROOT / "config" / "harvest_unknown.txt"
    out = []
    if p.exists():
        for line in p.open(encoding="utf-8"):
            n, _, name = line.rstrip("\n").partition("\t")
            # «0», «Слой1» у каждого проектировщика свои: общее назначение
            # по такому имени испортило бы все чертежи сразу. Имена, которые
            # уже знают новые правила, разметки больше не ждут.
            if (name and not is_anonymous(name)
                    and classify_layer(name) == "unknown"):
                out.append((int(n or 0), name))
    return out


@app.get("/label", response_class=HTMLResponse, include_in_schema=False)
def label_page(skip: int = 0, msg: str = ""):
    """Разметка выборки: частые имена слоёв, которых не знают правила.

    Имена собраны со всех прогнанных проектов, а не с одного чертежа.
    Назначенное работает сразу (сопоставление слоёв) и попадает в выборку
    как ручная разметка — единственный источник знаний сверх правил.
    """
    import sys as _sys
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    import layer_model as LM
    from layers import ASSIGNABLE
    names = _unknown_names()
    page = names[skip:skip + 40]
    model = LM.load(str(ROOT))
    rows = ""
    for i, (n, name) in enumerate(page):
        c, p = model.predict(name) if model else ("unknown", 0.0)
        # Подсказка не выбирается заранее: на именах, которых модель не
        # видела, она уверенно ошибается («line02» -> проезжая часть, 0,83),
        # а ручная разметка ценна ровно тем, что её сделал человек.
        opts = ('<option value="">не знаю / пропустить</option>' + "".join(
            f'<option value="{code}">{label}</option>'
            for code, label in ASSIGNABLE))
        hint = (f'{_cls_ru(c).lower()} · {p:.2f}' if c != "unknown" else "—")
        rows += (f'<tr><td>{_esc(name[:80])}</td><td class="n">{n}</td>'
                 f'<td class="hint" style="padding-left:14px">{hint}</td>'
                 f'<td><input type="hidden" name="name_{i}" value="{_esc(name)}">'
                 f'<select name="cls_{i}">{opts}</select></td></tr>')
    note = (f'<div class="card accent">{_esc(msg)}</div>' if msg else "")
    nav = (f'<a class="btn w" href="/label?skip={max(0, skip - 40)}">← предыдущие</a> '
           if skip else "") + (
           f'<a class="btn w" href="/label?skip={skip + 40}">следующие →</a>'
           if skip + 40 < len(names) else "")
    body = (f"""{note}<div class="card">
  <h2>Имена без класса: {len(names):,}</h2>
  <div class="hint">Собраны со всех прогнанных проектов, по убыванию того,
  как часто встречаются. Подсказка модели имён — для справки: на незнакомых
  именах она бывает уверенно неправа, поэтому класс выбираете вы. Назначенное
  сразу работает в расчёте для слоёв с
  таким же именем и пополняет выборку ручной разметкой.</div>
  <form method="post" action="/label">
    <input type="hidden" name="skip" value="{skip}">
    <input type="hidden" name="count" value="{len(page)}">
    <table><tr><th>Имя слоя</th><th class="n">встречается</th>
    <th style="padding-left:14px">подсказка модели</th><th>класс</th></tr>{rows}</table>
    <p><button type="submit">Сохранить отмеченные</button> {nav}</p>
  </form></div>""" if names else
            '<div class="card">Нераспознанных имён нет: соберите их командой '
            '<code>python src/harvest_layers.py &lt;папка с проектами&gt;</code>.</div>')
    return _page("Разметка выборки", body, active="/model",
                 lede="Ручная разметка — то, что учит модели сверх правил.")


@app.post("/label", include_in_schema=False)
async def label_save(request: Request):
    import sys as _sys
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    import json as _j
    from layers import load_overrides, save_overrides, ASSIGNABLE_CODES, normalize
    form = await request.form()
    got = {}
    for i in range(int(form.get("count", 0))):
        name, cls = form.get(f"name_{i}"), form.get(f"cls_{i}")
        if name and cls in ASSIGNABLE_CODES:
            got[name] = cls
    if got:
        mp = str(ROOT / "config" / "layer_map.yaml")
        ov = load_overrides(mp)
        # по очищенному имени: работает и с префиксами подосновы
        ov.update({normalize(n): c for n, c in got.items()})
        save_overrides(mp, ov)
        with (ROOT / "config" / "layer_dataset.jsonl").open("a", encoding="utf-8") as f:
            for n, c in got.items():
                if c != "ignore":
                    f.write(_j.dumps({"name": n, "cls": c, "src": "manual",
                                      "file": "разметка выборки"},
                                     ensure_ascii=False) + "\n")
        # уходят и варианты с другими префиксами: они теперь тоже распознаются
        done = {normalize(n) for n in got}
        p = ROOT / "config" / "harvest_unknown.txt"
        lines = p.open(encoding="utf-8").readlines()
        with p.open("w", encoding="utf-8") as f:
            f.writelines(l for l in lines
                         if normalize(l.rstrip("\n").partition("\t")[2]) not in done)
    skip = int(form.get("skip", 0))
    return RedirectResponse(f"/label?skip={skip}&msg=" + quote(
        f"Сохранено: {len(got)}. Чтобы модели учли разметку — «Обучить модель "
        f"имён» на странице «Модель посадки»."), status_code=303)


@app.post("/model/train_layers", include_in_schema=False)
def model_train_layers():
    import subprocess as _sp
    r = _sp.run([sys.executable, str(SRC / "train_layers.py")], cwd=str(ROOT),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace",
                env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
    lines = [l for l in (r.stdout or "").splitlines() if "порог" in l]
    msg = ("Модель имён обучена. " + " ".join(l.strip() for l in lines[-2:])
           if r.returncode == 0 else
           "Не удалось обучить: " + ((r.stdout or "") + (r.stderr or ""))[-300:])
    from urllib.parse import quote
    return RedirectResponse("/model?msg=" + quote(msg), status_code=303)


@app.post("/model/ollama", include_in_schema=False)
def model_ollama():
    import shutil as _sh
    import subprocess as _sp
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    _sp.run([sys.executable, str(SRC / "ollama_build.py")], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env)
    exe = _sh.which("ollama") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
    if not os.path.exists(exe):
        msg = ("Modelfile собран в config/Modelfile. Ollama не найдена — "
               "выполните вручную: ollama create greenai-layers -f config/Modelfile")
    else:
        r = _sp.run([exe, "create", "greenai-layers", "-f",
                     str(ROOT / "config" / "Modelfile")], cwd=str(ROOT),
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace")
        msg = ("Модель greenai-layers собрана. Расчёт будет использовать её "
               "для незнакомых слоёв." if r.returncode == 0 else
               "Ollama вернула ошибку: " + ((r.stderr or r.stdout or "")[-300:]))
    from urllib.parse import quote
    return RedirectResponse("/model?msg=" + quote(msg), status_code=303)


@app.post("/model/weights", include_in_schema=False)
async def model_weights_save(request: Request):
    import yaml
    form = await request.form()
    w = _load_weights()
    for key, _, _ in WEIGHT_LABELS:
        try:
            w[key] = max(0.0, min(5.0, float(form.get(key, w.get(key, 0.25)))))
        except ValueError:
            pass
    try:
        w["spacing_m"] = max(3.0, min(20.0, float(form.get("spacing_m", 6))))
    except ValueError:
        pass
    w["fill_remaining"] = bool(form.get("fill_remaining"))
    _weights_path().write_text(
        yaml.safe_dump(w, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return RedirectResponse("/model?msg=Знания сохранены. Следующий расчёт "
                            "будет выполнен с ними.", status_code=303)


@app.post("/model/train", include_in_schema=False)
def model_train(name: str = Form(...)):
    res = api_run(name=name, scenario="strict", mode="row", surface="auto",
                  territory="", trees="all", shrubs="all", cell=0.25,
                  spacing=6.0, trees_per_ha=0, max_trees=0, page=1,
                  north=0, force=True, llm=False, train=True, learned=False,
                  ignore_boundary=False,
                  do_validate=True, plan_only=False, no_shrubs=True,
                  crown_correction=False, crown_in_site=False)
    return RedirectResponse(f"/job/{res['job_id']}", status_code=303)


AI_CACHE = {}


@app.get("/ai", response_class=HTMLResponse, include_in_schema=False)
def ai_page(name: str = "", run: str = ""):
    """Разметка слоёв с помощью локальной модели.

    Модель предлагает класс для каждого нераспознанного слоя, с уверенностью
    и пояснением. Человек принимает или правит — и назначение уходит в
    config/layer_map.yaml, то есть работает дальше без модели. Так модель
    не решает за человека молча, а ускоряет ручную разметку.
    """
    import sys as _sys
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    import llm_layers
    from layers import ASSIGNABLE

    models = llm_layers.available()
    status = (f'Ollama запущена, модели: {", ".join(models)}' if models else
              'Ollama не запущена — откройте её из меню Пуск и обновите страницу')
    files = [f for f in api_inputs() if not f["name"].lower().endswith(".pdf")]
    fopts = "".join(f'<option value="{f["name"]}"'
                    f'{" selected" if f["name"] == name else ""}>{f["name"]}</option>'
                    for f in files)
    head = f"""<div class="card">
  <h2>Разметка слоёв моделью</h2>
  <div class="hint">{status}</div>
  <div class="hint">Модель получает нераспознанные слои и предлагает класс
  для каждого, с уверенностью и пояснением. Вы принимаете, правите или
  отклоняете. Принятое сохраняется в сопоставление слоёв и дальше работает
  без модели — в любых чертежах с такими же именами слоёв.</div>
  <form method="get" action="/ai">
    <label class="f">Чертёж<select name="name">{fopts}</select></label>
    <input type="hidden" name="run" value="1">
    <button type="submit">Спросить модель</button>
  </form>
</div>"""
    if not name or not run:
        return _page("Разметка моделью", head, active="/ai",
                     lede="Локальная модель предлагает класс для слоёв, у "
                          "которых непонятное имя. Решение остаётся за вами: "
                          "принятое сохраняется и дальше работает без модели.")

    try:
        data = api_layers(name)
    except HTTPException as e:
        return _page("Разметка моделью",
                     head + f'<div class="card bad">{e.detail}</div>',
                     active="/ai")
    unknown = [r["layer"] for r in data["layers"] if r["cls"] == "unknown"]
    counts = {r["layer"]: r["count"] for r in data["layers"]}
    if not unknown:
        return _page("Разметка моделью", head + '<div class="card">Все слои '
                     'распознаны — модель не нужна.</div>', active="/ai")

    key = (name, tuple(unknown))
    if key not in AI_CACHE:
        known = {r["layer"]: r["cls"] for r in data["layers"]
                 if r["cls"] not in ("unknown", "annotation", "ignore")}
        import sys as _s2
        import preview as _pv
        src_file = IN_DIR / name
        _bl, _ = _pv.load(str(src_file))
        geom = {l: llm_layers.describe(_bl.get(l, [])) for l in unknown}
        rows, msg = llm_layers.suggest(unknown, verbose=False, examples=known,
                                       geometry=geom,
                                       library=llm_layers.Library.load(str(ROOT)))
        AI_CACHE[key] = (rows, msg)
    rows, msg = AI_CACHE[key]
    if not rows:
        return _page("Разметка моделью",
                     head + f'<div class="card warn">{msg}</div>', active="/ai")

    rows.sort(key=lambda r: -r["confidence"])
    confident = sum(1 for r in rows if r["cls"] != "unknown" and r["confidence"] >= 0.5)
    obj_conf = sum(counts.get(r["layer"], 0) for r in rows
                   if r["cls"] != "unknown" and r["confidence"] >= 0.5)
    obj_all = sum(counts.get(r["layer"], 0) for r in rows)

    from layers import PERMISSIVE
    body = ""
    for r in rows:
        # Газон и границу работ модель не выбирает за человека: ошибка в
        # них ставит деревья на асфальт. Предложение видно, но не отмечено.
        risky = r["cls"] in PERMISSIVE
        sure = (r["cls"] != "unknown" and r["confidence"] >= 0.5
                and not risky)
        opts = ('<option value="">не назначать</option>' +
                "".join(f'<option value="{c}"{" selected" if sure and c == r["cls"] else ""}>'
                        f'{lbl}</option>' for c, lbl in ASSIGNABLE))
        bar = int(100 * max(0.0, min(1.0, r["confidence"])))
        col = "#2e7d32" if r["confidence"] >= 0.7 else (
              "#f9a825" if r["confidence"] >= 0.5 else "#c62828")
        body += (f'<tr><td>{r["layer"][:70]}</td>'
                 f'<td class="n">{counts.get(r["layer"], 0):,}</td>'
                 f'<td><div style="background:#eceff1;border-radius:4px;width:80px">'
                 f'<div style="background:{col};width:{bar}%;height:8px;'
                 f'border-radius:4px"></div></div>'
                 f'<span class="hint">{r["confidence"]:.2f}</span></td>'
                 f'<td class="hint">{r["reason"]}'
                 + (f'<br><b style="color:var(--warn)">предлагает '
                    f'«{_cls_ru(r["cls"]).lower()}» — проверьте на схеме и '
                    f'выберите вручную</b>' if risky else '') + '</td>'
                 f'<td><select name="cls__{r["layer"]}">{opts}</select></td></tr>')

    return _page("Разметка моделью", head + f"""
<div class="card">
  <h2>{name}: {len(rows)} нераспознанных слоёв</h2>
  <div class="kpi">
    <div><b>{confident}</b><span>уверенных предложений</span></div>
    <div><b>{obj_conf:,}</b><span>объектов они покрывают</span></div>
    <div><b>{obj_all:,}</b><span>объектов без класса всего</span></div>
  </div>
  <div class="hint">{msg}. Уверенные предложения (от 0,5) уже выбраны в
  списке — проверьте и сохраните. Неуверенные оставлены пустыми.</div>
  <form method="post" action="/layers">
    <input type="hidden" name="name" value="{name}">
    <table><tr><th>Слой</th><th>объектов</th><th>уверенность</th>
    <th>почему модель так решила</th><th>класс</th></tr>{body}</table>
    <p><button type="submit">Сохранить принятые</button>
    <a class="btn w" href="/layers?name={name}">Все слои чертежа</a></p>
  </form>
</div>""", active="/ai")


@app.get("/quality", response_class=HTMLResponse, include_in_schema=False)
def quality_page(run: str = ""):
    """Проверка качества на тестовой улице с известной геометрией.

    Одна кнопка — и видно, не сломало ли очередное изменение расчёт:
    нарушения, посадки вне зоны, конфликты с существующими деревьями и
    колодцами обязаны быть нулевыми.
    """
    import sys as _sys
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    intro = """<div class="card"><h2>Проверка качества</h2>
    <div class="hint">Прогоняет расчёт на тестовой улице, где заранее известно,
    что где лежит: широкий газон и узкая полоса, кабель под газоном, колодцы и
    существующие деревья на слоях с невнятными именами. Показывает, насколько
    точно сервис распознаёт чертёж и сажает.</div>
    <form method="get" action="/quality"><input type="hidden" name="run" value="1">
    <button type="submit">Запустить проверку</button></form></div>"""
    if not run:
        return _page("Проверка качества", intro, active="/quality",
                     lede="Прогон на тестовой улице, где заранее известно, "
                          "что где лежит. Показывает, не сломало ли "
                          "очередное изменение расчёт.")
    import benchmark
    r = benchmark.measure(verbose=False)
    rows = [
        ("Распознано объектов на слоях с невнятными именами",
         f'{r["recognized_objects_pct"]}%', r["recognized_objects_pct"] >= 90),
        ("Деревьев посажено", r["trees"], True),
        ("Кроны накрывают допустимую зону", f'{r["tree_coverage_pct"]}%', True),
        ("Кустарников", r["shrubs"], True),
        ("Зона «только кустарники» занята", f'{r["shrub_zone_cover_pct"]}%', True),
        ("Деревья на существующих стволах (ближе 4 м)", r["trees_on_existing"],
         r["trees_on_existing"] == 0),
        ("Посадки на колодцах", r["plants_on_wells"], r["plants_on_wells"] == 0),
        ("Нарушения нормативов", r["violations"], r["violations"] == 0),
        ("Посадки вне допустимой зоны", r["outside_zone"], r["outside_zone"] == 0),
        ("Минимальный интервал между стволами, м", r["min_spacing_m"],
         r["min_spacing_m"] >= 5.9),
    ]
    body = "".join(
        f'<tr><td>{a}</td><td class="n">{b}</td>'
        f'<td style="color:{"#2e7d32" if ok else "#c62828"}">'
        f'{"в норме" if ok else "ПРОБЛЕМА"}</td></tr>' for a, b, ok in rows)
    return _page("Проверка качества", intro +
                 f'<div class="card"><table><tr><th>Показатель</th><th>значение</th>'
                 f'<th>оценка</th></tr>{body}</table></div>', active="/quality")


@app.post("/upload", include_in_schema=False)
async def upload_form(file: UploadFile = File(...)):
    res = await api_upload(file)
    # Чертёж сначала показывается, как распознан: ошибку в слоях дешевле
    # увидеть до расчёта, чем по деревьям посреди дороги после него.
    if not res["name"].lower().endswith(".pdf"):
        return RedirectResponse(f"/preview?name={quote(res['name'])}",
                                status_code=303)
    return RedirectResponse(f"/?selected={quote(res['name'])}", status_code=303)


@app.post("/run", include_in_schema=False)
def run_form(
    name: str = Form(...),
    scenario: str = Form("strict"),
    mode: str = Form("row"),
    surface: str = Form("auto"),
    territory: str = Form(""),
    cell: float = Form(0.25),
    spacing: float = Form(6.0),
    trees_per_ha: float = Form(0),
    north: float = Form(0),
    page: int = Form(1),
    do_validate: str = Form(""),
    force: str = Form(""),
    llm: str = Form(""),
    train: str = Form(""),
    learned: str = Form(""),
    ignore_boundary: str = Form(""),
    plan_only: str = Form(""),
    no_shrubs: str = Form(""),
    crown_correction: str = Form(""),
    crown_in_site: str = Form(""),
    trees: list[str] = Form(default=[]),
    shrubs: list[str] = Form(default=[]),
):
    import json as _j
    try:
        LAST.write_text(_j.dumps({
            "name": name, "scenario": scenario, "mode": mode,
            "surface": surface, "spacing": spacing, "cell": cell,
            "north": north, "trees_per_ha": trees_per_ha, "page": page,
            "do_validate": bool(do_validate), "plan_only": bool(plan_only),
            "no_shrubs": bool(no_shrubs),
            "crown_correction": bool(crown_correction),
            "crown_in_site": bool(crown_in_site),
            "ignore_boundary": bool(ignore_boundary), "llm": bool(llm),
            "train": bool(train), "learned": bool(learned),
            "force": bool(force)}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    res = api_run(name=name, scenario=scenario, mode=mode, surface=surface,
                  territory=territory, trees=",".join(trees) or "all",
                  shrubs=",".join(shrubs) or "all", cell=cell, spacing=spacing,
                  trees_per_ha=trees_per_ha, max_trees=0, page=page,
                  north=north, force=bool(force), llm=bool(llm),
                  train=bool(train), learned=bool(learned),
                  ignore_boundary=bool(ignore_boundary),
                  do_validate=bool(do_validate), plan_only=bool(plan_only),
                  no_shrubs=bool(no_shrubs),
                  crown_correction=bool(crown_correction),
                  crown_in_site=bool(crown_in_site))
    return RedirectResponse(f"/job/{res['job_id']}", status_code=303)


@app.get("/job/{job_id}", response_class=HTMLResponse, include_in_schema=False)
def job_page(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return _page("Ход расчёта", '<div class="card bad">Расчёт не найден. '
                     '<a href="/">Вернуться к форме</a></div>', active="/")
    running = job["status"] == "running"
    refresh = ('<meta http-equiv="refresh" content="2">' if running else "")
    badge = {"running": ('run', 'считает'), "done": ('done', 'готово'),
             "error": ('err', 'ошибка')}[job["status"]]
    log = "\n".join(job["log"][-400:]) or "запуск ..."
    tail = ""
    if job["status"] == "done" and job["outdir"]:
        tail = (f'<p><a class="btn" href="/summary/{job["outdir"]}">'
                f'Посмотреть результат</a> '
                f'<a class="btn w" href="/">Новый расчёт</a></p>')
    elif not running:
        tail = '<p><a class="btn w" href="/">Вернуться</a></p>'
    return _page("Ход расчёта", f"""{refresh}
<div class="card">
  <h2>Ход расчёта <span class="chip {badge[0]}">{badge[1]}</span></h2>
  <div class="hint">Файл: {job["name"]}
  {'· страница обновляется сама каждые 2 секунды' if running else ''}</div>
  <pre class="log">{_esc(log)}</pre>
  {tail}
</div>""", active="/")


def _esc(t):
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _preview_link(source):
    """Кнопка просмотра исходного чертежа, если он ещё лежит в input."""
    if not source:
        return ""
    name = Path(source).name
    if name.endswith("_oda.dxf"):              # DWG после конвертации
        dwg = name[:-len("_oda.dxf")] + ".dwg"
        name = dwg if (IN_DIR / dwg).exists() else name
    if not (IN_DIR / name).exists() or name.lower().endswith(".pdf"):
        return ""
    return f'<a class="btn w" href="/preview?name={quote(name)}">Просмотр чертежа</a>'


def _cls_ru(code):
    """Класс слоя по-русски: в отчёте «heating» читать никто не должен."""
    import sys as _sys
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    from layers import ASSIGNABLE
    names = dict(ASSIGNABLE)
    names.update({"unknown": "не распознано",
                  "site_boundary_aux": "красные линии, граница улицы"})
    return names.get(code, code)


SITE_SOURCE_RU = {
    "site_boundary": ("ok", "по границе работ"),
    "site_boundary_aux": ("warn", "по красным линиям и границам улиц "
                                  "топоплана: границы работ нет или она не "
                                  "замкнулась"),
    "footprint": ("bad", "по следу всей геометрии: граница не замкнулась, "
                         "посадки могут выйти за проект"),
}


def _checks_html(s):
    """Светофор: можно ли верить результату и что проверить глазами.

    Каждая строка — признак, по которому раньше незаметно ломался
    результат: деревья на асфальте, участок по всему топоплану, слои
    без класса, газон от модели.
    """
    rows = []

    def add(level, title, text):
        rows.append(f'<li class="{level}"><i></i><b>{title}</b>'
                    f'<span>{text}</span></li>')

    src = s.get("site_source")
    if src in SITE_SOURCE_RU:
        lvl, txt = SITE_SOURCE_RU[src]
        add(lvl, "Участок", txt)

    ts = s.get("trees_by_surface") or {}
    total = sum(ts.values())
    if total:
        other = ts.get("прочее", 0)
        up = ts.get("незакрашенное", 0)
        lvl = "bad" if other else ("warn" if up > total / 2 else "ok")
        txt = (f"на газоне {ts.get('газон', 0)}, на незакрашенной земле {up}"
               + (f", на покрытиях или вне карты поверхностей {other}"
                  if other else ""))
        if lvl == "warn":
            txt += " — больше половины вне газонов, проверьте карту поверхностей"
        add(lvl, "Где стоят деревья", txt)

    if "vector_violation_ids" in s:
        vb = s.get("vector_violations") or []
        n = len(s["vector_violation_ids"])
        if n:
            worst = min(vb, key=lambda v: v["actual_m"] - v["required_m"])
            add("warn", "Точная проверка отступов",
                f"{n} посадок оказались ближе нормы по исходной геометрии "
                f"чертежа и убраны из результата (например, {worst['id']}: "
                f"{worst['title'].lower()} — {worst['actual_m']} м при норме "
                f"{worst['required_m']} м). Причина — грубая растровая сетка "
                f"{s.get('cell_m', 0):g} м; на участке поменьше посадок будет больше")
        else:
            add("ok", "Точная проверка отступов",
                "все посадки проверены по исходной геометрии чертежа, "
                "без растра: нарушений нет")

    u = s.get("unknown_share_pct")
    if u is not None:
        lvl = "ok" if u < 10 else ("warn" if u < 25 else "bad")
        add(lvl, "Нераспознанная геометрия",
            f"{u:g}% длины линий без класса"
            + ("" if lvl == "ok" else
               ' — <a href="/layers">назначьте классы крупнейшим слоям</a>'))

    nets = s.get("networks_found") or []
    miss = s.get("networks_missing") or []
    if nets or miss:
        lvl = "bad" if not nets else ("warn" if len(nets) < 3 else "ok")
        add(lvl, "Подземные сети",
            ("учтены: " + ", ".join(_cls_ru(c).lower() for c in nets)
             if nets else "не найдено ни одной")
            + ("; нет: " + ", ".join(_cls_ru(c).lower() for c in miss)
               if miss else ""))

    rule = s.get("rule") or {}
    if rule.get("applicable"):
        ok = all(v.get("ok") for k, v in rule.items()
                 if isinstance(v, dict) and "ok" in v)
        add("ok" if ok else "warn", "Правило 10-20-30",
            f"вид {rule['species']['share_pct']}%, род "
            f"{rule['genus']['share_pct']}%, семейство "
            f"{rule['family']['share_pct']}%"
            + ("" if ok else " — ассортимент однообразен, добавьте пород"))

    ref = s.get("refused") or []
    if ref:
        items = ", ".join(f"{_esc(r['layer'][:40])} → {_cls_ru(r['cls']).lower()}"
                          for r in ref[:5])
        add("warn", "Отклонено у модели",
            f"назначений газона или границы работ: {len(ref)} — {items}. "
            f"Если модель права — назначьте на странице «Слои чертежа»")

    if not rows:
        return ""
    return ('<div class="card"><h2>Проверка результата</h2>'
            f'<ul class="checks">{"".join(rows)}</ul></div>')


@app.get("/summary/{folder}", response_class=HTMLResponse, include_in_schema=False)
def summary_page(folder: str):
    try:
        s = api_summary(folder)
    except HTTPException:
        return _page("Результат", '<div class="card bad">Отчёт не найден. '
                     '<a href="/">Вернуться к форме</a></div>', active="/")
    f = lambda n: f"/file/{folder}/{n}"
    num = lambda v: "—" if v is None else f"{v:,.0f}".replace(",", " ")

    links = ""
    if s.get("plan_html"):
        links += f'<a class="btn" href="{f(s["plan_html"])}" target="_blank">Схема плана</a> '
    if s.get("chart_svg"):
        links += f'<a class="btn b" href="{f(s["chart_svg"])}" target="_blank">График отступов</a> '
    if s.get("dxf"):
        links += f'<a class="btn w" href="{f(s["dxf"])}">Скачать DXF</a>'

    sp = "".join(f'<tr><td>{r["name"]}</td><td class="n">{r["count"]}</td>'
                 f'<td class="n">{r["share_pct"]}%</td></tr>'
                 for r in s.get("species_trees") or [])
    cons = "".join(f'<tr><td>{c["title"]}</td><td class="n">{num(c["area"])}</td>'
                   f'<td class="n">{"—" if c["share"] is None else str(c["share"]) + "%"}</td></tr>'
                   for c in s.get("constraints") or [])
    ru = {"tree": "Деревья", "shrub": "Кустарники"}
    val = "".join(f'<tr><td>{ru.get(k, k)}</td><td class="n">{v["count"]}</td>'
                  f'<td class="n">{v["on_surface_pct"]}%</td>'
                  f'<td class="n">{v["inside_tol_pct"]}%</td></tr>'
                  for k, v in (s.get("validation") or {}).items() if v.get("count"))

    warn = ""
    if s.get("warning"):
        warn = (f'<div class="card" style="border-color:#ef9a9a;'
                f'background:#fff5f5"><b style="color:#c62828">'
                f'Результат непригоден для проектирования</b>'
                f'<div>{s["warning"]}</div></div>')
    nets = s.get("networks_found") or []
    nets_html = ('<div class="hint">Учтённые сети: '
                 + (", ".join(_cls_ru(c).lower() for c in nets) if nets else "нет")
                 + (" · выбор места: " + s["placement_source"]
                    if s.get("placement_source") else "") + "</div>")

    preview = (f'<div class="card"><h2>Схема плана</h2>'
               f'<iframe class="frame" loading="lazy" src="{f(s["plan_html"])}" '
               f'title="Схема плана"></iframe></div>'
               if s.get("plan_html") else "")

    return _page("Результат", f"""
{warn}
<div class="card">
  <h2>{folder} — {s.get("scenario") or ""}</h2>
  {nets_html}
  <div class="kpi">
    <div><b>{num(s.get("trees"))}</b><span>деревьев</span></div>
    <div><b>{num(s.get("shrubs"))}</b><span>кустарников</span></div>
    <div><b>{num(s.get("violations"))}</b><span>нарушений норм</span></div>
    <div><b>{num(s.get("site_area_m2"))}</b><span>участок, м²</span></div>
    <div><b>{num(s.get("tree_zone_m2"))}</b><span>зона деревьев, м²</span></div>
    <div><b>{num(s.get("shrub_zone_m2"))}</b><span>зона кустарников, м²</span></div>
  </div>
  <p>{links} <a class="btn b" href="/edit/{folder}">Редактировать план</a>
  {_preview_link(s.get("source"))}</p>
</div>
{_checks_html(s)}
{preview}
{'<div class="card"><h2>Что посажено</h2><table><tr><th>Порода</th><th>шт</th><th>доля</th></tr>' + sp + '</table></div>' if sp else ''}
{'<div class="card"><h2>Что ограничивает посадку</h2><table><tr><th>Объект</th><th>м²</th><th>доля зоны</th></tr>' + cons + '</table></div>' if cons else ''}
{'<div class="card"><h2>Сверка с эталонным проектом</h2><table><tr><th>Тип</th><th>эталон</th><th>в зелёной зоне</th><th>по норме</th></tr>' + val + '</table></div>' if val else ''}
{_empty_html(s.get("empty_zones") or [], s.get("coverage"))}
{_classif_html(s.get("classification") or {}, s.get("unknown_top"),
               s.get("build", ""))}
<div class="card"><a class="btn w" href="/">Новый расчёт</a></div>
""", active="/")


def _empty_html(zones, cov):
    """Почему пустуют газоны: главная причина для каждого участка."""
    if not zones and cov is None:
        return ""
    under = [z for z in zones if z.get("underplanted")]
    head = ""
    if cov is not None:
        head = (f'<div class="hint">Кроны накрывают <b>{cov}%</b> допустимой '
                f'зоны. Пустых участков газона: {len(zones)}, из них '
                f'недосадка: <b>{len(under)}</b>.</div>')
    rows = ""
    for z in zones[:20]:
        reason = z["reasons"][0]["title"] if z["reasons"] else "не ясна"
        style = ' style="background:#fff3e0"' if z.get("underplanted") else ""
        rows += (f'<tr{style}><td class="n">{z["area_m2"]:,.0f}</td>'
                 f'<td class="n">{z["max_width_m"]}</td><td>{reason}</td></tr>')
    table = ('<table><tr><th>площадь, м²</th><th>ширина, м</th>'
             '<th>главная причина</th></tr>' + rows + '</table>') if rows else ""
    note = ('<div class="hint">Оранжевым — участки, где допустимое место есть, '
            'а деревьев нет: это ошибка расстановки, пришлите её.</div>'
            if under else "")
    return (f'<div class="card"><h2>Почему пустуют газоны</h2>{head}'
            f'{table}{note}</div>')


ORDER = ["manual", "rules", "color", "geometry", "types", "colocate", "tokens",
         "labels", "model", "llm", "unknown"]


def _classif_html(c, unknown_top=None, build=""):
    """Откуда взялись классы слоёв: вклад правил, цвета и модели."""
    if not c or not c.get("by_source_objects"):
        return ""
    ru = {"manual": "назначено вручную", "rules": "правила по имени слоя",
          "color": "цвет линии", "geometry": "форма объектов",
          "types": "номер типа покрытия",
          "colocate": "соседство с распознанными линиями",
          "tokens": "слова в именах этого чертежа",
          "labels": "подписи у линий", "model": "обученная модель имён",
          "llm": "локальная модель", "unknown": "не распознано"}
    obj = c["by_source_objects"]
    lay = c.get("by_source_layers", {})
    total = sum(obj.values()) or 1
    rows = "".join(
        f'<tr><td>{ru.get(k, k)}</td><td class="n">{lay.get(k, 0)}</td>'
        f'<td class="n">{v:,}</td>'
        f'<td class="n">{100*v/total:.1f}%</td></tr>'
        for k, v in sorted(obj.items(),
                           key=lambda kv: ORDER.index(kv[0]) if kv[0] in ORDER else 99))
    extra = ""
    llm = c.get("llm_layers") or {}
    if llm:
        items = "".join(f'<tr><td>{k[:60]}</td><td>{_cls_ru(v)}</td></tr>'
                        for k, v in list(llm.items())[:25])
        extra += ('<h2>Что распознала модель</h2><table><tr><th>Слой</th>'
                  '<th>класс</th></tr>' + items + '</table>')
    col = c.get("color_layers") or {}
    if col:
        items = "".join(f'<tr><td>{k[:60]}</td><td>{_cls_ru(v)}</td></tr>'
                        for k, v in list(col.items())[:15])
        extra += ('<h2>Что распознано по цвету</h2><table><tr><th>Слой</th>'
                  '<th>класс</th></tr>' + items + '</table>')
    unk_html = ""
    if unknown_top:
        items = "".join(f'<tr><td>{r["layer"][:70]}</td>'
                        f'<td class="n">{r.get("length_m", 0):,}</td>'
                        f'<td class="n">{r["count"]:,}</td></tr>'
                        for r in unknown_top)
        unk_html = ('<h2>Крупнейшие нераспознанные слои</h2>'
                    '<div class="hint">Назначьте им класс на странице '
                    '«Слои чертежа» — обычно хватает двух-трёх, чтобы '
                    'опуститься ниже 10%.</div>'
                    '<table><tr><th>Слой</th><th class="n">длина линий, м</th>'
                    '<th class="n">объектов</th></tr>'
                    + items + '</table>')
    return (f'<div class="card"><h2>Как распознан чертёж</h2>'
            f'<div class="hint">сборка расчёта: {build}</div>'
            f'<table><tr><th>Источник</th><th>слоёв</th><th>объектов</th>'
            f'<th>доля</th></tr>{rows}</table>{extra}{unk_html}</div>')


def main():
    import webbrowser

    import uvicorn
    host = os.environ.get("GREENAI_HOST", "127.0.0.1")
    port = int(os.environ.get("GREENAI_PORT", "8000"))
    if os.environ.get("GREENAI_OPEN", "1") == "1" and host == "127.0.0.1":
        threading.Timer(1.2, lambda: webbrowser.open(
            f"http://{host}:{port}")).start()
    print(f"Сборка {BUILD}\nИнтерфейс: http://{host}:{port}\n"
          f"API: http://{host}:{port}/docs")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
