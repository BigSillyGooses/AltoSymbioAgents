@echo off
REM build-sidecar.bat - standalone PyInstaller invocation.
REM
REM Used by `npm run build:sidecar` and CI when the parent project doesn't want
REM to run the full electron-builder pipeline (dev\build-installer.bat).
REM Outputs backend\dist\server\.
REM
REM Mirrors build-sidecar.sh: create the venv on the fly if it doesn't already
REM exist and install backend deps + the pinned PyInstaller from
REM requirements-build.txt. Lets the windows-smoke CI workflow run this script
REM without depending on dev\install.ps1 having been invoked first.

setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%..\backend"

if not exist ".venv\Scripts\python.exe" (
    echo [build-sidecar] backend\.venv missing - creating it now
    python -m venv .venv
    if errorlevel 1 (
        echo [error] python -m venv failed
        exit /b 1
    )
    call .venv\Scripts\activate.bat
    if errorlevel 1 (
        echo [error] could not activate freshly-created venv
        exit /b 1
    )
    python -m pip install --timeout=1000 --retries=20 --no-cache-dir --upgrade pip wheel setuptools
    if errorlevel 1 ( echo [error] pip upgrade failed & exit /b 1 )
    python -m pip install --timeout=1000 --retries=20 --no-cache-dir -r requirements.txt
    if errorlevel 1 ( echo [error] pip install -r requirements.txt failed & exit /b 1 )
    python -m pip install --timeout=1000 --retries=20 --no-cache-dir -r requirements-build.txt
    if errorlevel 1 ( echo [error] pip install -r requirements-build.txt failed & exit /b 1 )
) else (
    call .venv\Scripts\activate.bat
    if errorlevel 1 (
        echo [error] could not activate venv
        exit /b 1
    )
)

python -m PyInstaller pyinstaller.spec --noconfirm --clean
set "ERR=%ERRORLEVEL%"

call .venv\Scripts\deactivate.bat 2>nul
exit /b %ERR%
