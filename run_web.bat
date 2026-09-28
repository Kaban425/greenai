@echo off
rem Запуск веб-интерфейса в один клик.
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe src\webapp.py
) else (
  python src\webapp.py
)
pause
