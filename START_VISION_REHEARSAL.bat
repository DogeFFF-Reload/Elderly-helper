@echo off
setlocal
set "TASK_PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "TASK_PYTHON=%~dp0.venv\Scripts\python.exe"
"%TASK_PYTHON%" "%~dp0run_vision_rehearsal.py" %*
set "TASK_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %TASK_EXIT%
