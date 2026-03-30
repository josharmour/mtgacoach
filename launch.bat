@echo off
setlocal

title mtgacoach

cd /d "%~dp0"

if not defined MTGACOACH_RUNTIME_ROOT (
    if defined LOCALAPPDATA (
        set "MTGACOACH_RUNTIME_ROOT=%LOCALAPPDATA%\mtgacoach"
    ) else (
        set "MTGACOACH_RUNTIME_ROOT=%~dp0runtime"
    )
)

set "PYTHONPATH=%~dp0src;%PYTHONPATH%"

call :resolve_console_python
if errorlevel 1 exit /b 1

call :resolve_gui_python
if errorlevel 1 exit /b 1

if /I "%~1"=="--coach" (
    shift
    "%PY_CONSOLE%" launcher.py %*
    exit /b %errorlevel%
)

if /I "%~1"=="--autopilot" (
    shift
    "%PY_CONSOLE%" launcher.py --autopilot %*
    exit /b %errorlevel%
)

if /I "%~1"=="--setup" (
    shift
    "%PY_GUI%" launcher_gui.py --setup %*
    exit /b %errorlevel%
)

if /I "%~1"=="--repair" (
    shift
    "%PY_GUI%" launcher_gui.py --setup %*
    exit /b %errorlevel%
)

if /I "%~1"=="--wizard" (
    shift
    "%PY_CONSOLE%" setup_wizard.py %*
    exit /b %errorlevel%
)

if /I "%~1"=="--gui" (
    shift
    "%PY_GUI%" launcher_gui.py %*
    exit /b %errorlevel%
)

if "%~1"=="" (
    "%PY_GUI%" launcher_gui.py
    exit /b %errorlevel%
)

:: Unrecognized arguments are treated as pass-through flags for the TUI runtime.
"%PY_CONSOLE%" launcher.py %*
exit /b %errorlevel%

:resolve_console_python
if exist "%MTGACOACH_RUNTIME_ROOT%\venv\Scripts\python.exe" (
    set "PY_CONSOLE=%MTGACOACH_RUNTIME_ROOT%\venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PY_CONSOLE=venv\Scripts\python.exe"
) else (
    python --version >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python not found in PATH
        echo Please run install.bat or the launcher setup flow first
        pause
        exit /b 1
    )
    set "PY_CONSOLE=python"
)
exit /b 0

:resolve_gui_python
if exist "%MTGACOACH_RUNTIME_ROOT%\venv\Scripts\pythonw.exe" (
    set "PY_GUI=%MTGACOACH_RUNTIME_ROOT%\venv\Scripts\pythonw.exe"
) else if exist "venv\Scripts\pythonw.exe" (
    set "PY_GUI=venv\Scripts\pythonw.exe"
) else (
    pythonw --version >nul 2>&1
    if not errorlevel 1 (
        set "PY_GUI=pythonw"
    ) else (
        set "PY_GUI=%PY_CONSOLE%"
    )
)
exit /b 0
