# Презентация GreenAI (шаблон ЛЦТ 2026)

Готовый файл: `GreenAI_ЛЦТ2026.pptx`. Шрифт шаблона — Montserrat: установите
его на компьютер, с которого показываете (https://fonts-online.ru/fonts/montserrat).

Слайды о команде (2–4) заполняются из `tools/deck/team/team.json` и фото в той же
папке — она не входит в репозиторий, потому что там телефон и почта. Готовая
презентация тоже не хранится в git: её отправляют отдельной ссылкой.

## Пересборка (после нового прогона по улицам цифры обновятся)

Нужны Docker и образ с LibreOffice и python-pptx (`tools/deck/Dockerfile`),
шаблон организаторов `tools/deck/template.pptx` и скрипты навыка pptx в
`tools/deck/skill` (папка `tools/` в репозиторий не входит).

```bash
docker build -t greenai-deck tools/deck
docker run --rm -v "$PWD:/work" greenai-deck sh -c '
  python3 docs/deck/lct_structure.py tools/deck/template.pptx tools/deck/lct_base.pptx &&
  python3 docs/deck/lct_fill.py tools/deck/lct_base.pptx docs/deck/GreenAI_ЛЦТ2026.pptx'
```

* `lct_structure.py` — выбирает и упорядочивает макеты шаблона;
* `lct_fill.py` — текст, диаграммы и картинки; цифры берутся из
  `config/placement_model.json` и `config/batch_report.json`;
* `render_plan.py` — картинки «до/после» по результату расчёта.
