@echo off
rem Kindle PW3 konyvszinkron inditasa. Ugyanabban a mappaban kell lennie,
rem mint a sync_kindle.py. Asztali parancsikont erre a fajlra erdemes tenni.
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "SCRIPT=%~dp0sync_kindle.py"

where py >nul 2>&1
if %errorlevel%==0 (
    py -3 "%SCRIPT%" %*
) else (
    python "%SCRIPT%" %*
)
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (echo Kesz.) else (echo Hibakod: %RC% - lasd a fenti uzeneteket.)
pause
exit /b %RC%
