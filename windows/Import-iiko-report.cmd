@echo off
setlocal
pushd "%~dp0" || exit /b 1
chcp 65001 >nul
set "PYTHONUTF8=1"
if not exist "runtime\python.exe" (
    echo Extract the entire Windows ZIP first.
    popd
    pause
    exit /b 1
)

if "%~1"=="" (
    "runtime\python.exe" -m iiko_intelligence.cli import-sales --interactive
) else (
    "runtime\python.exe" -m iiko_intelligence.cli import-sales --interactive --file "%~1"
)
set "IMPORT_EXIT=%ERRORLEVEL%"
if "%IMPORT_EXIT%"=="0" echo Report ready. Ask iiko_intelligence to check the new coverage in Codex.
if not "%IMPORT_EXIT%"=="0" echo Import did not finish. Check the message above and try again.
popd
pause
exit /b %IMPORT_EXIT%
