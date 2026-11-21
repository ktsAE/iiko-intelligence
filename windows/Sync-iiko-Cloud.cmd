@echo off
setlocal
pushd "%~dp0" || exit /b 1
chcp 65001 >nul
"runtime\python.exe" -m iiko_intelligence.cli sync-cloud-sales --interactive
set "SYNC_EXIT=%ERRORLEVEL%"
if "%SYNC_EXIT%"=="0" echo Sales snapshot ready.
popd
pause
exit /b %SYNC_EXIT%
