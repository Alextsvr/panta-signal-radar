@echo off
REM Push the local repo to GitHub (uses your Windows Git login). Log: logs\push_output.txt
cd /d "%~dp0"
if not exist logs mkdir logs
where git >nul 2>nul || (echo Git for Windows is not installed: https://git-scm.com/download/win & pause & exit /b 1)
git config --global --add safe.directory "%CD:\=/%" >nul 2>nul
echo --- tracked secrets check > logs\push_output.txt
git ls-files | findstr /i /x ".env" >> logs\push_output.txt && (echo ABORT: .env is tracked & pause & exit /b 1)
git push -u origin main >> logs\push_output.txt 2>&1
echo push exit code: %ERRORLEVEL% >> logs\push_output.txt
type logs\push_output.txt
pause
