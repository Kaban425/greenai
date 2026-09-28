# -*- coding: utf-8 -*-
"""Шаг 2 сборки презентации ЛЦТ 2026: содержание в шаблоне организаторов.

    python3 docs/deck/lct_fill.py <структура.pptx> <выход.pptx>

Текст пишется в поля шаблона с сохранением их оформления, данные — в
диаграммы шаблона, картинки — в рамки шаблона. Поля о команде (ФИО,
контакты, история) не заполняются: там остаются подсказки шаблона.
Цифры берутся из результатов расчётов в config/.
"""
import copy
import json
import os
import sys

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.util import Emu, Inches, Pt

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
IMG = os.path.join(ROOT, "docs", "deck", "img")
PINK, PALE, LILAC, DEEP, PURPLE = "FF0053", "FFD6E4", "8A83D1", "310F53", "520978"
DARK, WHITE = "1C1D22", "FFFFFF"


def load(p, default=None):
    try:
        return json.load(open(os.path.join(ROOT, p), encoding="utf-8"))
    except Exception:
        return default


MODEL = load("config/placement_model.json", {})
BATCH = load("config/batch_report.json", {"rows": []})


def shape(slide, sid):
    for sh in slide.shapes:
        if sh.shape_id == sid:
            return sh
    raise KeyError(sid)


def put(sh, paras, size=None, color=DARK, bold=None, bullet=False):
    """Текст в поле с сохранением оформления первого абзаца и первого фрагмента.

    paras — список абзацев; абзац — строка или список фрагментов
    (текст, {"bold": .., "size": .., "color": ..}).
    """
    tf = sh.text_frame
    p0 = tf.paragraphs[0]._p
    ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    pPr = p0.find(ns + "pPr")
    r0 = p0.find(ns + "r")
    rPr = r0.find(ns + "rPr") if r0 is not None else None
    for p in list(tf.paragraphs)[1:]:
        p._p.getparent().remove(p._p)
    for r in list(p0):
        if r.tag in (ns + "r", ns + "br", ns + "fld"):
            p0.remove(r)
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        if i > 0 and pPr is not None:
            p._p.insert(0, copy.deepcopy(pPr))
        runs = para if isinstance(para, list) else [(para, {})]
        for txt, opt in runs:
            r = p.add_run()
            if rPr is not None:
                r._r.insert(0, copy.deepcopy(rPr))
            r.text = txt
            b = opt.get("bold", bold)
            if b is not None:
                r.font.bold = b
            sz = opt.get("size", size)
            if sz:
                r.font.size = Pt(sz)
            c = opt.get("color", color)
            if c:
                r.font.color.rgb = RGBColor.from_string(c)
        if not bullet:
            _no_bullet(p)


def _no_bullet(p):
    """Абзац без маркера и без висячего отступа."""
    pPr = p._p.get_or_add_pPr()
    ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    for tag in ("buNone", "buChar", "buAutoNum", "buFont", "buSzPct", "buClr"):
        for e in pPr.findall(ns + tag):
            pPr.remove(e)
    from lxml import etree
    bu = etree.Element(ns + "buNone")
    # по схеме маркер стоит перед tabLst, defRPr и extLst
    after = [e for e in pPr if e.tag in (ns + "tabLst", ns + "defRPr", ns + "extLst")]
    if after:
        after[0].addprevious(bu)
    else:
        pPr.append(bu)
    pPr.set("marL", "0")
    pPr.set("indent", "0")


def fill(sh, rows):
    """Обязательные слайды: текст подставляется в абзацы и фрагменты шаблона,
    их оформление (жирность, курсив, кегль, маркеры) не трогается.

    rows — по строке на абзац шаблона: список текстов фрагментов или None
    (абзац удаляется). Лишние тексты дописываются обычным, не жирным
    фрагментом — это значение после жирной подписи «Капитан: ».
    """
    paras = list(sh.text_frame.paragraphs)
    for p, row in zip(paras, rows):
        if row is None:
            p._p.getparent().remove(p._p)
            continue
        runs = list(p.runs)
        for r, txt in zip(runs, row):
            r.text = txt
        for txt in row[len(runs):]:
            r = p.add_run()
            if runs:
                rPr = runs[-1]._r.find(
                    "{http://schemas.openxmlformats.org/drawingml/2006/main}rPr")
                if rPr is not None:
                    r._r.insert(0, copy.deepcopy(rPr))
            r.text = txt
            r.font.bold = False
            r.font.italic = False


