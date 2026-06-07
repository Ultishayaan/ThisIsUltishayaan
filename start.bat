@echo off
REM PyCharm Projects Hub - launcher
setlocal
set "HERE=%~dp0"
cd /d "%HERE%"
python app.py %*
endlocal
