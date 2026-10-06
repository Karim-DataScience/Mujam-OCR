@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py -3 extraire_prenoms_llm.py %*) || (python extraire_prenoms_llm.py %*)
if errorlevel 1 pause
