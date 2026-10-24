@echo off
setlocal
rem Server uses only the Python standard library: prefer the project .venv, else the system Python 3
set "TASK_PYTHON=%~dp0.venv\Scripts\python.exe"
set "PY_ARGS="
if not exist "%TASK_PYTHON%" (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "TASK_PYTHON=py"
        set "PY_ARGS=-3"
    ) else (
        set "TASK_PYTHON=python"
    )
)
"%TASK_PYTHON%" %PY_ARGS% -u -X utf8 "%~dp0run_camlink.py" %*
set "TASK_EXIT=%ERRORLEVEL%"
pause
exit /b %TASK_EXIT%
