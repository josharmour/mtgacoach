@echo off
setlocal enabledelayedexpansion

:: Change to the directory where this script lives
cd /d "%~dp0"

if not defined MTGACOACH_RUNTIME_ROOT (
    if defined LOCALAPPDATA (
        set "MTGACOACH_RUNTIME_ROOT=%LOCALAPPDATA%\mtgacoach"
    ) else (
        set "MTGACOACH_RUNTIME_ROOT=%~dp0runtime"
    )
)

:: Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo.
    echo Please install Python 3.10+ from https://python.org
    echo Make sure to check "Add Python to PATH" during installation
    pause
    exit /b 1
)

:: Hand off to the interactive setup wizard
python setup_wizard.py
if errorlevel 1 (
    echo.
    echo Setup wizard encountered an error.
    echo You can re-run install.bat after fixing the issue.
    echo.
    pause
    exit /b 1
)

echo.
echo Setup finished.
echo Launch mtgacoach with:
echo   launch.vbs  (double-click / shortcut)
echo   launch.bat  (from a console)
echo.
pause
