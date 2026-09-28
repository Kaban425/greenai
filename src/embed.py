# -*- coding: utf-8 -*-
"""Смысловые векторы имён слоёв через Ollama (модель bge-m3).

Модель имён на кусочках букв (layer_model.py) узнаёт опечатки и чужие
префиксы, но не смысл: «Водовод» и «Трубопровод водоснабжения» для неё
разные слова. Векторная модель переводит имя в точку смыслового
пространства, где близкие по смыслу имена лежат рядом, — на любом языке
и в любых сокращениях, которые встречались в её обучении.

Работает локально через Ollama. Векторы кешируются в
config/layer_embeddings.npz: одно имя считается один раз.

    ollama pull bge-m3
"""
import json
import os
import urllib.request

import numpy as np

from layers import normalize, xref_part

MODEL = "bge-m3"
HOST = "http://127.0.0.1:11434"


def text_of(name):
    """Что векторизуется: очищенное имя слоя и хвост имени подосновы."""
    t = normalize(name)
    part = xref_part(name)
    return f"{t} ({part})" if part and part.lower() not in t.lower() else t


def _request(texts, host=HOST, model=MODEL, timeout=120, keep_alive=0):
    req = urllib.request.Request(
        host + "/api/embed",
        # keep_alive 0 на последней пачке: выгрузить модель из видеопамяти,
        # иначе она теснит языковую модель, и та уходит на процессор.
        # На промежуточных — держать: перезагрузка на каждую пачку из 64
        # имён превращала подсчёт векторов в минуты занятой видеокарты.
        data=json.dumps({"model": model, "input": texts,
                         "keep_alive": keep_alive},
                        ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))["embeddings"]


class Embedder:
    """Векторы имён с кешем на диске. Пустой результат, если Ollama или
    модель недоступны: вызывающий код тогда обходится без векторов."""

    def __init__(self, root, model=MODEL, host=HOST):
        self.path = os.path.join(root, "config", "layer_embeddings.npz")
        self.model, self.host = model, host
        self.cache = {}
        try:
            d = np.load(self.path)
            if str(d["model"]) == model:
                self.cache = dict(zip(d["texts"].tolist(), d["vecs"]))
        except Exception:
            pass
        self.ok = True

    def vectors(self, names):
        """Имена -> матрица нормированных векторов или None."""
        texts = [text_of(n) for n in names]
        todo = [t for t in dict.fromkeys(texts) if t not in self.cache]
        try:
            for i in range(0, len(todo), 64):
                chunk = todo[i:i + 64]
                last = i + 64 >= len(todo)
                got = _request(chunk, self.host, self.model,
                               keep_alive=0 if last else "30s")
                for t, v in zip(chunk, got):
                    v = np.asarray(v, dtype=np.float32)
                    self.cache[t] = v / (np.linalg.norm(v) or 1.0)
        except Exception:
            self.ok = False
            return None
        if todo:
            self.save()
        return np.vstack([self.cache[t] for t in texts]) if texts else None

    def coverage(self, names):
        """Доля имён, для которых векторы уже в кеше."""
        texts = [text_of(n) for n in names]
        return sum(t in self.cache for t in texts) / max(len(texts), 1)

    def save(self):
        try:
            texts = list(self.cache)
            np.savez_compressed(self.path, model=self.model,
                                texts=np.array(texts),
                                vecs=np.vstack([self.cache[t] for t in texts]))
        except Exception:
            pass


class EmbedModel:
    """Ближайшие по смыслу размеченные имена голосуют за класс.

    Уверенность — сходство с лучшим соседом, умноженное на согласие
    соседей: вариация знакомого имени даёт высокое сходство, незнакомое —
    низкое, и тогда модель отказывается отвечать.
    """

    def __init__(self, embedder, k=5):
        self.emb, self.k = embedder, k
        self.names, self.labels, self.mat = [], [], None

    def fit(self, names, labels):
        self.names, self.labels = list(names), list(labels)
        self.mat = self.emb.vectors(self.names)
        return self

    def neighbours_many(self, names):
        q = self.emb.vectors(names)
        if q is None or self.mat is None:
            return None
        sims = q @ self.mat.T
        k = min(self.k, sims.shape[1])
        idx = np.argsort(-sims, axis=1)[:, :k]
        return [[(float(sims[r, j]), self.labels[j], self.names[j])
                 for j in idx[r]] for r in range(len(names))]

    def predict_many(self, names):
        nbs = self.neighbours_many(names)
        if nbs is None:
            return None
        out = []
        for nb in nbs:
            vote = {}
            for sim, lab, _ in nb:
                vote[lab] = vote.get(lab, 0.0) + max(sim, 0.0)
            if not vote:
                out.append(("unknown", 0.0))
                continue
            cls = max(vote, key=vote.get)
            agree = vote[cls] / (sum(vote.values()) or 1.0)
            best = max(s for s, lab, _ in nb if lab == cls)
            out.append((cls, round(best * agree, 3)))
        return out
