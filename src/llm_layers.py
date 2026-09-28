# -*- coding: utf-8 -*-
"""Классификация имён слоёв локальной языковой моделью.

Правила в layers.py точны на знакомых именах, но у каждой проектной
организации свои привычки, и на чужом чертеже часть слоёв остаётся без
класса. Модель разбирает именно их: список имён небольшой (100-200 штук),
это один запрос.

Работает через Ollama на localhost, без интернета — требование закрытого
контура МосТех.ОС соблюдается. Если Ollama не запущена, модуль молча
возвращает пустой результат, и расчёт идёт на одних правилах.

    ollama serve
    ollama pull qwen3:8b
    python src/main.py --input файл.dxf --llm

Ответ модели ограничен схемой JSON: структура гарантируется грамматикой
вывода, а не просьбой в запросе. Результат кешируется, повторный запуск
модель не ждёт.
"""
import json
import os
import urllib.error
import urllib.request

CLASSES = [
    "gas", "water", "sewer", "heating", "power_cable", "power_line",
    "road", "walkway", "building", "lawn", "existing_tree",
    "reference_planting", "lighting_pole", "well", "furniture",
    "site_boundary", "retaining_wall", "slope", "tram", "annotation",
    "unknown",
]

# Слой идентифицируется номером, а не именем. Раньше ответ сопоставлялся
# по имени буква в букву, и модель, переписав длинное тире в дефис или
# исправив опечатку, получала отказ: ответы молча выбрасывались, и из
# 65 слоёв принимался один. Номер модель переписать не может.
SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "cls": {"type": "string", "enum": CLASSES},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "cls", "confidence", "reason"],
            },
        }
    },
    "required": ["rows"],
}

# Знания о московских соглашениях об именах. Общие для запроса и для
# Modelfile (ollama_build.py): правка здесь попадает в обе модели сразу.
# Каждый абзац — ошибка, которую модель уже делала на реальных чертежах.
KNOWLEDGE = """Разделы проекта в именах слоёв вида ДВ_<раздел>_...:
  ГП — генеральный план: объекты на плане (борта, МАФ, павильоны).
  ПП — план покрытий, ДО — дорожная одежда: это покрытия. ПЧ — проезжая
    часть (road), ТР или «трот» — тротуар (walkway). Уширение, обочина,
    кромка укреплённой обочины — road.
  АКР — альбом конструктивных решений: поперечные профили и конструкции
    покрытий, нарисованные рядом с планом. ВСЁ из АКР — annotation,
    даже «ДВ_АКР_Газон» и «ДВ_АКР_Граница_работ». КДО — конструкция
    дорожной одежды, это не газон.
  ПОР — план организации рельефа, ОФР — оформление: annotation.
  ЭН, ЭС — электроснабжение и наружное освещение: кабели и трубы-футляры
    для кабелей (ПНД, ГНБ, РКЛ, КЛ) — power_cable; ВЛИ и СИП — power_line;
    опоры и светильники — lighting_pole; «освещённость» — расчёт
    освещённости сеткой цифр, это annotation.
Слова в именах:
  «сущ.» — существующий объект того же вида, а не дерево: «Водопровод
    сущ.» — water, «Сущ. опора» — lighting_pole, «ПЕШ.переход Сущ» —
    разметка, annotation. Деревом слой делают слова дерево, кустарник,
    дендро или название породы.
  Колодец, люк, камера, решётка ливнесточная, ДК, СК — well, даже если
    в имени названа сеть: «колодец_ТР_водосток» — это well.
  Горизонтали, отметки, пикеты, GRADES, PIKETS, уклоноуказатели,
    вспомогательные и непечатаемые слои, условные обозначения,
    размеры, выноски, рамки листов — annotation.
  Демонтаж — объекта уже не будет: annotation. Но «замена», «ремонт»,
    «установка», «устройство» — объект будет: класс по самому объекту.
  «Тр. ПЭ» — полиэтиленовая труба, а не тротуар.
Физические объекты — никогда не annotation:
  знаки, дорожные знаки, светофоры, светильники, опоры — lighting_pole;
  мосты, стелы, памятники, павильоны, навесы — building;
  железные дороги, трамвайные пути — tram; парапеты, ограды — furniture;
  береговая линия, откосы — slope.
Топоплан (tp): «Граница площадки» — край площадки с покрытием, walkway,
  а не граница работ; «Полоса деревьев», «Отдельно стоящее дерево» —
  existing_tree. Дендроплан: «ДП_<порода>_план» — проектная посадка,
  reference_planting; с «сущ» — existing_tree.
Civil 3D (стандарт NCS): C-ANNO, C-PROP, C-ROAD-TEXT/LABL/PROF/SECT/LINE
  (ось трассы) — annotation; V-CTRL, V-SURV — ходы съёмки, annotation;
  V-NODE-TREE — existing_tree, V-NODE-POLE — lighting_pole, V-NODE-SSWR —
  sewer, V-NODE-STRM/WATR — water, V-NODE-NGAS — gas; A-BLDG — building.
Чертежи из PDF: «Новый_<имя>_Ном._пера__N» — смысл только в <имя>.
  «Растения фр1», «Растения 2-2» — проектные посадки по фрагментам,
  reference_planting.
Олимпийская деревня и ей подобные: PLNT_*, PL_TREE_*, PL_BUSH_*,
  «Посадочная яма» — проектные посадки, reference_planting; DENDRO_* —
  существующие, existing_tree. Топоним в имени («Олимпийская деревня»,
  «Лесная улица») класса не задаёт.
Стройгенплан (подъёмные краны, бытовки), охранные зоны, объекты
  культурного наследия, маршруты ГПТ — annotation. ЭЗС (зарядные
  станции) — building. «Пересадка» деревьев — removed_tree.
Осторожность: lawn и site_boundary определяют, ГДЕ сажать деревья.
  Ошибка в них ставит деревья на асфальт. Назначай их, только если в
  имени прямо сказано «газон», «цветник» или «граница работ»; иначе
  unknown."""


