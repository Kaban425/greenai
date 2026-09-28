# -*- coding: utf-8 -*-
"""Проверка сборки: где лежит проект, какие файлы, что читается.

    python src/selfcheck.py

Нужен, когда правки будто не применяются: показывает реальные пути, даты
файлов и содержимое каталогов — сразу видно, распаковался архив или нет.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = "2026-09-28.1"          # метка сборки, меняется с каждым архивом

MUST = [
    "src/main.py", "src/webapp.py", "src/raster_engine.py", "src/layers.py",
    "src/dxf_io.py", "src/assortment.py", "src/validate.py", "src/charts.py",
    "src/pdf_import.py", "src/viewer.py", "src/report.py",
    "src/build_assortment.py", "src/selfcheck.py",
    "config/norms.yaml", "config/species.yaml", "config/assortment_moscow.yaml",
    "requirements.txt",
]


def main():
    ok = True
    print(f"Сборка: {BUILD}")
    print(f"Папка проекта: {ROOT}")
    print(f"Python: {sys.version.split()[0]}  ({sys.executable})\n")

    print("ФАЙЛЫ")
    for rel in MUST:
        p = ROOT / rel
        if p.is_file():
            when = time.strftime("%d.%m %H:%M", time.localtime(p.stat().st_mtime))
            print(f"  есть   {rel:<34} {p.stat().st_size:>8,} Б  {when}")
        else:
            ok = False
            print(f"  НЕТ    {rel}")

    print("\nБИБЛИОТЕКИ")
    for mod in ("numpy", "scipy", "shapely", "ezdxf", "yaml", "pypdf",
                "fastapi", "uvicorn", "multipart"):
        try:
            m = __import__(mod)
            print(f"  есть   {mod:<12} {getattr(m, '__version__', '')}")
        except Exception as e:
            ok = False
            print(f"  НЕТ    {mod:<12} {e}")

    print("\nКАТАЛОГИ ПОРОД")
    sys.path.insert(0, str(ROOT / "src"))
    try:
        import yaml
        own = yaml.safe_load((ROOT / "config" / "species.yaml").read_text("utf-8"))
        print(f"  свой каталог: деревьев {len(own.get('trees', []))}, "
              f"кустарников {len(own.get('shrubs', []))}")
    except Exception as e:
        ok = False
        print(f"  свой каталог не читается: {e}")
    try:
        import assortment
        cat, cats = assortment.load_official(
            str(ROOT / "config" / "assortment_moscow.yaml"), None)
        print(f"  официальный: деревьев {len(cat['trees'])}, "
              f"кустарников {len(cat['shrubs'])}, категорий {len(cats)}")
        hw, _ = assortment.load_official(
            str(ROOT / "config" / "assortment_moscow.yaml"), "highway")
        print(f"  магистрали:  деревьев {len(hw['trees'])}, "
              f"кустарников {len(hw['shrubs'])}")
    except Exception as e:
        ok = False
        print(f"  официальный каталог не читается: {e}")

    print("\nПАПКИ ДАННЫХ")
    for d in ("input", "output"):
        p = ROOT / d
        n = len(list(p.iterdir())) if p.is_dir() else -1
        print(f"  {d:<8} {'есть' if n >= 0 else 'НЕТ'}  объектов: {max(n, 0)}")

    print("\nИТОГ:", "всё на месте" if ok else "ЕСТЬ ПРОБЛЕМЫ, см. строки НЕТ выше")
    if ok:
        print("Запуск интерфейса:  python src/webapp.py")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
