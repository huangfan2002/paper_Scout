@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 goto python
py -3 -X utf8 app.py --open
goto end
:python
where python >nul 2>&1
if errorlevel 1 goto missing
python -X utf8 app.py --open
goto end
:missing
echo Python 3.10+ is required. Download: https://www.python.org/downloads/
:end
if errorlevel 1 pause
endlocal
