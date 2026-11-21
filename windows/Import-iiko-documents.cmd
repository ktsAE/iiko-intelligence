@echo off
setlocal
pushd "%~dp0" || exit /b 1
chcp 65001 >nul
if "%~1"=="" (
    "runtime\python.exe" -m iiko_intelligence.cli import-documents --interactive
) else (
    "runtime\python.exe" -m iiko_intelligence.cli import-documents --interactive --file "%~1"
)
set "DOC_EXIT=%ERRORLEVEL%"
if "%DOC_EXIT%"=="0" echo Document list ready.
popd
pause
exit /b %DOC_EXIT%