def title(slide, text, sid, pill=None):
    """Заголовок; розовая плашка под ним подгоняется по длине."""
    put(shape(slide, sid), [text.upper()], color=WHITE if pill is not None else PURPLE)
    if pill is not None:
        w = Inches(0.5 + 0.205 * len(text))
        shape(slide, pill).width = w
        shape(slide, sid).width = w


def drop(slide, sid):
    sh = shape(slide, sid)
    sh._element.getparent().remove(sh._element)


def picture_in(slide, sid, path, box=None):
    """Картинка в рамку поля: положение — как у рамки на слайде (или box),
    обрезка по центру под её пропорции. Без явного положения рамка брала
    место из макета, и фото уезжало в угол или пропадало."""
    from PIL import Image
    ph = shape(slide, sid)
    x, y, w, h = box or (ph.left, ph.top, ph.width, ph.height)
    pic = ph.insert_picture(path)
    pic.left, pic.top, pic.width, pic.height = int(x), int(y), int(w), int(h)
    iw, ih = Image.open(path).size
    img_r, box_r = iw / ih, w / h
    pic.crop_left = pic.crop_right = pic.crop_top = pic.crop_bottom = 0
    if img_r > box_r:                                  # картинка шире рамки
        c = (1 - box_r / img_r) / 2
        pic.crop_left = pic.crop_right = c
    elif img_r < box_r:
        c = (1 - img_r / box_r) / 2
        pic.crop_top = pic.crop_bottom = c
    return pic


