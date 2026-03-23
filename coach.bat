@echo off
setlocal

:: Standard coach wrapper (keeps existing shortcut target stable)
cd /d "%~dp0"
set "ARENAMCP_DEFAULT_MODE=standard"
call launch.bat %*
exit /b %errorlevel%
