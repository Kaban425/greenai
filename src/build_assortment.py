# -*- coding: utf-8 -*-
"""Разбор официальных списков ассортимента озеленения Москвы в YAML.

Исходники — два документа ДПиООС:
  «Основной ассортимент деревьев, кустарников и лиан, рекомендуемый для
   озеленения различных категорий территорий города Москвы»
  «Ассортимент перспективных видов ... для увеличения биоразнообразия»

В каждом — таблица: порода и восемь колонок допустимости по категориям
территорий, плюс примечания [1]-[7] с ограничениями (реагенты, близость
к детским площадкам, риск самосева и так далее).

    python src/build_assortment.py "Основной ассортимент.docx" \\
        "Перспективные виды.docx" -o config/assortment_moscow.yaml
"""
import argparse
import html
import re
import zipfile

import yaml

# Колонки таблицы в порядке следования
CATEGORIES = [
    ("yard", "Дворовые территории"),
    ("preschool", "Дошкольные учреждения"),
    ("school", "Школы, колледжи, спортивные учреждения"),
    ("health", "Учреждения здравоохранения и реабилитации"),
    ("highway", "Магистрали, шоссе, проспекты"),
    ("square", "Площади и общественно-деловые пространства"),
    ("park", "Парки, бульвары, скверы, набережные"),
    ("industrial", "Производственные и санитарно-защитные зоны"),
]

SECTIONS = {
    "хвойные деревья": ("tree", "хвойное"),
    "лиственные деревья": ("tree", "лиственное"),
    "хвойные кустарники": ("shrub", "хвойный"),
    "лиственные кустарники": ("shrub", "лиственный"),
    "кустарники": ("shrub", "лиственный"),
    "лианы": ("liana", "лиана"),
    "деревья": ("tree", "лиственное"),
}

YES = {"+", "＋", "±", "+/-", "+ /-"}
NO = {"-", "–", "—", "‒"}

NOTE_RE = re.compile(r"\[(\d)\]")
NUM_RE = re.compile(r"^\d{1,3}$")


def docx_lines(path):
    """Абзацы и ячейки документа в порядке чтения."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"</w:tc>", "\n", xml)
    xml = re.sub(r"</w:tr>", "\n", xml)
    text = html.unescape(re.sub(r"<[^>]+>", "", xml))
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def parse(path, source_label):
    lines = docx_lines(path)
    notes = {}
    for ln in lines:
        m = re.match(r"^\[(\d)\]\s*[-—–]\s*(.+)$", ln)
        if m:
            notes[m.group(1)] = m.group(2).strip()

    items = []
    section = ("tree", "лиственное")
    i = 0
    while i < len(lines):
        low = lines[i].lower().strip(" :")
        if low in SECTIONS:
            section = SECTIONS[low]
            i += 1
            continue

        if NUM_RE.match(lines[i]):
            # Строка таблицы: номер, название (иногда в несколько абзацев),
            # затем восемь отметок допустимости.
            j = i + 1
            name_parts = []
            marks = []
            while j < len(lines) and len(marks) < len(CATEGORIES):
                tok = lines[j]
                if tok in YES or tok in NO:
                    marks.append(tok in YES)
                elif NUM_RE.match(tok) and name_parts and not marks:
                    break              # пошла следующая строка таблицы
                elif tok.lower().strip(" :") in SECTIONS:
                    break
                else:
                    if marks:          # текст после отметок — уже не эта строка
                        break
                    name_parts.append(tok)
                j += 1

            name = " ".join(name_parts).strip()
            if name and len(marks) == len(CATEGORIES) and len(name) > 3:
                used = sorted(set(NOTE_RE.findall(name)))
                # После удаления меток [1][5] остаются висячие запятые
                # вида "Сосна обыкновенная , , (формы и сорта)".
                clean = NOTE_RE.sub("", name)
                clean = re.sub(r"\s*,\s*(?=,|\)|$)", "", clean)
                clean = re.sub(r"\s*,\s*(?=\()", " ", clean)
                clean = re.sub(r"\s*,\s*,\s*", ", ", clean)
                clean = re.sub(r"\s{2,}", " ", clean).strip(" ,")
                clean = re.sub(r"\s+\(", " (", clean)
                items.append({
                    "name": clean,
                    "kind": section[0],
                    "group": section[1],
                    "allowed": {code: marks[k]
                                for k, (code, _) in enumerate(CATEGORIES)},
                    "notes": [notes.get(n, f"примечание {n}") for n in used],
                    "source": source_label,
                })
            i = j
            continue
        i += 1
    return items


def main():
    ap = argparse.ArgumentParser(description="Официальный ассортимент -> YAML")
    ap.add_argument("docx", nargs="+")
    ap.add_argument("-o", "--out", default="config/assortment_moscow.yaml")
    args = ap.parse_args()

    all_items, seen = [], {}
    for path in args.docx:
        label = ("перспективный" if "erspektiv" in path or "перспектив" in path
                 else "основной")
        got = parse(path, label)
        print(f"{path}: пород разобрано {len(got)}")
        for it in got:
            key = it["name"].lower()
            if key in seen:
                continue
            seen[key] = it
            all_items.append(it)

    by_kind = {}
    for it in all_items:
        by_kind.setdefault(it["kind"], []).append(it)
    for k, v in by_kind.items():
        print(f"  {k}: {len(v)}")

    doc = {
        "categories": {c: t for c, t in CATEGORIES},
        "items": all_items,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False, width=100)
    print(f"\nЗаписано: {args.out} ({len(all_items)} пород)")


if __name__ == "__main__":
    main()
