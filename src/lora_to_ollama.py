# -*- coding: utf-8 -*-
"""Подключение дообученной модели к Ollama.

    .venv-train\\Scripts\\python src\\lora_to_ollama.py

Ollama с версии 0.3x не принимает отдельные адаптеры LoRA («LoRA adapters
are no longer supported»), а сжатие при импорте safetensors у неё работает
только через MLX (Mac). Поэтому:

1. models/greenai-lora (адаптер) + исходные 16-битные веса базовой модели
   -> models/greenai-merged (safetensors). Веса Unsloth скачивает сам;
2. конвертер llama.cpp -> models/greenai-layers-lora.q8_0.gguf
   (git clone --depth 1 https://github.com/ggml-org/llama.cpp tools/llama.cpp);
3. ollama create greenai-layers-lora из этого GGUF.

Проверка: python src/eval_llm.py --model greenai-layers-lora --holdout
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER = os.path.join(ROOT, "models", "greenai-lora")
MERGED = os.path.join(ROOT, "models", "greenai-merged")
GGUF = os.path.join(ROOT, "models", "greenai-layers-lora.q8_0.gguf")
CONVERT = os.path.join(ROOT, "tools", "llama.cpp", "convert_hf_to_gguf.py")
NAME = "greenai-layers-lora"


def merge():
    from unsloth import FastLanguageModel
    model, tok = FastLanguageModel.from_pretrained(
        ADAPTER, max_seq_length=2048, load_in_4bit=True)
    model.save_pretrained_merged(MERGED, tok, save_method="merged_16bit")


def main():
    if not os.path.isdir(ADAPTER):
        raise SystemExit("Адаптера нет: сначала src/train_lora.py")
    if not os.path.isfile(os.path.join(MERGED, "config.json")):
        print("Вливаю адаптер в базовую модель ...", flush=True)
        merge()
    if not os.path.isfile(GGUF):
        if not os.path.isfile(CONVERT):
            raise SystemExit("Нет tools/llama.cpp: git clone --depth 1 "
                             "https://github.com/ggml-org/llama.cpp tools/llama.cpp")
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   PYTHONPATH=os.path.join(ROOT, "tools", "llama.cpp", "gguf-py"))
        # q8_0 конвертер делает сам; 4B в q8_0 — 4,3 ГБ, в 8 ГБ помещается
        if subprocess.run([sys.executable, CONVERT, MERGED, "--outfile", GGUF,
                           "--outtype", "q8_0"], env=env).returncode:
            raise SystemExit("Конвертация в GGUF не удалась")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from llm_layers import PROMPT
    classes = PROMPT.split("Классы:", 1)[1].split("Правила разбора:", 1)[0]
    system = ("Ты классифицируешь слои чертежей благоустройства Москвы. "
              "Отвечай строго по схеме: номер слоя, класс, уверенность, "
              "короткое пояснение.\n\nКлассы:" + classes)
    mf = os.path.join(ROOT, "config", "Modelfile.lora")
    with open(mf, "w", encoding="utf-8") as f:
        f.write(f"FROM {GGUF}\nPARAMETER temperature 0\n"
                f"PARAMETER num_ctx 8192\n"
                f'SYSTEM """{system}"""\n')
    exe = shutil.which("ollama") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
    r = subprocess.run([exe, "create", NAME, "-f", mf])
    if r.returncode:
        raise SystemExit("ollama create не удалась")
    print(f"\nГотово: модель {NAME}. Проверка:\n"
          f"  python src/eval_llm.py --model {NAME} --holdout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
