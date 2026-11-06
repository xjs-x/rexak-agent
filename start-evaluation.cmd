@echo off
chcp 65001 >nul
cd /d "%~dp0"
uv run --frozen --extra benchmark python "%~dp0scripts\start_tsecbench.py"
if errorlevel 1 pause
