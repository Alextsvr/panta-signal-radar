@echo off
REM Backfill Panta trade tapes for all markets (read-only), log to logs\backfill_output.txt
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
.venv\Scripts\python.exe scripts\backfill_trades.py --export auto > logs\backfill_output.txt 2>&1
type logs\backfill_output.txt
echo.
echo Done. You can close this window.
pause
