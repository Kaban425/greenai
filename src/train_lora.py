# -*- coding: utf-8 -*-
"""Дообучение модели распознавания слоёв (QLoRA) на накопленной выборке.

Запускается в отдельном окружении с PyTorch и Unsloth, чтобы тяжёлые
библиотеки не попадали в рабочее:

    .venv-train\\Scripts\\python src\\train_lora.py
    .venv-train\\Scripts\\python src\\train_lora.py --epochs 2 --base unsloth/Qwen3-8B-unsloth-bnb-4bit

Что делает:
  1. делит config/layer_dataset.jsonl на обучение и отложенную проверку
     по очищенному имени (10%); список проверочных имён — в
     config/lora_split.json, eval_llm.py --holdout проверяет только на них;
  2. собирает примеры в том же виде, в каком модель спрашивают в расчёте:
     запрос llm_layers.PROMPT с пачкой пронумерованных имён, ответ — JSON
     по схеме;
  3. обучает адаптер LoRA и сохраняет его в models/greenai-lora.

Подключение к Ollama — src/lora_to_ollama.py.
"""
import argparse
import hashlib
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layer_model as LM                                  # noqa: E402
from layers import ASSIGNABLE, normalize                  # noqa: E402
from llm_layers import PROMPT                             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPLIT = os.path.join(ROOT, "config", "lora_split.json")
OUT = os.path.join(ROOT, "models", "greenai-lora")
BATCH_NAMES = 20
# Короткое пояснение к ответу: модель учится называть признак, а не
# сочинять. Для обучения достаточно класса по-русски.
REASON = {"annotation": "оформление", "unknown": "по имени не понять"}


def is_test(key):
    """Детерминированное деление: одно и то же имя всегда в одной части."""
    return int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % 10 == 0


def build(rows, seed=0, variants=2):
    """Пары (запрос, ответ) и список проверочных имён."""
    by_key = {}
    for r in rows:
        by_key.setdefault(normalize(r["name"]).lower(), []).append(r)
    rnd = random.Random(seed)
    train, test = [], []
    for key, rs in by_key.items():
        if is_test(key):
            test.append(rs[0]["name"])
            continue
        rnd.shuffle(rs)
        # до двух написаний одного имени: с префиксом подосновы и без
        train += rs[:variants]
    rnd.shuffle(train)
    prompt = PROMPT.replace("{knowledge}", "")
    CLASS_RU = dict(ASSIGNABLE)
    pairs = []
    for i in range(0, len(train), BATCH_NAMES):
        chunk = train[i:i + BATCH_NAMES]
        listing = "\n".join(f"{k}. {r['name']}" for k, r in enumerate(chunk, 1))
        answer = {"rows": [{"id": k, "cls": r["cls"], "confidence": 0.9,
                            "reason": REASON.get(r["cls"],
                                                 CLASS_RU.get(r["cls"], r["cls"]))}
                           for k, r in enumerate(chunk, 1)]}
        pairs.append((prompt + listing, json.dumps(answer, ensure_ascii=False)))
    return pairs, test


def main():
    ap = argparse.ArgumentParser()
    # 4B: 8B в 4 битах на 8 ГБ видеопамяти с запасом на обучение не
    # помещается, если рабочий стол уже занял полтора гигабайта
    ap.add_argument("--base", default="unsloth/Qwen3-4B-unsloth-bnb-4bit")
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--max-len", type=int, default=2048)
    ap.add_argument("--dry-run", action="store_true",
                    help="только собрать примеры и показать размер")
    args = ap.parse_args()

    LM.refresh_dataset(ROOT)
    rows = LM.load_dataset(ROOT)
    pairs, test = build(rows)
    os.makedirs(os.path.dirname(SPLIT), exist_ok=True)
    with open(SPLIT, "w", encoding="utf-8") as f:
        json.dump({"test": test}, f, ensure_ascii=False)
    print(f"Выборка: {len(rows)} примеров; обучающих запросов {len(pairs)} "
          f"по {BATCH_NAMES} имён, отложено для проверки {len(test)} имён")
    if args.dry_run:
        print(pairs[0][0][-600:], "\n---\n", pairs[0][1][:400])
        return 0

    from unsloth import FastLanguageModel
    from unsloth.chat_templates import get_chat_template
    # Бюджет памяти на функцию потерь Unsloth берёт как половину свободной
    # видеопамяти в момент первого вызова — когда модель уже заняла почти
    # всё, это «ноль», и обучение падает. На 8 ГБ задаём его явно.
    import unsloth_zoo.fused_losses.cross_entropy_loss as _ce
    _ce._free_target_gb = lambda: 0.5
    from datasets import Dataset
    from trl import SFTConfig, SFTTrainer

    model, tok = FastLanguageModel.from_pretrained(
        args.base, max_seq_length=args.max_len, load_in_4bit=True)
    model = FastLanguageModel.get_peft_model(
        model, r=args.rank, lora_alpha=args.rank, lora_dropout=0.0,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth", random_state=0)
    tok = get_chat_template(tok, chat_template="qwen3")

    def prompt_of(q):
        # без рассуждений: в расчёте модель зовут с think=false
        return tok.apply_chat_template(
            [{"role": "user", "content": q}], tokenize=False,
            add_generation_prompt=True, enable_thinking=False)

    # Запрос и ответ раздельно: потери считаются только по ответу. Когда
    # учили на всём тексте, девять десятых сигнала уходило на заучивание
    # неизменного запроса, и модель научилась лишь отвечать «оформление».
    ds = Dataset.from_list([{"prompt": prompt_of(q),
                             "completion": a + tok.eos_token}
                            for q, a in pairs])
    trainer = SFTTrainer(
        model=model, tokenizer=tok, train_dataset=ds,
        args=SFTConfig(
            completion_only_loss=True, max_length=args.max_len,
            per_device_train_batch_size=1, gradient_accumulation_steps=8,
            num_train_epochs=args.epochs, learning_rate=2e-4,
            warmup_ratio=0.05, lr_scheduler_type="cosine",
            logging_steps=10, optim="adamw_8bit", weight_decay=0.01,
            output_dir=os.path.join(ROOT, "models", "lora_runs"),
            save_strategy="no", report_to="none", seed=0))
    trainer.train()
    model.save_pretrained(OUT)
    tok.save_pretrained(OUT)
    print(f"Адаптер сохранён: {os.path.relpath(OUT, ROOT)}")
    print("Дальше: .venv-train\\Scripts\\python src\\lora_to_ollama.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
