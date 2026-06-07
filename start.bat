@echo off
REM This Is Ultishayaan - launcher
setlocal
set "HERE=%~dp0"
cd /d "%HERE%"
python app.py %*
endlocal
