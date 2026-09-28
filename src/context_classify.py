# -*- coding: utf-8 -*-
"""Распознавание слоёв по контексту чертежа, когда имя ничего не говорит.

Три независимых способа, каждый смотрит на своё:

  colocate  — соседство. Слой, чьи линии лежат поверх линий уже
              распознанного слоя, скорее всего, того же класса. Так ловятся
              дубли: подложка PDF, разобранная на примитивы («PDF _Проект»),
              повторяет проектную геометрию линия в линию.
  tokens    — слова в именах. Из слоёв, распознанных в этом же чертеже,
              выучивается, какие части имени к какому классу относятся.
              У одного проектировщика соглашение об именах устойчиво.
  labels    — подписи рядом с линиями. «К1», «В1», «d=300», «газон»
              у линии однозначно называют, что это.

Все три осторожные: класс назначается, только когда подавляющее
большинство свидетельств указывает на него. Лучше оставить слой без
класса, чем присвоить неверный.
"""
import re
from collections import Counter, defaultdict

import numpy as np

from layers import normalize

SKIP = {"unknown", "annotation", "ignore", "reference_planting"}
# Поверхности соседством не назначаются: граница газона и проезжей части —
# одна и та же линия, и слой дороги ложится «поверх газона» по всей длине.
# Так дорога получала класс газона, и деревья уходили на асфальт.
# Проезжая часть и тротуар — препятствия: слой, лежащий поверх бортов
# (подложка PDF, разобранная на штрихи), получает их класс безопасно —
# ошибка стоит недосадки, а не посадки на асфальте.
NO_COLOCATE = {"lawn", "site_boundary"}


def _points(geoms, step=0.5, cap=4000):
    """Точки вдоль геометрий слоя, не больше cap штук."""
    from raster_engine import _rings, _densify
    chunks = []
    for g in geoms:
        if g is None or g.is_empty:
            continue
        for r in _rings(g):
            if len(r):
                chunks.append(_densify(r, step))
    if not chunks:
        return np.empty((0, 2))
    arr = np.vstack(chunks)
    if len(arr) > cap:
        arr = arr[np.linspace(0, len(arr) - 1, cap).astype(int)]
    return arr


# ---------------------------------------------------------------------- #
#  1. Соседство
# ---------------------------------------------------------------------- #

def colocate(by_layer, layer_class, tol=0.5, share=0.85, cell=0.5):
    """Слой без класса, лежащий поверх распознанного, получает его класс."""
    from scipy import ndimage
    from scipy.spatial import cKDTree

    known = defaultdict(list)
    for lay, cls in layer_class.items():
        if cls in SKIP or cls in NO_COLOCATE:
            continue
        p = _points(by_layer.get(lay, []), step=cell, cap=5000)
        if len(p):
            known[cls].append(p)
    if not known:
        return {}
    trees = {cls: cKDTree(np.vstack(ps)) for cls, ps in known.items()}

    out = {}
    for lay, cls in layer_class.items():
        if cls != "unknown" or mixed_layer(lay, by_layer.get(lay, [])):
            continue
        p = _points(by_layer.get(lay, []), step=cell, cap=3000)
        if len(p) < 40:
            continue
        best, best_share = None, 0.0
        for kc, tr in trees.items():
            d, _ = tr.query(p, k=1, distance_upper_bound=tol)
            sh = float(np.isfinite(d).mean())
            if sh > best_share:
                best, best_share = kc, sh
        # Отрыв от второго кандидата: если слой одинаково ложится на две
        # разные сущности, решение ненадёжно и класс не назначается.
        if best and best_share >= share:
            second = sorted(
                (float(np.isfinite(tr.query(p, k=1,
                                            distance_upper_bound=tol)[0]).mean())
                 for kc, tr in trees.items() if kc != best), reverse=True)
            if not second or best_share - second[0] >= 0.15:
                out[lay] = best
    return out


# ---------------------------------------------------------------------- #
#  2. Слова в именах
# ---------------------------------------------------------------------- #

_SPLIT = re.compile(r"[_\s\-\.\$\|\(\)\[\],;:]+")


def _tokens(name):
    toks = []
    for t in _SPLIT.split(normalize(name).lower()):
        if len(t) < 2 or t.isdigit():
            continue
        toks.append(t)
    return toks


def tokens(layer_class, min_support=3, min_share=0.8):
    """Голосование частей имени по классам, выученным в этом же чертеже."""
    votes = defaultdict(Counter)
    for lay, cls in layer_class.items():
        if cls in SKIP:
            continue
        for t in set(_tokens(lay)):
            votes[t][cls] += 1

    out = {}
    for lay, cls in layer_class.items():
        if cls != "unknown":
            continue
        tally = Counter()
        support = 0
        for t in set(_tokens(lay)):
            c = votes.get(t)
            if not c:
                continue
            total = sum(c.values())
            top, n = c.most_common(1)[0]
            # слово должно быть надёжным признаком: встречаться в нескольких
            # слоях и почти всегда при одном классе
            if total >= min_support and n / total >= min_share:
                tally[top] += n
                support += 1
        if tally and support:
            top, n = tally.most_common(1)[0]
            if n / sum(tally.values()) >= min_share:
                out[lay] = top
    return out


# ---------------------------------------------------------------------- #
#  2а. Номер типа покрытия
# ---------------------------------------------------------------------- #

