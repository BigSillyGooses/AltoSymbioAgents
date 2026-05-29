@echo off
REM dev\dev.bat — single developer entry point for the Pinokio-style pipeline.
REM
REM The end-user install flow downloads Miniconda + creates the sidecar venv
REM on first launch (see desktop-shell/bootstrap/), so developers only need
REM Node to work on the renderer + main process. Python is not required to
REM run `npm run dev` — the bootstrap install happens inside the running
REM Electron app the first time you launch it.
REM
REM If you want to run the backend's pytest suite directly, set up a
REM Python 3.12 venv at backend/.venv yourself and `pip install -r backend/
REM requirements.txt` — the bootstrap path doesn't touch backend/.venv.

setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%.."

call npm install
if errorlevel 1 exit /b 1

call npm run dev
