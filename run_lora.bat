@echo off
rem Дообучение модели распознавания слоёв (LoRA) целиком, одной командой:
rem обучение -> слияние с базовой моделью -> модель Ollama -> проверка.
rem Журнал: logs\lora.log. Занимает около часа на RTX 5060 Ti.
cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
echo === %date% %time% обучение > logs\lora.log
if exist models\greenai-lora rmdir /s /q models\greenai-lora
if exist models\greenai-merged rmdir /s /q models\greenai-merged
if exist models\greenai-layers-lora.q8_0.gguf del models\greenai-layers-lora.q8_0.gguf
.venv-train\Scripts\python.exe src\train_lora.py >> logs\lora.log 2>&1 || goto :fail
echo === %date% %time% слияние и Ollama >> logs\lora.log
.venv-train\Scripts\python.exe src\lora_to_ollama.py >> logs\lora.log 2>&1 || goto :fail
echo === %date% %time% проверка >> logs\lora.log
.venv\Scripts\python.exe src\eval_llm.py --model greenai-layers-lora --holdout --n 150 >> logs\lora.log 2>&1
.venv\Scripts\python.exe src\eval_llm.py --model greenai-layers --holdout --n 150 >> logs\lora.log 2>&1
echo === %date% %time% ГОТОВО >> logs\lora.log
exit /b 0
:fail
echo === %date% %time% ОШИБКА >> logs\lora.log
exit /b 1
