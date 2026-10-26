@echo off
REM ============================================================
REM  ODU Tracker - build the dedicated Electron desktop app
REM  (Windows NSIS installer + portable exe)
REM
REM  Run from the project root on a Windows machine:
REM     packaging\windows\build-electron.bat
REM
REM  Output:
REM     dist\electron\ODU-Tracker-Setup.exe     (installer)
REM     dist\electron\ODU-Tracker-Portable.exe
REM ============================================================
setlocal
cd /d "%~dp0..\.."

echo == using interpreter: %CD%\.venv\Scripts\python
if not exist ".venv\Scripts\python.exe" (
  echo == creating venv + installing requirements ^+ PyInstaller
  py -m venv .venv || exit /b 1
  .venv\Scripts\pip install -r requirements.txt pyinstaller || exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: python not found. Install Python 3.10+ first. 1>&2
  exit /b 1
)

echo == building headless engine (PyInstaller)
.venv\Scripts\python -m PyInstaller --noconfirm --clean ^
  --name odu-tracker-server ^
  --add-data "odu_tracker\web\static;static" ^
  --hidden-import waitress ^
  --noconsole ^
  tools\server_entry.py
if errorlevel 1 exit /b 1

echo == staging engine into packaging\electron\resources
if exist "packaging\electron\resources" rmdir /s /q "packaging\electron\resources"
mkdir "packaging\electron\resources\odu-tracker-server"
xcopy /e /i /y "dist\odu-tracker-server" "packaging\electron\resources\odu-tracker-server" || exit /b 1

pushd packaging\electron
if not exist node_modules (
  echo == npm install (electron + electron-builder)
  call npm install --no-audit --no-fund || exit /b 1
)
echo == electron-builder (Windows NSIS + portable)
call npx electron-builder --win --publish never || exit /b 1
popd

echo.
echo == done. artifacts:
dir dist\electron
exit /b 0
