@echo off
rem Runs Livery Swap from source. Most people should use LiverySwap.exe instead.
cd /d "%~dp0"
where python >nul 2>nul || (
  echo Python 3 was not found. Install it from https://www.python.org/downloads/
  echo or download LiverySwap.exe from the Releases page instead.
  pause
  exit /b 1
)
python liveryswap.py
if errorlevel 1 pause