PROMPT = """Ты разбираешь слои чертежа благоустройства города Москвы.
Для каждого имени слоя определи класс объекта.

Классы:
  gas — газопровод
  water — водопровод, водосток, дренаж
  sewer — канализация
  heating — теплосеть, коллектор
  power_cable — кабель силовой или связи
  power_line — воздушная линия электропередачи
  road — проезжая часть, бортовой камень, парковка
  walkway — тротуар, дорожка, площадка с покрытием
  building — здание, сооружение, павильон остановки
  lawn — газон, цветник, зелёная поверхность
  existing_tree — существующие деревья и кустарники
  reference_planting — проектируемые посадки
  lighting_pole — опора освещения, знак, светофор
  well — колодец, люк, дождеприёмник
  furniture — малые формы, ограждения, скамьи, урны
  site_boundary — граница работ, красные линии
  retaining_wall — подпорная стенка
  slope — откос, терраса
  tram — трамвайные пути
  annotation — оформление: размеры, тексты, рамки, экспликации
  unknown — определить невозможно

Правила разбора:
  Коды Мосгоргеотреста: В1 и В2 вода, К1 канализация, К2 водосток,
  Т1 и Т2 теплосеть, Г газ, КЛС кабель связи, ДК дождеприёмный колодец.
  Часть имени до знаков $0$ — это имя внешней ссылки, его игнорируй.
  В кодировке ДВ_ПП: ТР тротуар, ПЧ проезжая часть, АБ асфальтобетон.
  Оборот «за счёт» указывает, что было раньше: значим результат, то есть
  часть имени до этого оборота.
  В квадратных скобках после имени — описание геометрии слоя. Им
  пользуйся, когда имя непонятно: замкнутые круги 3-6 м — кроны деревьев,
  круги 0,3-0,9 м — колодцы, длинные линии — сети или кромки.
  Если уверенности нет — ставь unknown и confidence ниже 0.5.
{knowledge}
Ответ: для каждой строки верни её номер id, класс cls, уверенность
confidence от 0 до 1 и короткое пояснение reason — по какому признаку
в имени ты решил. Номер копируй точно, имя слоя в ответ не пиши.

Слои (номер. имя):
"""


def _cache_path(root):
    return os.path.join(root, "config", "llm_layers_cache.json")


