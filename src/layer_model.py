# -*- coding: utf-8 -*-
"""Обучаемый классификатор имён слоёв.

Правила в layers.py точны на знакомых словах, но не видят вариаций:
опечаток («зеземление»), сокращений («Водопр_пр»), чужих префиксов.
Модель учится на именах, которые уже получили класс надёжным способом —
правилом или ручным назначением, — и переносит закономерность на
похожие имена, которые правила пропускают.

Устройство: наивный байесовский классификатор по буквенным n-граммам
(кусочкам слов по 3-5 букв) и целым словам. Выбран сознательно:
  * учится на сотнях примеров за доли секунды, без видеокарты;
  * не требует внешних библиотек;
  * выдаёт вероятность, по которой отсекаются неуверенные ответы.

Обучающая выборка накапливается сама: каждый расчёт дописывает в
config/layer_dataset.jsonl слои с надёжным классом. Чем больше улиц
прогнано, тем точнее модель.

    python src/train_layers.py         обучить и показать точность
"""
import json
import math
import os
import re
from collections import Counter, defaultdict

from layers import normalize

TRUSTED = {"manual", "rules"}          # источники, которым верим как учителю
SKIP = {"unknown", "ignore"}
_SPLIT = re.compile(r"[_\s\-\.\$\|\(\)\[\],;:—–]+")


def features(name):
    """Признаки имени: буквенные n-граммы и слова."""
    t = normalize(name).lower()
    t = re.sub(r"\d+", "#", t)
    feats = []
    padded = f" {t} "
    for n in (3, 4, 5):
        feats += [padded[i:i + n] for i in range(len(padded) - n + 1)]
    feats += ["w:" + w for w in _SPLIT.split(t) if len(w) > 1]
    return feats


# ---------------------------------------------------------------------- #
#  Обучающая выборка
# ---------------------------------------------------------------------- #

def dataset_path(root):
    return os.path.join(root, "config", "layer_dataset.jsonl")


def append_examples(root, layer_class, layer_source, source_file=""):
    """Дописывает в выборку слои с надёжным классом. Дубли не пишутся."""
    path = dataset_path(root)
    seen = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    seen.add(json.loads(line)["name"])
                except Exception:
                    continue
    added = 0
    with open(path, "a", encoding="utf-8") as f:
        for name, cls in layer_class.items():
            if cls in SKIP or layer_source.get(name) not in TRUSTED:
                continue
            if name in seen:
                continue
            f.write(json.dumps({"name": name, "cls": cls,
                                "src": layer_source.get(name),
                                "file": os.path.basename(source_file)},
                               ensure_ascii=False) + "\n")
            seen.add(name)
            added += 1
    return added


def refresh_dataset(root):
    """Переразмечает примеры, записанные правилами, текущими правилами.

    Выборка пополняется правилами того времени, когда шёл расчёт. Когда
    правило исправляют, старая ошибка остаётся в выборке и дальше учит
    модель имён и образцы в Modelfile: так «ЭН_освещенность» долго
    числилась опорой освещения. Ручные назначения не трогаются, слои,
    ставшие для правил непонятными, из выборки убираются.
    Возвращает (изменено, удалено).
    """
    from layers import classify_layer
    rows = load_dataset(root)
    out, changed, dropped = [], 0, 0
    for r in rows:
        if r.get("src") == "rules":
            cls = classify_layer(r["name"])
            if cls in SKIP:
                dropped += 1
                continue
            if cls != r["cls"]:
                changed += 1
                r = dict(r, cls=cls)
        out.append(r)
    if changed or dropped:
        path = dataset_path(root)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in out:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    return changed, dropped


