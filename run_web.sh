#!/bin/sh
# Запуск веб-интерфейса под Linux и МосТех.ОС.
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
  .venv/bin/python src/webapp.py
else
  python3 src/webapp.py
fi
