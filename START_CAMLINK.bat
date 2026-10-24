@echo off
setlocal
set "TASK_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%TASK_PYTHON%" (
    echo Project .venv is missing. Install code/requirements-vision.txt first.
    pause
    exit /b 1
)
"%TASK_PYTHON%" -u -X utf8 "%~dp0run_camlink.py" %*
set "TASK_EXIT=%ERRORLEVEL%"
pause
exit /b %TASK_EXIT%
