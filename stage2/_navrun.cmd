@echo off
REM Запускается задачей SigmaNav в интерактивной сессии. Аргументы — из nav_cmd.txt.
cd /d "%~dp0"
set "ARGS="
set /p ARGS=<nav_cmd.txt
"%~dp0venv\Scripts\python.exe" nav.py %ARGS% > nav_out.txt 2>&1