def picture_fit(slide, sid, path, align="left"):
    """Картинка внутри рамки поля без обрезки; поле удаляется."""
    from PIL import Image
    ph = shape(slide, sid)
    x, y, w, h = ph.left, ph.top, ph.width, ph.height
    drop(slide, sid)
    iw, ih = Image.open(path).size
    k = min(w / iw, h / ih)
    pw, ph_ = int(iw * k), int(ih * k)
    px = x if align == "left" else x + (w - pw) // 2
    return slide.shapes.add_picture(path, px, y + (h - ph_) // 2, pw, ph_)


def textbox(slide, x, y, w, h, paras, size=12, color="1C1D22", bold=False):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        runs = para if isinstance(para, list) else [(para, {})]
        for txt, opt in runs:
            r = p.add_run()
            r.text = txt
            r.font.name = "Montserrat"
            r.font.size = Pt(opt.get("size", size))
            r.font.bold = opt.get("bold", bold)
            r.font.color.rgb = RGBColor.from_string(opt.get("color", color))
    return tb


def series_labels(ser, fmt_code, color, size, bold=False):
    """Подписи значений у ряда: в шаблоне они выключены на уровне ряда,
    и включение на уровне диаграммы их не показывает."""
    dl = ser.data_labels
    dl.show_value = True
    dl.show_category_name = False
    dl.show_series_name = False
    dl.show_percentage = False           # у кольцевой шаблона включена доля
    dl.number_format = fmt_code
    dl.number_format_is_linked = False
    dl.font.size = Pt(size)
    dl.font.bold = bold
    dl.font.color.rgb = RGBColor.from_string(color)


def fmt(v, nd=2):
    return f"{v:.{nd}f}".replace(".", ",")


def short(name, n=18):
    import re
    s = re.sub(r"^\d+\.\s*", "", name)
    s = re.sub(r"\(.*$", "", s)
    s = re.sub(r"\b(улица|ул|академика|пр-д|проезд)\b\.?", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:n]


def main(src, out):
    prs = Presentation(src)
    S = list(prs.slides)
    B = lambda t: (t, {"bold": True})                      # noqa: E731

    # Данные команды — в tools/deck/team/team.json (вне репозитория: там
    # телефон и почта). Без файла слайды о команде остаются с подсказками.
    team = load("tools/deck/team/team.json")
    tdir = os.path.join(ROOT, "tools", "deck", "team")

    # 1. Титул ------------------------------------------------------------------
    s = S[0]
    put(shape(s, 3), ["Сервис автоматического проектирования озеленения с учётом подземных коммуникаций"], color=WHITE)
    put(shape(s, 5), [(f"Команда «{team['name']}» · " if team else "") + "GreenAI · кейс ДПиООС Москвы"], color=WHITE)
    picture_fit(s, 4, os.path.join(IMG, "logo_eco.png"))

    # 2. О команде ------------------------------------------------------------------
    s = S[1]
    put(shape(s, 18), [f"Команда «{team['name']}»" if team else "GreenAI"], color=PURPLE, bold=True, size=24)
    if team:
        # слайды 7–11 шаблона обязательные: абзацы и оформление — как в шаблоне,
        # меняется только текст; пустой ответ убирает строку-подсказку
        fill(shape(s, 14), [["Капитан: ", team["captain"]],
                            ["Кол-во участников: ", team["count"]],
                            ["Краткое описание: "],
                            [team.get("formed") or team["about"]],
                            [team["work"]] if team.get("work") else None,
                            ["Город и регион: ", team["city"]]])
        picture_in(s, 2, os.path.join(tdir, "team.jpg"))
    put(shape(s, 5), ["Принимаем чертёж улицы (DXF/DWG), сами находим подземные сети и строим план "
                      "посадок, который не нарушает нормативных отступов. Результат — DXF на отдельных "
                      "слоях и объяснение каждой посадки со ссылкой на пункт нормы."])
    put(shape(s, 8), ["Три независимые проверки норм, включая точную по исходной геометрии; модель "
                      "выбора места обучена на посадочных планах 8 улиц пилота."])

    # 3. Участники: по правилам организаторов лишние карточки только удаляются,
    # оставшиеся стоят на своих местах в прежнем размере
    s = S[2]
    put(shape(s, 7), ["УЧАСТНИКИ КОМАНДЫ"], color=WHITE)
    if team:
        cards = [(17, 2, 15, 9), (56, 3, 58, 57), (59, 4, 61, 60), (62, 5, 64, 63), (65, 6, 67, 66)]
        n = len(team["members"])
        for ids in cards[n:]:
            for sid in ids:
                drop(s, sid)
        for (rect, pic, name, det), m in zip(cards, team["members"]):
            fill(shape(s, name), [[m["name"]]])
            # строки шаблона: роль, ник в мессенджере, телефон, место работы/учёбы
            fill(shape(s, det), [[m[k]] if m.get(k) else None
                                 for k in ("role", "nick", "phone", "work")])
            picture_in(s, pic, os.path.join(tdir, m["photo"]))

    # 4. История команды ---------------------------------------------------------------
    s = S[3]
    put(shape(s, 7), ["ИСТОРИЯ КОМАНДЫ"], color=WHITE)
    if team:
        put(shape(s, 37), [team["history"]])
        put(shape(s, 43), [team["why"]])
    put(shape(s, 40), ["Чертежи без единого стандарта: генпланы-«рамки» с десятками внешних ссылок, "
                       "слои из PDF, Civil 3D, выгрузки ДЖКХ — сделали автоподгрузку ссылок и "
                       "распознавание в четыре ступени. Проектировщики сами отступают от норм — "
                       "сравнили их планы с таблицей 9.1 и вынесли это в отдельный сценарий."])

    # 5. Коротко о решении -----------------------------------------------------------
    s = S[4]                                # заголовок уже есть в шаблоне
    put(shape(s, 3), [
        "Разбор DXF и DWG, автоподгрузка внешних ссылок",
        "Классы слоёв: правила, модель имён (97,5 %), локальная LLM",
        "Карты отступов по СП 42.13330, 743-ПП и 623-ПП",
        "Аллеи вдоль борта, продолжение рядов, модель места",
        "Три проверки норм, DXF со слоями GREEN_AI_*",
        "Docker, FastAPI + Swagger, командная строка",
    ], bullet=True)
    put(shape(s, 7), [
        "Черновик плана посадок за минуты вместо дней ручной сверки отступов",
        "Проверка готовых проектов: где посадки нарушают нормы — с цифрами и пунктами",
        "20 улиц пилота одним прогоном",
        "Внедрение: пилот в контуре ДПиООС, Docker под МосТех.ОС",
    ], bullet=True)

    # 6. Проблема -------------------------------------------------------------------------
    s = S[5]
    title(s, "Проблема", 9, pill=2)
    for sid, (h, d) in zip((3, 4, 5), [
            ("Долго", "Проектировщик сверяет каждое дерево с таблицей отступов вручную и перепроверяет после каждой правки."),
            ("Опасно", "Ошибка в отступе — повреждённый кабель, водопровод или газопровод при посадке."),
            ("Не масштабируется", "Результат зависит от опыта специалиста; десятки участков одновременно не проверить.")]):
        put(shape(s, sid), [[B(h)], d])

    # 7. Как работает: пять стадий -------------------------------------------------------------
    s = S[6]
    title(s, "Как работает сервис", 15, pill=12)
    steps = [(2, 3, "Чтение", "DXF, DWG, PDF; внешние ссылки подгружаются сами"),
             (8, 9, "Распознавание", "слои → сети, дороги, газоны, деревья"),
             (4, 5, "Карта «где можно»", "отступы по нормам для деревьев и кустарников"),
             (10, 11, "Расстановка", "аллеи, ряды, группы, кустарники"),
             (6, 7, "Проверка и DXF", "три проверки, слои GREEN_AI_*, объяснения")]
    for h_id, d_id, h, d in steps:
        put(shape(s, h_id), [h], bold=True, color=PINK, size=15)
        put(shape(s, d_id), [d], size=12)

    # 8. Архитектура -----------------------------------------------------------------------------
    s = S[7]
    put(shape(s, 14), ["АРХИТЕКТУРА РЕШЕНИЯ"], color=PURPLE, bold=True)
    cols = [(15, 2, 3, "01", "Вход", ["Командная строка", "Веб-интерфейс", "HTTP API + Swagger", "DXF, DWG, PDF, ссылки"]),
            (16, 4, 5, "02", "Ядро на Python", ["Чтение и внешние ссылки", "Классы слоёв и модели", "Карты отступов, зоны", "Размещение и 3 проверки"]),
            (17, 6, 7, "03", "Выход", ["DXF: исходные слои + GREEN_AI_*", "DXF только с посадками", "Объяснения: JSON, CSV, MD", "Интерактивная схема"])]
    for n_id, h_id, b_id, n, h, lines in cols:
        put(shape(s, n_id), [n], color=PINK)
        put(shape(s, h_id), [h], color=PURPLE, bold=True)
        put(shape(s, b_id), lines, bullet=True, size=13)

    # 9. Распознавание слоёв -----------------------------------------------------------------------
    s = S[8]
    title(s, "Как сервис понимает чертёж", 14, pill=10)
    st = [(15, 2, 3, "Правила", "Имена Мосгеотреста, разделов ДВ, Civil 3D, чертежей из PDF. Газон и границу работ назначают только правила."),
          (16, 4, 5, "Контекст", "Типы покрытий, соседство с известными линиями, подписи у линий."),
          (17, 6, 7, "Модель имён", "Обучена на 19,5 тыс. размеченных слоёв 20 улиц: точность 97,5 %."),
          (18, 8, 9, "Локальная LLM", "Ollama без интернета: подсказка для незнакомых имён, 78 %.")]
    for i, (n_id, h_id, b_id, h, b) in enumerate(st, 1):
        put(shape(s, n_id), [f"0{i}"], color=PINK)
        put(shape(s, h_id), [h], color=PURPLE, bold=True)
        put(shape(s, b_id), [b], size=13)

    # 10. Нормы: таблица + акты ----------------------------------------------------------------------
    s = S[9]
    title(s, "Нормы отступов", 14, pill=2)
    box = shape(s, 4)
    x, y, w = box.left, box.top, box.width
    drop(s, 4)
    rows = [("Объект", "Дерево, м", "Кустарник, м"), ("Стена здания", "5,0", "1,5"),
            ("Край проезжей части", "2,0", "1,0"), ("Край тротуара", "0,7", "0,5"),
            ("Опора освещения", "4,0", "—"), ("Газопровод, канализация", "1,5", "—"),
            ("Теплосеть", "2,0", "1,0"), ("Водопровод, дренаж", "2,0", "—"),
            ("Кабель силовой, связи", "2,0", "0,7")]
    tbl = s.shapes.add_table(len(rows), 3, x, y, w, Inches(0.5 * len(rows))).table
    tbl.columns[0].width = int(w * 0.56)
    tbl.columns[1].width = int(w * 0.22)
    tbl.columns[2].width = w - tbl.columns[0].width - tbl.columns[1].width
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            cell = tbl.cell(r, c)
            cell.text = v
            para = cell.text_frame.paragraphs[0]
            for run in para.runs:
                run.font.name = "Montserrat"
                run.font.size = Pt(13 if r else 12)
                run.font.bold = r == 0
                run.font.color.rgb = RGBColor.from_string("FFFFFF" if r == 0 else "1C1D22")
            cell.fill.solid()
            cell.fill.fore_color.rgb = RGBColor.from_string(PURPLE if r == 0 else ("FFFFFF" if r % 2 else "F4ECFA"))
    refs = [(5, "СП 42.13330.2016 — таблица 9.1"),
            (6, "743-ПП: прил. 1, п. 3.6.3, табл. 3.6.1 (МГСН 1.01-99)"),
            (7, "623-ПП: МГСН 1.02-02, п. 4.2.4"),
            (8, "Шаг посадки 6 м: 743-ПП, п. 3.6.4, табл. 3.6.2"),
            (9, "Значения трёх актов совпадают — сверено с текстом 743-ПП")]
    for sid, t in refs:
        put(shape(s, sid), [t])

    # 11. Алгоритм --------------------------------------------------------------------------------
    s = S[10]
    put(shape(s, 6), ["АЛГОРИТМ РАССТАНОВКИ"], color=PURPLE, bold=True)
    alg = [(8, "Карты расстояний", "каждый класс объектов → растр, шаг 0,25 м"),
           (9, "Зона допустимости", "газон минус отступы, отдельно для деревьев и кустарников"),
           (10, "Продолжение рядов", "ряды существующих деревьев — с их же шагом"),
           (11, "Аллея вдоль борта", "постоянный отступ от бордюра: норма + 0,5 м"),
           (12, "Группы и кустарники", "сетка в широких зонах, изгороди вдоль проезжей части")]
    for sid, h, d in alg:
        put(shape(s, sid), [[B(h)], d])
    sh8 = shape(s, 8)
    params = ["ячейка 0,25 м", "13 видов отступов", "3+ дерева, шаг 4–12 м", "шаг 6 м по 743-ПП", "модель места: AUC 0,85"]
    for i, t in enumerate(params):
        yy = sh8.top + i * Inches(0.94)
        textbox(s, 7.2, yy / 914400 + 0.18, 5.6, 0.5, [t], size=16, color=PURPLE, bold=True)

    # 12. До и после ---------------------------------------------------------------------------------
    s = S[11]
    put(shape(s, 2), ["ДО И ПОСЛЕ: КАМЧАТСКАЯ УЛИЦА"], color=WHITE, bold=True)
    ph = shape(s, 3)
    x, y, w = ph.left, ph.top, ph.width
    drop(s, 3)
    h = int(w * 720 / 2400)
    s.shapes.add_picture(os.path.join(IMG, "kamchatka.png"), x, y, w, h)
    textbox(s, x / 914400, (y + h) / 914400 + 0.12, w / 914400, 0.9, [
        [("Слева — подоснова и сети, справа — зона допустимости и посадки. ", {"bold": True}),
         ("Вдоль главной улицы — пучок кабелей, теплосети и водопровода: по нормам там сажать нельзя, "
          "и сервис не сажает. Кроны закрывают 94 % допустимой зоны, нарушений 0.", {})]], size=13)

    # 13. Модель выбора места -------------------------------------------------------------------------
    s = S[12]
    title(s, "Модель выбора места", 15, pill=2)
    by = sorted(MODEL.get("auc_by_street", {}).items(), key=lambda kv: kv[1])
    cd = CategoryChartData()
    cd.categories = [short(k) for k, _ in by]
    cd.add_series("AUC", [round(v, 3) for _, v in by])
    ch = shape(s, 12).chart
    ch.replace_data(cd)
    plot = ch.plots[0]
    plot.vary_by_categories = False
    ser = plot.series[0]
    for i in range(len(by)):                           # цвета точек шаблона — одним цветом
        pt = ser.points[i]
        pt.format.fill.solid()
        pt.format.fill.fore_color.rgb = RGBColor.from_string(PINK)
    ser.format.fill.solid()
    ser.format.fill.fore_color.rgb = RGBColor.from_string(PINK)
    ch.value_axis.minimum_scale = 0.5
    ch.value_axis.maximum_scale = 1.0
    plot.has_data_labels = True
    series_labels(ser, "0.00", DARK, 11)
    pairs = [(3, 4, fmt(MODEL.get("auc_holdout", 0)), "средний AUC на улице, которую модель не видела"),
             (5, 6, f"{len(by)} улиц", f"{MODEL.get('positives', 0)} посадок проектировщиков в обучении"),
             (7, 8, "Учит", "дальше от проезжей части, ближе к существующим деревьям и к оси газона"),
             (9, 10, "Нормы", "не меняет — только ранжирует места внутри разрешённой зоны")]
    for a, b, v, l in pairs:
        put(shape(s, a), [v], color=PINK, bold=True)
        put(shape(s, b), [l], size=12)

    # 14. Три проверки ------------------------------------------------------------------------------
    s = S[13]
    title(s, "Три проверки норм", 9, pill=2)
    for sid, (h, d) in zip((3, 4, 5), [
            ("Отступы по картам", "Расстояние до каждого нормируемого объекта в точке посадки."),
            ("Попадание в зону", "Точка лежит внутри итоговой допустимой зоны своего типа."),
            ("Точная геометрия", "Расстояние до исходных линий без растра. 3-я Парковая: 7 посадок на 6–16 см ближе нормы найдены и убраны.")]):
        put(shape(s, sid), [[B(h)], d])

    # 15. Слои DXF ----------------------------------------------------------------------------------
    s = S[14]
    title(s, "Слои DXF", 14, pill=2)
    put(shape(s, 4), [[B("Исходные слои — без изменений")], "подоснова, сети, покрытия, существующие деревья",
                      "ДВ_ГП_П_Борт_БР100.30.15", "…ДЖКХ-25_00577up$0$Водопровод", "Гео_Камчатская$0$Кабель",
                      "ДВ_ПП_Газон_Р", [B("Формат: DXF R2013, метры; включаются и выключаются в CAD независимо")]])
    lay = [(5, "GREEN_AI_TREES", "деревья: блок с атрибутами ID, PORODA, NORMA"),
           (6, "GREEN_AI_SHRUBS", "кустарники"),
           (7, "GREEN_AI_ZONES", "зоны допустимости"),
           (8, "GREEN_AI_NOTES", "служебные пометки"),
           (9, "*_PLAN_ONLY.dxf", "отдельный файл только с посадками")]
    for sid, n, d in lay:
        put(shape(s, sid), [[B(n), (" — " + d, {})]])

    # 16. Объяснение посадки -----------------------------------------------------------------------
    s = S[15]
    put(shape(s, 3), ["ОБЪЯСНЕНИЕ ПОСАДКИ"], color=PURPLE, bold=True)
    put(shape(s, 7), [
        [B("T-214 · Ель колючая · Камчатская")],
        "Посадка допустима. Определяющее ограничение — силовой кабель: требуется не менее 2,0 м, фактически 3,16 м.",
        [B("СП 42.13330.2016, табл. 9.1; 743-ПП, прил. 1, п. 3.6.3, табл. 3.6.1; 623-ПП (МГСН 1.02-02), п. 4.2.4")],
        "Прочие отступы: существующее дерево 5,27 м при норме 4,0; стена здания 10,08 м при норме 5,0.",
        [B("Где лежат объяснения")],
        "*_explain.json и *_explain.csv — каждая посадка и все проверки",
        "*_report.md — отчёт; атрибут NORMA — в блоке посадки в DXF",
    ])
    picture_in(s, 4, os.path.join(IMG, "kam_portrait.png"))

    # 17. Результаты пилота -----------------------------------------------------------------------
    s = S[16]
    title(s, "Результаты пилота", 13, pill=2)
    ok = [r for r in BATCH.get("rows", []) if r.get("ok")]
    top = sorted(ok, key=lambda r: -(r.get("trees") or 0))[:10]
    cd = CategoryChartData()
    cd.categories = [short(r["street"], 12) for r in top]
    cd.add_series("Деревьев", [r.get("trees") or 0 for r in top])
    ch = shape(s, 10).chart
    ch.replace_data(cd)
    ch.has_legend = False
    series_labels(ch.plots[0].series[0], "0", WHITE, 10)
    ch.value_axis.major_unit = 200
    ch.value_axis.has_major_gridlines = False
    # vector_violations — посадки, убранные точной проверкой: в плане их нет
    viol = sum(r.get("violations") or 0 for r in ok)
    secs = sorted(r.get("seconds") or 0 for r in ok)
    med = (secs[(len(secs) - 1) // 2] + secs[len(secs) // 2]) / 120 if secs else 0
    n_all = len(BATCH.get("rows", [])) or 20
    for a, b, v, l in [(3, 4, f"{len(ok)} из {n_all}", "улиц пилота посчитано, строгий сценарий"),
                       (5, 6, str(viol), "нарушений норм в готовых планах"),
                       (7, 8, f"{fmt(med, 1)} мин", "медиана времени расчёта улицы")]:
        put(shape(s, a), [v], color=PINK, bold=True)
        put(shape(s, b), [l], size=12)

    # 18. Сравнение с проектировщиками ---------------------------------------------------------------
    s = S[17]
    title(s, "Сравнение с проектировщиками", 14, pill=2)
    cd = CategoryChartData()
    cd.categories = ["ближе нормы к кабелю", "по норме"]
    cd.add_series("Деревья проектировщика", [67, 33])
    ch = shape(s, 12).chart
    ch.replace_data(cd)
    ser = ch.plots[0].series[0]
    for i, c in enumerate((PINK, PALE)):
        ser.points[i].format.fill.solid()
        ser.points[i].format.fill.fore_color.rgb = RGBColor.from_string(c)
    series_labels(ser, '0"%"', DARK, 16, bold=True)
    for a, b, v, l in [(3, 4, "67 %", "деревьев проектировщика на Камчатской стоят ближе нормы к кабелю"),
                       (5, 6, "1,27 м", "медиана расстояния до кабеля при норме 2,0 м"),
                       (7, 8, "6–7 м", "шаг посадки у проектировщиков — сервис принял 6 м по 743-ПП"),
                       (9, 10, "«Практика»", "отдельный сценарий с отступами проектов — не норматив")]:
        put(shape(s, a), [v], color=PINK, bold=True)
        put(shape(s, b), [l], size=12)

    # 19. Запуск ----------------------------------------------------------------------------------
    s = S[18]
    title(s, "Как запустить", 9, pill=3)
    picture_in(s, 2, os.path.join(IMG, "kam_43.png"))
    put(shape(s, 4), [[B("Сайт и API")], "docker compose up", "localhost:8000, Swagger /docs"])
    put(shape(s, 5), [[B("Расчёт")], "docker compose run --rm greenai python src/main.py --input улица.dwg"])
    put(shape(s, 6), [[B("Время")], "от 32 с (Понтрягина) до 9,5 мин (Олимпийская, 5 тыс. посадок)",
                      "github.com/Kaban425/greenai"])

    # 20. Ограничения и развитие ------------------------------------------------------------------
    s = S[19]
    title(s, "Ограничения и развитие", 14, pill=23)
    items = [(15, 2, 3, "01", "Строгие нормы", "на плотных улицах оставляют мало мест — это свойство норм"),
             (16, 4, 5, "02", "Чертежи из PDF", "со слитой топосъёмкой: до 19 % длины линий без класса"),
             (17, 6, 7, "03", "МосТех.ОС", "проверено в Linux-контейнере Debian, не на самой ОС"),
             (18, 8, 9, "04", "Корнезащитный экран", "сценарий сближения с сетью по согласованию с владельцем"),
             (19, 10, 11, "05", "Данные data.mos.ru", "здания, границы участков, охранные зоны"),
             (20, 12, 13, "06", "Пилот в ДПиООС", "правка на схеме и пополнение выборки размеченных слоёв")]
    for n_id, h_id, b_id, n, h, b in items:
        put(shape(s, n_id), [n], color=PINK)
        put(shape(s, h_id), [h], color=PURPLE, bold=True)
        put(shape(s, b_id), [b], size=13)

    prs.save(out)
    print("saved", out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
