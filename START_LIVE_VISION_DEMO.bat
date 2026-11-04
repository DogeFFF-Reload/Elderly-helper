@echo off
setlocal
set "TASK_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%TASK_PYTHON%" (
    echo Project .venv is missing. Install code/requirements-vision.txt in the project environment first.
    pause
    exit /b 1
)
set "PYTHONPATH=%~dp0code"
"%TASK_PYTHON%" -m vision.integrated_demo --camera 0 %*
set "TASK_EXIT=%ERRORLEVEL%"
if not "%TASK_EXIT%"=="0" pause
exit /b %TASK_EXIT%
