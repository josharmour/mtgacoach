@echo off
setlocal

title ArenaMCP Launcher

:: Change to script directory (handles running from shortcut)
cd /d "%~dp0"

:: Prefer venv Python; fall back to system Python.
if exist "venv\Scripts\python.exe" (
    set "PY=venv\Scripts\python.exe"
) else (
    python --version >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python not found in PATH
        echo Please run install.bat first
        pause
        exit /b 1
    )
    set "PY=python"
)

:: Explicit autopilot mode (used by autopilot.bat wrapper)
if /I "%~1"=="--autopilot" (
    shift
    "%PY%" launcher.py --autopilot %*
    exit /b %errorlevel%
)

:: Wrapper-provided default mode (avoids passing synthetic flags through).
if /I "%ARENAMCP_DEFAULT_MODE%"=="standard" (
    "%PY%" launcher.py %*
    exit /b %errorlevel%
)
if /I "%ARENAMCP_DEFAULT_MODE%"=="autopilot" (
    "%PY%" launcher.py --autopilot %*
    exit /b %errorlevel%
)

:: No args: offer a mode picker for convenience.
if "%~1"=="" (
    echo ArenaMCP Launcher
    echo.
    echo   1^) Standard Coach
    echo   2^) Autopilot
    echo   Q^) Quit
    echo.
    set /p MODE=Select mode [1/2/Q]: 
    if /I "%MODE%"=="Q" exit /b 0
    if "%MODE%"=="2" (
        "%PY%" launcher.py --autopilot
        exit /b %errorlevel%
    )
    "%PY%" launcher.py
    exit /b %errorlevel%
)

:: Any args are forwarded directly to launcher.py (standard mode).
"%PY%" launcher.py %*
exit /b %errorlevel%
