@echo off
REM Panta Signal Radar: take one real snapshot from Panta, run tests, then start the dashboard.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist .venv\Scripts\python.exe (
  echo .venv missing - run setup_and_inspect.bat first.
  pause
  exit /b 1
)
if not exist logs mkdir logs
set LOG=logs\run_output.txt
echo === %DATE% %TIME% === > %LOG%
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt >> %LOG% 2>&1
echo --- sync 24/7 dataset from GitHub >> %LOG%
.venv\Scripts\python.exe scripts\sync_data.py >> %LOG% 2>&1
echo --- fetch_snapshot >> %LOG%
.venv\Scripts\python.exe scripts\fetch_snapshot.py >> %LOG% 2>&1
echo --- pytest >> %LOG%
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider >> %LOG% 2>&1
type %LOG%
echo.
echo Starting dashboard at http://localhost:8501  (close this window to stop)
.venv\Scripts\python.exe -m streamlit run app.py --server.headless false --browser.gatherUsageStats false
