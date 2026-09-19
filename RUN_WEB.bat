@echo off
title SIH26123 Edge Fleet - live web demo
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not on your PATH. Install Python 3.10+ or add it to PATH.
  pause
  exit /b 1
)
echo Starting the fleet dashboard on http://127.0.0.1:8765/
echo Leave this window open while you demo. Press Ctrl+C here to stop.
echo.
python scripts\run_web.py --open %*
echo.
echo Dashboard stopped.
pause
