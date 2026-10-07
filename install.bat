@echo off
rem One-shot setup (Windows): venv + playwright + chromium
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo error: Python not found. Install Python 3.10+ first ^(https://www.python.org/downloads/^.^).
  exit /b 1
)

echo ==^> Creating virtual environment (.venv)
python -m venv .venv
call .venv\Scripts\activate.bat

echo ==^> Installing playwright
python -m pip install --upgrade pip
pip install -r requirements.txt

echo ==^> Downloading Chromium (Playwright build)
playwright install chromium

echo.
echo Setup complete. Next steps:
echo   1. Put your liked_videos.json (JSON array of video URLs) in this folder.
echo   2. Run:  .venv\Scripts\python.exe migrate_liked_videos.py
