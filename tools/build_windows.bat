@echo off
REM ============================================================
REM  ODU Tracker — one-click Windows build (creates installers)
REM  Output: dist\ODU-Tracker.exe (portable) and, when Inno Setup
REM  is installed, dist\installer\ODU-Tracker-setup.exe
REM ============================================================
setlocal
cd /d "%~dp0.."

where py >nul 2>nul && (set PY=py -3) || (
  where python >nul 2>nul && (set PY=python) || (
    echo [!] Python 3 not found. Install from https://python.org and re-run.
    exit /b 1))

if not exist .venv-build ( %PY% -m venv .venv-build )
call .venv-build\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt || goto :fail
python tools\make_icon.py || goto :fail
python tools\build_app.py || goto :fail

REM ---- optional: real installer if Inno Setup is present ----
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
  if not exist dist\installer mkdir dist\installer
  %ISCC% packaging\windows\odu-tracker.iss && echo [ok] installer: dist\installer\ODU-Tracker-setup.exe
) else (
  echo [i] Inno Setup 6 not found — portable exe: dist\ODU-Tracker.exe
  echo     Get Inno Setup free from https://jrsoftware.org/isdl.php to build
  echo     a real setup.exe (it reads packaging\windows\odu-tracker.iss)
)
echo.
echo DONE. Copy dist\ to your PC and run the app.
exit /b 0
:fail
echo [x] build failed — check messages above.
exit /b 1
