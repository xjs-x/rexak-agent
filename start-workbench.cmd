@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found. Please install project dependencies first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "scripts\start_workbench.py" %*
if errorlevel 1 pause
