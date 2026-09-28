# -*- coding: utf-8 -*-
"""Расчёт по всем улицам пилота одним прогоном.

    python src/batch_streets.py "<папка «Пилотный проект 20 улиц»>"
    python src/batch_streets.py <папка> --only Лодочная --only Камчатская

По каждой улице выбирается главный чертёж (генплан, ГП, АПОТ, посадочный),
к нему автоматически подгружаются внешние ссылки, а если своих сетей в
нём нет — файлы сетей той же улицы («up» ДЖКХ, «Сети», «Коммуникации»).
Сводка — config/batch_report.json и страница «Улицы» на сайте: видно,
не сломала ли очередная правка другие улицы.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(ROOT, "config", "batch_report.json")
OUT = os.path.join(ROOT, "output", "batch")

MAIN_RE = re.compile(r"генплан|генеральн\w*\s+план|(^|[_ \-.])гп([_ \-.]|$)|апот|"
                     r"благоустр|посадоч|сводн\w*\s+план|^ип_", re.I)
NOT_MAIN_RE = re.compile(r"output|джкх|(^|[_ ])(tp|up|kl|pp)([_ .]|$)|xref|"
                         r"топо|геодез|^гео|гео\+|игди|сети|коммуник|дендро|штамп|"
                         r"иот|_oda|ссылк|рабочий файл|профил|разрез|акр", re.I)
NET_RE = re.compile(r"up\.dwg$|^up[_ ]|(^|[_ ])сети|коммуникац|(^|[_ ])иот\d?", re.I)
MIN_MB = 1.0


def _dwgs(folder, ext=".dwg"):
    out = []
    for d, _, fs in os.walk(folder):
        if "PaxHeader" in d or os.sep + "_" in d:
            continue
        for f in fs:
            if f.lower().endswith(ext):
                p = os.path.join(d, f)
                try:
                    mb = os.path.getsize(p) / 1e6
                except OSError:
                    continue
                if mb >= MIN_MB:
                    out.append((p, f, mb))
    return out


def pick(folder):
    """(главный чертёж, [файлы сетей]) для папки улицы."""
    files = _dwgs(folder)
    main = [x for x in files if MAIN_RE.search(x[1]) and not NOT_MAIN_RE.search(x[1])]
    if not main:
        # чертежей нет, только листы PDF (Фрунзенская): векторный генплан
        pdfs = [x for x in _dwgs(folder, ".pdf")
                if MAIN_RE.search(x[1]) and not NOT_MAIN_RE.search(x[1])]
        if pdfs:
            return max(pdfs, key=lambda x: x[2])[0], []
        return None, []

    def rank(x):
        name = x[1].lower()
        # посадочный план даёт сверку с проектировщиком — он в приоритете,
        # затем генплан; внутри — крупнее значит полнее
        return (0 if "посадоч" in name else 1 if re.search(r"генплан|генеральн|гп", name)
                else 2, -x[2])
    main.sort(key=rank)
    # сети: без дублей (один файл лежит в нескольких папках), три крупнейших
    nets, seen = [], set()
    for p, f, mb in sorted((x for x in files if NET_RE.search(x[1])),
                           key=lambda x: -x[2]):
        key = f.lower()
        if key not in seen:
            seen.add(key)
            nets.append(p)
    return main[0][0], nets[:3]


def run(street, main, nets, timeout):
    out = os.path.join(OUT, re.sub(r"[^\w\-. ]", "_", street)[:60])
    cmd = [sys.executable, os.path.join(ROOT, "src", "main.py"), "--input", main,
           "--outdir", out, "--validate"]
    t0 = time.time()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

    def once(extra):
        try:
            r = subprocess.run(cmd + extra, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=timeout, env=env, cwd=ROOT)
            return r.returncode, r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            return -1, "превышено время"
    code, log = once([])
    used_nets = []
    if nets and ("подземные сети" in log or "Найдены не все виды сетей" in log):
        # своих сетей в чертеже нет или не все — добавляем файлы сетей улицы;
        # берём повтор, если он посчитался
        code2, log2 = once([a for p in nets for a in ("--with", p)])
        if code2 == 0 or code != 0:
            code, log, used_nets = code2, log2, nets
    try:                                  # полный журнал расчёта — рядом с результатом
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "run.log"), "w", encoding="utf-8") as f:
            f.write(log)
    except OSError:
        pass
    row = {"street": street, "main": main, "nets": used_nets,
           "seconds": round(time.time() - t0), "ok": code == 0, "outdir": out}
    if code != 0:
        tail = [l for l in log.splitlines() if l.strip() and "copy process" not in l]
        why = [l.strip() for l in tail if re.search(
            r"не найдено|НЕНАДЁЖНО|ни одного|Error|ошибк|время", l)]
        row["why"] = (why or tail[-3:])[:4]
        return row
    js = [f for f in os.listdir(out) if f.endswith("_explain.json")]
    if js:
        with open(os.path.join(out, js[0]), encoding="utf-8") as f:
            data = json.load(f)
        m = data.get("meta", {})
        pl = data.get("plantings", [])
        row.update({
            "trees": sum(1 for p in pl if p.get("type") == "дерево"),
            "shrubs": sum(1 for p in pl if p.get("type") == "кустарник"),
            "violations": len(m.get("violations") or []),
            "vector_violations": len(m.get("vector_violation_ids") or []),
            "unknown_share_pct": m.get("unknown_share_pct"),
            "networks": m.get("networks_found"),
            "site_source": m.get("site_source"),
            "cell_m": m.get("cell_m"),
            "xrefs": sum(1 for l in log.splitlines() if "+ ссылка" in l),
        })
        v = m.get("validation") or {}
        tr = (v.get("tree") or {}) if isinstance(v, dict) else {}
        if tr:
            row["reference_trees"] = tr.get("n_ref") or tr.get("count")
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    rows = []
    if args.only and os.path.exists(REPORT):
        with open(REPORT, encoding="utf-8") as f:
            rows = [r for r in json.load(f).get("rows", [])
                    if not any(o.lower() in r["street"].lower() for o in args.only)]
    streets = sorted(d for d in os.listdir(args.folder)
                     if os.path.isdir(os.path.join(args.folder, d)))
    if args.only:
        streets = [s for s in streets if any(o.lower() in s.lower() for o in args.only)]
    for s in streets:
        main_dwg, nets = pick(os.path.join(args.folder, s))
        if not main_dwg:
            rows.append({"street": s, "ok": False, "why": ["нет генплана или "
                                                           "посадочного плана"]})
            print(f"{s[:40]:<40} нет главного чертежа", flush=True)
            continue
        print(f"{s[:40]:<40} {os.path.basename(main_dwg)[:50]} ...", flush=True)
        r = run(s, main_dwg, nets, args.timeout)
        rows.append(r)
        print(f"   {'готово' if r['ok'] else 'остановлен'} за {r['seconds']} с: "
              + (f"деревьев {r.get('trees')}, нарушений {r.get('violations')}, "
                 f"убрано точной проверкой {r.get('vector_violations')}, нераспознано "
                 f"{r.get('unknown_share_pct')}%" if r["ok"] else
                 "; ".join(r.get("why", []))[:200]), flush=True)
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump({"updated": time.strftime("%d.%m.%Y %H:%M"),
                       "rows": sorted(rows, key=lambda r: r["street"])}, f,
                      ensure_ascii=False, indent=1)
    ok = sum(r["ok"] for r in rows)
    print(f"\nПосчитано {ok} из {len(rows)} улиц. Сводка: config/batch_report.json")


if __name__ == "__main__":
    main()