def absorb_harvest(root):
    """Имена из сбора по проектам, которые теперь знают правила, — в выборку.

    config/harvest_unknown.txt копит имена, не распознанные при сборе.
    Когда правила дописывают, часть их становится понятной, но в выборку
    сама не попадала: модель имён училась без них. Здесь такие имена
    добавляются с классом по правилу и уходят из списка на разметку.
    Возвращает число добавленных.
    """
    from layers import classify_layer, is_anonymous
    p = os.path.join(root, "config", "harvest_unknown.txt")
    if not os.path.exists(p):
        return 0
    have = {r["name"] for r in load_dataset(root)}
    keep, add = [], []
    with open(p, encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines:
        name = line.rstrip("\n").partition("\t")[2]
        cls = classify_layer(name) if name and not is_anonymous(name) else "unknown"
        if cls in SKIP or cls == "unknown":
            keep.append(line)
            continue
        if name not in have:
            add.append({"name": name, "cls": cls, "src": "rules", "file": "harvest"})
            have.add(name)
    if add:
        with open(dataset_path(root), "a", encoding="utf-8", newline="\n") as f:
            for r in add:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if len(keep) != len(lines):
        with open(p + ".tmp", "w", encoding="utf-8", newline="\n") as f:
            f.writelines(keep)
        os.replace(p + ".tmp", p)
    return len(add)


def load_dataset(root):
    path = dataset_path(root)
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("cls") and r.get("name"):
                    rows.append(r)
            except Exception:
                continue
    return rows


# ---------------------------------------------------------------------- #
#  Модель
# ---------------------------------------------------------------------- #

class LayerModel:
    """Поиск похожих имён: TF-IDF по буквенным n-граммам и косинусная мера.

    Наивный Байес на маленькой выборке оказался излишне самоуверен: ставил
    вероятность 0,95 и ошибался, так что порог уверенности почти не
    отсекал ошибки. Сходство с ближайшими известными именами устроено
    честнее: вариация знакомого имени — опечатка, сокращение, чужой
    префикс — даёт высокое сходство, а незнакомое имя — низкое, и тогда
    модель честно отказывается отвечать.
    """

    def __init__(self, k=3):
        self.k = k
        self.names, self.labels, self.vecs = [], [], []
        self.idf = {}

    def _vec(self, name):
        tf = Counter(features(name))
        v = {f: (1 + math.log(c)) * self.idf.get(f, 0.0) for f, c in tf.items()
             if f in self.idf}
        # слова весомее кусочков: в них смысл, а n-граммы ловят опечатки
        for f in list(v):
            if f.startswith("w:"):
                v[f] *= 2.0
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {f: x / norm for f, x in v.items()}

    def fit(self, names, labels):
        df = Counter()
        docs = [set(features(n)) for n in names]
        for d in docs:
            df.update(d)
        N = len(names)
        self.idf = {f: math.log((1 + N) / (1 + c)) + 1.0 for f, c in df.items()}
        self.names, self.labels = list(names), list(labels)
        self.vecs = [self._vec(n) for n in names]
        return self

    @staticmethod
    def _cos(a, b):
        if len(a) > len(b):
            a, b = b, a
        return sum(x * b.get(f, 0.0) for f, x in a.items())

    def neighbours(self, name):
        q = self._vec(name)
        sims = [(self._cos(q, v), lab, n)
                for v, lab, n in zip(self.vecs, self.labels, self.names)]
        sims.sort(key=lambda t: -t[0])
        return sims[:self.k]

    def predict(self, name):
        """Класс и уверенность: взвешенный голос ближайших, уверенность —
        сходство с лучшим соседом, умноженное на согласие соседей."""
        nb = self.neighbours(name)
        if not nb or nb[0][0] <= 0:
            return "unknown", 0.0
        vote = Counter()
        for sim, lab, _ in nb:
            vote[lab] += sim
        cls, w = vote.most_common(1)[0]
        agree = w / (sum(vote.values()) or 1.0)
        best = max(sim for sim, lab, _ in nb if lab == cls)
        return cls, round(best * agree, 3)

    def predict_proba(self, name):
        c, p = self.predict(name)
        return {c: p} if c != "unknown" else {}

    def to_json(self):
        return {"k": self.k, "names": self.names, "labels": self.labels}

    @classmethod
    def from_json(cls, d):
        m = cls(d.get("k", 3))
        return m.fit(d["names"], d["labels"])


def model_path(root):
    return os.path.join(root, "config", "layer_model.json")


def save(model, root, report=None):
    d = model.to_json()
    if report:
        d["report"] = report
    with open(model_path(root), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


def load(root):
    try:
        with open(model_path(root), encoding="utf-8") as f:
            return LayerModel.from_json(json.load(f))
    except Exception:
        return None


def cross_validate(rows, folds=5, threshold=0.5, seed=0):
    """Перекрёстная проверка: модель проверяется на именах, которых не видела.

    Возвращает точность среди уверенных ответов и долю уверенных ответов.
    Точность важнее охвата: неуверенный ответ отбрасывается, и слой
    остаётся нераспознанным, а не получает неверный класс.
    """
    import random
    # Части делятся по очищенному имени: «Горизонтали» с разных листов
    # и цветовые варианты одного слоя попадают в одну часть. Иначе модель
    # проверяется на копиях того, что видела, и точность завышена.
    groups = sorted({normalize(r["name"]).lower() for r in rows})
    random.Random(seed).shuffle(groups)
    fold_of = {g: i % folds for i, g in enumerate(groups)}
    right = answered = total = 0
    for k in range(folds):
        test = {i for i, r in enumerate(rows)
                if fold_of[normalize(r["name"]).lower()] == k}
        idx = range(len(rows))
        train = [rows[i] for i in idx if i not in test]
        if not train:
            continue
        m = LayerModel().fit([r["name"] for r in train], [r["cls"] for r in train])
        for i in test:
            total += 1
            c, p = m.predict(rows[i]["name"])
            if p >= threshold:
                answered += 1
                right += c == rows[i]["cls"]
    return {
        "examples": len(rows),
        "accuracy_pct": round(100.0 * right / max(answered, 1), 1),
        "coverage_pct": round(100.0 * answered / max(total, 1), 1),
        "threshold": threshold,
    }


# Порог по перекрёстной проверке с разбиением по именам: на 0,5 модель
# ошибалась в каждом пятом уверенном ответе, на 0,6 — в каждом двенадцатом.
THRESHOLD = 0.6


# Нижний порог, при котором ответ принимается, если с ним согласна
# векторная модель (bge-m3). По перекрёстной проверке: одна буквенная
# модель от 0,6 — 92% верных при охвате 27%; вместе с согласием векторов
# от 0,3 — 89% верных при охвате 44%.
AGREE_THRESHOLD = 0.3


def classify_unknown(model, layer_class, threshold=THRESHOLD, embed_model=None):
    """Слоям без класса — класс от модели, если она уверена.

    embed_model — векторная модель (embed.EmbedModel): неуверенный ответ
    принимается, если она называет тот же класс.
    """
    out = {}
    if model is None:
        return out
    names = [n for n, c in layer_class.items() if c == "unknown"]
    second = embed_model.predict_many(names) if (embed_model and names) else None
    for i, name in enumerate(names):
        c, p = model.predict(name)
        if c in SKIP:
            continue
        # Согласие двух моделей не делает ответ надёжным в опасную сторону:
        # «ГП_контр_тип6» обе сочли газоном, по сходству с «ГП_контр_газон».
        # Неуверенный ответ принимается, только если это препятствие.
        from layers import PERMISSIVE
        agree = (second is not None and second[i][0] == c
                 and c not in PERMISSIVE and c != "annotation")
        if p >= threshold or (agree and p >= AGREE_THRESHOLD):
            out[name] = c
    return out
