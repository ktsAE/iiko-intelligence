@echo off
setlocal
pushd "%~dp0" || exit /b 1
"runtime\python.exe" -m iiko_intelligence.cli export-brief
set "REPORT_EXIT=%ERRORLEVEL%"
if "%REPORT_EXIT%"=="0" (
    echo Your report is in the reports folder. Review it before attaching it to ChatGPT.
    if /i not "%~1"=="/quiet" start "" explorer.exe "%CD%\reports"
) else (
    echo Run Setup-iiko.cmd first and check the message above.
)
popd
if /i not "%~1"=="/quiet" pause
exit /b %REPORT_EXIT%