def load_cache(root):
    try:
        with open(_cache_path(root), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(root, data):
    try:
        os.makedirs(os.path.join(root, "config"), exist_ok=True)
        with open(_cache_path(root), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    except Exception:
        pass


def available(host="http://127.0.0.1:11434", timeout=2.0):
    """Запущена ли Ollama и какие модели доступны."""
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def _model_size(name):
    """Грубая оценка размера модели по имени: 14b > 7b > 3b."""
    import re as _re
    m = _re.search(r"(\d+(?:\.\d+)?)\s*b\b", name.lower())
    return float(m.group(1)) if m else 0.0


def pick_model(models, preferred):
    """Своя модель со знаниями о слоях, если собрана; иначе самая крупная."""
    if not models:
        return preferred
    own = [m for m in models if m.split(":")[0] == "greenai-layers"]
    if own:
        return own[0]
    big = max(models, key=_model_size)
    if preferred in models and _model_size(preferred) >= _model_size(big):
        return preferred
    return big


def describe(geoms):
    """Короткое описание геометрии слоя для модели: сколько, какой формы,
    какого размера. По одному имени «ГП_изм_3» не понять ничего, а
    «14 замкнутых кругов радиусом 3 м» — явно кроны деревьев."""
    import numpy as _np
    n = len(geoms)
    if not n:
        return ""
    closed, sizes, lengths = 0, [], []
    for g in geoms[:400]:
        try:
            c = _np.asarray(g.coords, dtype=float)
        except Exception:
            try:
                c = _np.asarray(g.exterior.coords, dtype=float)
            except Exception:
                continue
        if len(c) < 2:
            continue
        seg = _np.hypot(*_np.diff(c, axis=0).T).sum()
        lengths.append(seg)
        if len(c) > 3 and _np.hypot(*(c[0] - c[-1])) < 1e-3 + 0.01 * seg:
            closed += 1
            sizes.append(max(_np.ptp(c[:, 0]), _np.ptp(c[:, 1])))
    parts = [f"{n} объектов"]
    if lengths:
        parts.append(f"средняя длина {float(_np.median(lengths)):.1f} м")
    if closed:
        parts.append(f"замкнутых {100*closed/max(len(lengths),1):.0f}%, "
                     f"размер {float(_np.median(sizes)):.1f} м")
    return ", ".join(parts)


OWN_MODEL = "greenai-layers"
# Модели с режимом рассуждений: без think=false они пишут рассуждение
# вместо JSON, и ответ по схеме ломается.
THINKING = ("qwen3", "deepseek-r1", "gpt-oss", "magistral")


def _shots(title, pairs):
    if not pairs:
        return ""
    return (f"\n{title}\n" + "\n".join(f"  {n} -> {c}" for n, c in pairs) + "\n")


def _drawing_examples(examples, limit=30):
    """Образцы из этого же чертежа, по несколько на класс."""
    picked, per_cls = [], {}
    for name, cls in (examples or {}).items():
        if per_cls.get(cls, 0) < 3:
            picked.append((name, cls))
            per_cls[cls] = per_cls.get(cls, 0) + 1
        if len(picked) >= limit:
            break
    return picked


class Library:
    """Размеченная выборка для подбора похожих примеров к каждому слою.

    Модель не дообучается, но каждый прогнанный чертёж пополняет выборку,
    и к непонятному имени в запрос подкладываются самые похожие уже
    размеченные имена. Так модель «учится» на накопленном опыте без
    переобучения. Примеры с тем же очищенным именем, что и у вопроса, не
    берутся: иначе модель списывает ответ, а не рассуждает.
    """

    def __init__(self, rows, embedder=None):
        from layer_model import LayerModel
        from layers import normalize
        self._norm = normalize
        self.embedder = embedder
        seen, names, labels = set(), [], []
        for r in rows:
            key = normalize(r["name"]).lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(r["name"])
            labels.append(r["cls"])
        self.model = LayerModel(k=4).fit(names, labels) if names else None
        # По смыслу (bge-m3): находит «Трубопровод водоснабжения» к «Водоводу»,
        # чего буквенная модель не видит. Если векторов нет — только буквы.
        self.emodel = None
        if embedder is not None and names:
            from embed import EmbedModel
            em = EmbedModel(embedder, k=6).fit(names, labels)
            self.emodel = em if em.mat is not None else None

    @classmethod
    def load(cls, root):
        try:
            from layer_model import load_dataset
            rows = load_dataset(root)
        except Exception:
            rows = []
        return cls(rows) if rows else None

    def similar(self, names, per_name=3, limit=45, min_sim=0.15):
        if self.model is None:
            return []
        out, seen = [], set()
        sem = self.emodel.neighbours_many(names) if self.emodel else None
        for q, name in enumerate(names):
            own = self._norm(name).lower()
            cands = self.model.neighbours(name)[:per_name + 1]
            if sem:
                # два лучших по смыслу — порог выше: сходства векторов
                # в среднем выше буквенных
                cands += [c for c in sem[q][:per_name] if c[0] >= 0.6][:2]
            for sim, lab, n in cands:
                if sim < min_sim or n in seen or self._norm(n).lower() == own:
                    continue
                seen.add(n)
                out.append((n, lab))
        return out[:limit]


def _can_think(host, model):
    """Умеет ли модель рассуждать. По имени не видно: greenai-layers,
    собранная на qwen3, рассуждает, хотя «qwen3» в имени нет."""
    try:
        req = urllib.request.Request(
            host + "/api/show", data=json.dumps({"model": model}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            caps = json.loads(r.read().decode("utf-8")).get("capabilities") or []
        return "thinking" in caps
    except Exception:
        return any(t in model.lower() for t in THINKING)


def _chat(host, payload, timeout):
    req = urllib.request.Request(
        host + "/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return json.loads(data["message"]["content"]).get("rows", [])


def _show(name):
    """Имя слоя для модели: сам слой отдельно от имени внешней ссылки.

    «Газон|ИНЖ_Кл» — кабель из файла-ссылки «Газон», но модель читала
    имя ссылки как смысл слоя и называла кабели и фонтаны газоном.
    Имя ссылки остаётся подсказкой («…up» у ДЖКХ — подземные сети).
    """
    import re
    from layers import XREF_PREFIX, normalize, split_color
    raw = split_color(name)[0]
    m = XREF_PREFIX.match(raw)
    tail = normalize(name)
    if not m or not tail:
        # «Новый_Цветник фр2_Ном._пера__150» из PDF: смысл — «Цветник фр2»
        return tail if tail and "пера" in name else name
    ref = m.group(0).rstrip("|")
    ref = re.sub(r"\$\d+\$$", "", ref).strip()
    return f"{tail}  (слой из внешней ссылки «{ref}»)"


def suggest(layers, model="qwen3:8b", host="http://127.0.0.1:11434",
            timeout=300.0, verbose=True, examples=None, geometry=None,
            library=None, exact=False):
    """Предложения модели по каждому слою: класс, уверенность, пояснение.

    Возвращает (список строк, сообщение). Строка: layer, cls, confidence,
    reason. Используется и расчётом, и страницей разметки, где человек
    принимает или отклоняет каждое предложение.
    library — размеченная выборка (Library): к каждой пачке слоёв в
    запрос добавляются самые похожие уже известные имена.
    """
    layers = [x for x in dict.fromkeys(layers) if x]
    if not layers:
        return [], "слоёв нет"
    models = available(host)
    if not models:
        return [], "Ollama не запущена: откройте её из меню Пуск"
    # exact — взять ровно эту модель (для проверки), иначе своя, если собрана
    if not (exact and any(m == model or m == model + ":latest" for m in models)):
        model = pick_model(models, model)
    # Своя модель несёт знания в системном сообщении, повторять их незачем.
    knowledge = "" if model.split(":")[0] == OWN_MODEL else "\n" + KNOWLEDGE + "\n"
    base_prompt = PROMPT.replace("{knowledge}", knowledge)
    thinks = _can_think(host, model)

    # Образцы из этого же чертежа: слои, уже распознанные правилами.
    # По ним модель видит, как именно этот проектировщик называет слои,
    # и переносит закономерность на непонятные имена.
    local = _shots("Примеры из этого же чертежа, где класс уже известен "
                   "(используй как образец соглашения об именах):",
                   _drawing_examples(examples))

    if library is not None and library.emodel is not None:
        # векторы всех имён — одним заходом, до языковой модели: иначе
        # две модели попеременно вытесняют друг друга из видеопамяти
        library.emodel.emb.vectors(layers)
    out = []
    batch = 40                      # крупными пачками модель путается
    for start in range(0, len(layers), batch):
        chunk = layers[start:start + batch]
        near = _shots("Похожие слои из других чертежей с проверенным "
                      "классом:", library.similar(chunk) if library else [])
        listing = "\n".join(
            f"{i}. {_show(name)}" + (f"  [{geometry[name]}]"
                              if geometry and geometry.get(name) else "")
            for i, name in enumerate(chunk, 1))
        payload = {"model": model, "stream": False,
                   "options": {"temperature": 0},
                   "format": SCHEMA,
                   "messages": [{"role": "user",
                                 "content": base_prompt.replace(
                                     "\nСлои (номер. имя):",
                                     local + near + "\nСлои (номер. имя):")
                                 + listing}]}
        if thinks:
            payload["think"] = False
        try:
            if verbose:
                print(f"      модель {model}: слои {start+1}-{start+len(chunk)} "
                      f"из {len(layers)} ...", flush=True)
            rows = _chat(host, payload, timeout)
        except Exception as e:
            return out, f"модель не ответила: {e}"
        seen = set()
        for row in rows:
            try:
                k = int(row.get("id")) - 1
            except (TypeError, ValueError):
                continue
            if not (0 <= k < len(chunk)) or k in seen:
                continue
            seen.add(k)
            cls = row.get("cls")
            if cls not in CLASSES:
                cls = "unknown"
            out.append({"layer": chunk[k], "cls": cls,
                        "confidence": float(row.get("confidence") or 0),
                        "reason": str(row.get("reason") or "")[:200]})
        # слои, по которым модель промолчала, — тоже в выдачу, как неизвестные
        for k, name in enumerate(chunk):
            if k not in seen:
                out.append({"layer": name, "cls": "unknown", "confidence": 0.0,
                            "reason": "модель не дала ответа"})
    return out, f"модель {model}"


# Версия запроса: ответы, полученные старым запросом, в кеше не
# переиспользуются. Старый кеш хранил, например, «ДВ_АКР_КДО -> lawn».
PROMPT_VERSION = "2026-09-28"


def classify(layers, model="qwen3:8b", host="http://127.0.0.1:11434",
             root=".", timeout=300.0, min_confidence=0.5, verbose=True,
             examples=None, geometry=None, library=None):
    """Имена слоёв -> {имя: класс} для расчёта. Пусто, если модель недоступна.

    Ответы ниже порога уверенности отбрасываются: лучше оставить слой
    нераспознанным, чем молча отнести не к тому классу.
    """
    layers = [x for x in dict.fromkeys(layers) if x]
    if not layers:
        return {}
    cache = load_cache(root)
    key = f"{model}@{PROMPT_VERSION}"
    known = {x: cache[key][x] for x in layers
             if key in cache and x in cache[key]}
    todo = [x for x in layers if x not in known]
    if verbose and known:
        print(f"      модель: из кеша {len(known)} слоёв", flush=True)

    got, low = {}, 0
    if todo:
        rows, msg = suggest(todo, model=model, host=host, timeout=timeout,
                            verbose=verbose, examples=examples,
                            geometry=geometry, library=library)
        if not rows and verbose:
            print(f"      {msg}, работаю на правилах", flush=True)
        for r in rows:
            if r["cls"] == "unknown" or r["confidence"] < min_confidence:
                got[r["layer"]] = "unknown"
                low += 1
            else:
                got[r["layer"]] = r["cls"]
        cache.setdefault(key, {}).update(got)
        save_cache(root, cache)

    result = {k: v for k, v in {**known, **got}.items() if v != "unknown"}
    if verbose:
        print(f"      модель распознала {len(result)} из {len(layers)} слоёв, "
              f"отклонено по низкой уверенности {low}", flush=True)
    return result
