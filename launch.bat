@echo off
setlocal EnableExtensions

title mtgacoach

cd /d "%~dp0"

if not defined MTGACOACH_RUNTIME_ROOT (
    if defined LOCALAPPDATA (
        set "MTGACOACH_RUNTIME_ROOT=%LOCALAPPDATA%\mtgacoach"
    ) else (
        set "MTGACOACH_RUNTIME_ROOT=%~dp0runtime"
    )
)

set "MTGACOACH_APP_ROOT=%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"

set "VENV_PYTHON=%MTGACOACH_RUNTIME_ROOT%\venv\Scripts\python.exe"
set "VENV_PYTHONW=%MTGACOACH_RUNTIME_ROOT%\venv\Scripts\pythonw.exe"

set "MODE=%~1"

if /I "%MODE%"=="--wizard" goto :run_wizard
if /I "%MODE%"=="--setup" goto :run_wizard
if /I "%MODE%"=="--repair" goto :run_wizard
if /I "%MODE%"=="--console" goto :launch_console

if not exist "%VENV_PYTHON%" goto :run_wizard

rem Repair-audit blocker #2: a present-but-dead venv used to be launched
rem hidden and detached — double-click did nothing, forever. Probe the
rem runtime before the silent GUI launch; fall back to the wizard.
"%VENV_PYTHON%" -c "import PySide6" >nul 2>&1
if errorlevel 1 (
    echo The app runtime is broken - running repair...
    goto :run_wizard
)

goto :launch_gui

:run_wizard
call :resolve_system_python
if errorlevel 1 (
    echo.
    echo ERROR: Python 3.10 or newer was not found on PATH.
    echo Install Python from https://www.python.org/downloads/ and re-run.
    echo.
    pause
    exit /b 1
)
"%SYSTEM_PY%" setup_wizard.py --setup-environment
if errorlevel 1 (
    echo.
    echo Setup did not complete successfully.
    pause
    exit /b 1
)
if not exist "%VENV_PYTHON%" (
    echo.
    echo Setup finished but the venv was not created at:
    echo     %VENV_PYTHON%
    pause
    exit /b 1
)
if /I "%MODE%"=="--wizard" exit /b 0
if /I "%MODE%"=="--setup" exit /b 0
if /I "%MODE%"=="--repair" exit /b 0
goto :launch_gui

:launch_gui
if exist "%VENV_PYTHONW%" (
    start "" "%VENV_PYTHONW%" scripts\launch_installed.py
    exit /b 0
)
"%VENV_PYTHON%" scripts\launch_installed.py
exit /b %errorlevel%

:launch_console
"%VENV_PYTHON%" scripts\launch_installed.py
exit /b %errorlevel%

:resolve_system_python
set "SYSTEM_PY="
for /f "usebackq delims=" %%P in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do set "SYSTEM_PY=%%P"
if defined SYSTEM_PY exit /b 0
for /f "usebackq delims=" %%P in (`where python 2^>nul`) do if not defined SYSTEM_PY set "SYSTEM_PY=%%P"
if defined SYSTEM_PY exit /b 0
exit /b 1
