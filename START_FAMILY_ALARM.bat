@echo off
setlocal
set "TASK_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%TASK_PYTHON%" (
    echo Project .venv is missing. Install code/requirements-vision.txt first.
    pause
    exit /b 1
)
if "%~1"=="" (
    echo Usage: START_FAMILY_ALARM.bat --camera 0 --mode live --preview
    echo Default mode is OFF. --mode dry checks format; --mode live creates alarms.
    "%TASK_PYTHON%" "%~dp0run_family_alarm.py" --help
    pause
    exit /b 0
)
"%TASK_PYTHON%" "%~dp0run_family_alarm.py" %*
set "TASK_EXIT=%ERRORLEVEL%"
if not "%TASK_EXIT%"=="0" pause
exit /b %TASK_EXIT%
