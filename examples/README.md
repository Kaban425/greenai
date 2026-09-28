# Пример входа и выхода

Учебный участок улицы: граница работ, проезжая часть, тротуар, здание, сети
(водопровод, газопровод, кабель, канализация), опоры освещения, существующие
деревья. Чертёж сгенерирован скриптом `src/make_sample.py` — данных пилота
в репозитории нет.

* `sample_input/sample.dxf` — входной DXF.
* `sample_output/sample_GREEN_AI.dxf` — выходной DXF: исходные слои без
  изменений + слои `GREEN_AI_TREES`, `GREEN_AI_SHRUBS`, `GREEN_AI_ZONES`,
  `GREEN_AI_NOTES`.
* `sample_output/sample_PLAN_ONLY.dxf` — только посадки.
* `sample_output/sample_explain.json`, `.csv` — объяснение каждой посадки:
  определяющее ограничение, норма, факт, акт и пункт.
* `sample_output/sample_report.md` — отчёт; `sample_schedule.csv` — ведомость;
  `sample_plan.html` — интерактивная схема.

Повторить:

```bash
docker compose run --rm greenai python src/main.py \
    --input examples/sample_input/sample.dxf --outdir output/sample --plan-only
python src/verify_output.py examples/sample_input/sample.dxf examples/sample_output
```
