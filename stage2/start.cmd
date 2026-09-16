@echo off
REM Ручной запуск supervisor в консоли (для отладки). Штатно — задача SigmaSteamBot.
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" supervisor.py
