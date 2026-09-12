@echo off
REM Trae la ultima version del proyecto. Doble clic para ejecutar.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe actualizar.py
) else (
  python actualizar.py
)
