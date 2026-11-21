@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "PYTHONUTF8=1"
echo Checking the bundled Python and connecting iiko Intelligence...
"runtime\python.exe" -m iiko_intelligence.cli setup-local
if errorlevel 1 goto failed

echo.
echo Setup verified. Open this folder in local Codex in the ChatGPT desktop app.
echo Trust the folder when prompted, then type /mcp and look for iiko_intelligence.
popd
if /i not "%~1"=="/quiet" pause
exit /b 0

:failed
echo.
echo Setup did not finish. The error above explains what needs attention.
popd
if /i not "%~1"=="/quiet" pause
exit /b 1
