# -*- coding: utf-8 -*-
"""Шаг 1 сборки презентации ЛЦТ 2026: структура из шаблона организаторов.

    python3 docs/deck/lct_structure.py <шаблон.pptx> <выход.pptx>

Оставляет нужные макеты шаблона в нужном порядке, дублирует макеты,
которые используются дважды, и удаляет остальные (вводные, иконки).
Содержание заполняет второй шаг — docs/deck/lct_fill.py.
Номера — порядковые номера слайдов шаблона.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

SKILL = os.path.join(os.path.dirname(__file__), "..", "..", "tools", "deck", "skill")
# порядок итоговых слайдов: номер слайда шаблона (повтор — копия)
ORDER = [7, 8, 9, 10, 11, 24, 25, 17, 16, 20, 15, 13, 22, 24, 20, 14, 21, 23, 26, 18]


def main(src, out):
    tmp = tempfile.mkdtemp()
    zipfile.ZipFile(src).extractall(tmp)
    pres = os.path.join(tmp, "ppt", "presentation.xml")
    rels = os.path.join(tmp, "ppt", "_rels", "presentation.xml.rels")

    def slide_files():
        """Порядок слайдов: список имён файлов slideN.xml."""
        p = open(pres, encoding="utf-8").read()
        r = open(rels, encoding="utf-8").read()
        rid2file = dict(re.findall(r'Id="(rId\d+)"[^>]*Target="slides/(slide\d+\.xml)"', r))
        rid2file.update({a: b for b, a in re.findall(r'Target="slides/(slide\d+\.xml)"[^>]*Id="(rId\d+)"', r)})
        lst = re.search(r"<p:sldIdLst>(.*?)</p:sldIdLst>", p, re.S).group(1)
        return [rid2file[rid] for rid in re.findall(r'r:id="(rId\d+)"', lst)]

    base = slide_files()                    # файлы в порядке шаблона
    used, files = set(), []
    for n in ORDER:
        f = base[n - 1]
        if f in used:                       # повтор — дубликат слайда
            res = subprocess.run([sys.executable, os.path.join(SKILL, "add_slide.py"), tmp, f],
                                 capture_output=True, text=True, check=True)
            f = re.search(r"(slide\d+\.xml)", res.stdout.split("Created", 1)[-1]).group(1)
        used.add(f)
        files.append(f)

    # новый порядок в sldIdLst: только нужные слайды
    p = open(pres, encoding="utf-8").read()
    r = open(rels, encoding="utf-8").read()
    file2rid = {}
    for m in re.finditer(r"<Relationship [^>]*>", r):
        tag = m.group(0)
        t = re.search(r'Target="slides/(slide\d+\.xml)"', tag)
        if t:
            file2rid[t.group(1)] = re.search(r'Id="(rId\d+)"', tag).group(1)
    lst = re.search(r"<p:sldIdLst>(.*?)</p:sldIdLst>", p, re.S).group(1)
    by_rid = {re.search(r'r:id="(rId\d+)"', e).group(1): e
              for e in re.findall(r"<p:sldId [^>]*/>", lst)}
    new = "".join(by_rid[file2rid[f]] for f in files)
    p = p.replace(lst, new)
    open(pres, "w", encoding="utf-8").write(p)

    subprocess.run([sys.executable, os.path.join(SKILL, "clean.py"), tmp], check=True,
                   capture_output=True)
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for d, _, fs in os.walk(tmp):
            for f in fs:
                full = os.path.join(d, f)
                arc = os.path.relpath(full, tmp).replace(os.sep, "/")
                z.write(full, arc)
    shutil.rmtree(tmp)
    print("slides:", files)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
