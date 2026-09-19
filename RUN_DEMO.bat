@echo off
title SIH26123 Edge Fleet - live demo
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not on your PATH. Install Python 3.10+ or add it to PATH.
  pause
  exit /b 1
)
python scripts\run_demo.py %*
echo.
echo Dashboard stopped.
pause
