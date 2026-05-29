@echo off
REM START_HERE.bat — double-click to launch the app in dev mode.
REM
REM This installs Node dependencies (first time only, ~2 min) and then
REM starts the Electron shell + Python sidecar. The first launch inside
REM the app also downloads Miniconda (~600 MB, one-time).
REM
REM End users: this script is for running from source. If you just want
REM to use the app, download the installer from the GitHub Releases
REM page instead — see START_HERE.md.

setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

echo.
echo ============================================================
echo  altosybioagents — launching in dev mode
echo ============================================================
echo.

where node >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Node.js is not installed.
    echo.
    echo Install Node 20 or newer from https://nodejs.org/ and then
    echo double-click this file again.
    echo.
    pause
    exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
    echo [ERROR] npm is not on your PATH.
    echo.
    echo Reinstall Node.js from https://nodejs.org/ and make sure the
    echo "Add to PATH" checkbox stays selected, then try again.
    echo.
    pause
    exit /b 1
)

echo Step 1/2: installing dependencies (skips if already installed)...
echo.
call npm install
if errorlevel 1 (
    echo.
    echo [ERROR] npm install failed. See the messages above.
    pause
    exit /b 1
)

echo.
echo Step 2/2: starting the app...
echo.
echo An Electron window should open in a few seconds. The first launch
echo inside the app downloads Miniconda (~600 MB, one-time, ~5 min).
echo Watch the status bar at the bottom of the window for progress.
echo.
echo Closing this terminal will close the app.
echo.

call npm run dev
