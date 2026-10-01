@echo off
REM Panta Signal Radar - one-click setup + first real read-only API inspection.
REM Creates .venv, installs requirements, runs scripts\inspect_api.py.
REM Output is also written to logs\inspect_output.txt (API key is always masked).
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
set LOG=logs\inspect_output.txt
echo === %DATE% %TIME% === > %LOG%

if not exist .venv\Scripts\python.exe (
  echo Creating .venv ...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
)
if not exist .venv\Scripts\python.exe (
  echo ERROR: could not create .venv - is Python 3.10+ installed? >> %LOG%
  echo ERROR: could not create .venv - is Python 3.10+ installed?
  pause
  exit /b 1
)
.venv\Scripts\python.exe --version >> %LOG% 2>&1
echo Installing requirements (first run takes a minute) ...
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt >> %LOG% 2>&1
echo pip exit code: %ERRORLEVEL% >> %LOG%
echo Running inspect_api.py ...
.venv\Scripts\python.exe scripts\inspect_api.py >> %LOG% 2>&1
echo inspect exit code: %ERRORLEVEL% >> %LOG%
type %LOG%
echo.
echo Done. You can close this window.
pause
