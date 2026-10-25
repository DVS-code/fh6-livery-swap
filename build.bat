@echo off
rem Builds dist\LiverySwap.exe (single file, no console window).
cd /d "%~dp0"

python -m pip install -r requirements.txt pyinstaller || goto :fail

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name LiverySwap ^
  --icon icon.ico ^
  --add-data "icon.ico;." ^
  --exclude-module numpy --exclude-module jinja2 --exclude-module markupsafe ^
  --exclude-module psutil --exclude-module setuptools --exclude-module pkg_resources ^
  --exclude-module win32com --exclude-module pythoncom --exclude-module pywintypes ^
  --exclude-module cffi --exclude-module pycparser --exclude-module packaging ^
  --exclude-module Crypto --exclude-module unittest --exclude-module pydoc --exclude-module doctest ^
  liveryswap.py || goto :fail

echo.
echo Built dist\LiverySwap.exe
exit /b 0

:fail
echo.
echo Build failed.
pause
exit /b 1
