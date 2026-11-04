@echo off
setlocal
set "TASK_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%TASK_PYTHON%" set "TASK_PYTHON=python"
set "PYTHONPATH=%~dp0code"
if "%~1"=="" (
    "%TASK_PYTHON%" -m vision.eval_falls --latest-recording
) else (
    "%TASK_PYTHON%" -m vision.eval_falls %*
)
set "TASK_EXIT=%ERRORLEVEL%"
pause
exit /b %TASK_EXIT%
