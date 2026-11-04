@echo off
setlocal
set "TASK_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%TASK_PYTHON%" (
    echo Project .venv is missing. See docs/34 for setup.
    pause
    exit /b 1
)
if "%~1"=="" (
    "%TASK_PYTHON%" "%~dp0run_bedside_vision.py" --help
    pause
    exit /b 0
)
"%TASK_PYTHON%" "%~dp0run_bedside_vision.py" %*
set "TASK_EXIT=%ERRORLEVEL%"
if not "%TASK_EXIT%"=="0" pause
exit /b %TASK_EXIT%
