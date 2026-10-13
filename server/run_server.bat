@echo off
REM Double-click to start the WebRTC server (wraps run_server.ps1).
REM Extra arguments are passed through, e.g.:  run_server.bat -Port 3001 -Force
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_server.ps1" %*
echo.
echo [server] stopped (exit code %ERRORLEVEL%)
pause
