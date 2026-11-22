@echo off
setlocal
pushd "%~dp0" || exit /b 1
chcp 65001 >nul
"runtime\python.exe" -m iiko_intelligence.cloud_profile
set "RESULT=%ERRORLEVEL%"
popd
pause
exit /b %RESULT%
