@echo off
setlocal
pushd "%~dp0" || exit /b 1
chcp 65001 >nul
if "%~1"=="" (
    "runtime\python.exe" -m iiko_intelligence.cli import-stock --interactive
) else (
    "runtime\python.exe" -m iiko_intelligence.cli import-stock --interactive --file "%~1"
)
set "STOCK_EXIT=%ERRORLEVEL%"
if "%STOCK_EXIT%"=="0" echo Warehouse snapshot ready.
popd
pause
exit /b %STOCK_EXIT%
