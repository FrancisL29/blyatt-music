@echo off
rem Lanzador de Blyatt: doble click y listo. Usa pyw (sin consola) si existe; si no, py minimizado.
cd /d "%~dp0"
rem cierra instancias previas para no dejar procesos viejos sirviendo codigo antiguo
taskkill /F /IM pythonw.exe >nul 2>nul
taskkill /F /IM pyw.exe >nul 2>nul
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw main.py
) else (
  start "Blyatt" /min py main.py
)