_TYPE = re.compile(r"\bтип\s*(\d+[а-яё]?)\b", re.IGNORECASE)
PAVEMENTS = ("road", "walkway")
# Слово в имени, по которому видно, что это контур покрытия, а не,
# скажем, «тип опоры»: так безымянный тип считается покрытием.
_PAVEMENT_HINT = re.compile(r"контр|контур|покрыт|\bпп\b|\bдо\b", re.IGNORECASE)


def type_codes(layer_class, layer_source):
    """Контуры типов покрытий по номеру типа, выученному в этом же чертеже.

    «ГП_контр_тип6» не говорит, что это тротуар, но в том же чертеже есть
    «ДВ_ПП_ДО_Тип6_Устройство_трот», распознанный правилом. Номер типа —
    сквозной для проекта, поэтому класс переносится по нему. Тип, который
    в чертеже не встретился, считается тротуаром: это всё равно твёрдое
    покрытие, и сажать внутри него нельзя. Проезжей частью неизвестный тип
    не назначается — отступ в 2 м от него съел бы газоны без причины.
    """
    votes = defaultdict(Counter)
    for lay, cls in layer_class.items():
        if cls in PAVEMENTS and layer_source.get(lay) in ("rules", "manual"):
            for code in _TYPE.findall(normalize(lay)):
                votes[code.lower()][cls] += 1

    out = {}
    for lay, cls in layer_class.items():
        if cls != "unknown":
            continue
        name = normalize(lay)
        codes = [c.lower() for c in _TYPE.findall(name)]
        if not codes:
            continue
        tally = Counter()
        for c in codes:
            tally.update(votes.get(c, {}))
        if tally:
            top, n = tally.most_common(1)[0]
            if n == sum(tally.values()):         # только единогласно
                out[lay] = top
        elif _PAVEMENT_HINT.search(name):
            out[lay] = "walkway"
    return out


# ---------------------------------------------------------------------- #
#  3. Подписи рядом с линиями
# ---------------------------------------------------------------------- #

LABELS = [
    ("sewer",       r"\bк1\b|канализ|фекал|\bлот\.?\b"),
    ("water",       r"\bк2\b|\bв1\b|\bв2\b|водопр|водосток|ливн|дренаж"),
    ("heating",     r"\bт1\b|\bт2\b|тепло|\b\d+\s*[xх×]\s*\d{3,4}\b"),
    ("gas",         r"\bг\b|газ"),
    ("power_cable", r"\bклс\b|\bкл\b|кабел|\b\d+\s*к\b|\b\d+\s*тр\b|связ|\bэс\b"),
    ("lawn",        r"газон|цветник"),
    ("walkway",     r"тротуар|дорожк|плитк"),
    ("road",        r"проезж|\bпч\b|асфальт"),
]
_LABEL_RX = [(c, re.compile(p, re.I)) for c, p in LABELS]


def collect_texts(doc):
    """Подписи чертежа: (x, y, текст)."""
    out = []
    try:
        msp = doc.modelspace()
    except Exception:
        return out

    def take(e):
        try:
            txt = e.plain_text() if hasattr(e, "plain_text") else e.dxf.text
            ins = e.dxf.insert
            out.append((float(ins.x), float(ins.y), str(txt or "")))
        except Exception:
            pass

    for e in msp:
        t = e.dxftype()
        if t in ("TEXT", "MTEXT", "ATTRIB"):
            take(e)
        elif t == "INSERT":
            # Подписи сетей и колодцев часто — атрибуты блоков: «К1-12»,
            # «В1 d=300». Раньше они не собирались вовсе.
            for a in getattr(e, "attribs", []) or []:
                take(a)
    return out


# Слой-свалка: вся геометрия чужого чертежа одним слоем («Никулинский
# импорт ПДФ … _Геометрия», 222 тысячи объектов). Несколько подписей
# кабеля рядом делали его кабелем целиком, и зона кабеля накрывала 63%
# газонов Олимпийской деревни. Одной сетью такой слой не бывает.
MIXED_RE = re.compile(r"импорт\w*.{0,6}(пдф|pdf)|(пдф|pdf).{0,4}геометр|"
                      r"\$_?геометр", re.I)
MIXED_MAX_OBJECTS = 20000


def mixed_layer(name, geoms):
    return len(geoms) > MIXED_MAX_OBJECTS or bool(MIXED_RE.search(name))


def labels(by_layer, layer_class, texts, radius=3.0, min_hits=3, min_share=0.75):
    """Слой без класса получает класс по подписям у его линий."""
    if not texts:
        return {}
    from scipy.spatial import cKDTree
    tagged = []
    for x, y, txt in texts:
        for cls, rx in _LABEL_RX:
            if rx.search(txt):
                tagged.append((x, y, cls))
                break
    if not tagged:
        return {}
    tp = np.array([[x, y] for x, y, _ in tagged])
    tc = [c for _, _, c in tagged]
    tree = cKDTree(tp)

    out = {}
    for lay, cls in layer_class.items():
        if cls != "unknown" or mixed_layer(lay, by_layer.get(lay, [])):
            continue
        p = _points(by_layer.get(lay, []), step=1.0, cap=2000)
        if len(p) < 5:
            continue
        hits = Counter()
        for idx in tree.query_ball_point(p, r=radius):
            for i in idx:
                hits[tc[i]] += 1
        if not hits:
            continue
        top, n = hits.most_common(1)[0]
        if n >= min_hits and n / sum(hits.values()) >= min_share:
            out[lay] = top
    return out
