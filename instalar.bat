@echo off
REM Instalador para Windows. Doble clic para ejecutar.
REM Toda la logica esta en instalar.py, para que sea la misma en Windows y Mac.
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 goto sinpython

python instalar.py
goto :eof

:sinpython
echo.
echo  ============================================================
echo   No encontre Python en esta computadora.
echo  ============================================================
echo.
echo   1. Bajalo de  https://www.python.org/downloads/
echo   2. IMPORTANTE: en la primera pantalla del instalador,
echo      tilda la casilla "Add python.exe to PATH".
echo   3. Cerra esta ventana y volve a hacer doble clic aca.
echo.
pause
