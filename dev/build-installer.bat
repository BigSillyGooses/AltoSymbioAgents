@echo off
REM dev\build-installer.bat — installer build for the Pinokio-style pipeline.
REM
REM Bootstrap (Miniconda + sidecar venv) now happens on first launch of the
REM INSTALLED app, not at build time — see desktop-shell/bootstrap/. As a
REM result there is no PyInstaller step, no branding\sidecar-bundle\ mirror,
REM and no venv activation here. The installer ships only:
REM
REM   * the Electron shell (electron-builder output)
REM   * the sidecar source tree from backend\  -> resources\sidecar\
REM     (declared in electron-builder.yml's extraResources)
REM
REM Local engines (llama-server / whisper.cpp / piper) are NOT bundled in
REM this release — the follow-up engine-download branch adds a runtime
REM fetch wizard. End users on v1.0.0-test.2 are Claude-API-only.

setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%.."

call npm install
if errorlevel 1 exit /b 1

call npm run build
if errorlevel 1 exit /b 1

call npx electron-builder --win
if errorlevel 1 exit /b 1

echo.
echo Installer in dist\. Test on a clean Windows VM (no Python, no Node).
