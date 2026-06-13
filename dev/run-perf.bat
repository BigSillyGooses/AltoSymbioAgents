@echo off
REM dev\run-perf.bat - local performance bench runner.
REM
REM Activates the project venv and runs the deterministic perf scenarios
REM (benchmarks\perf\run_perf.py) against the real backend services with
REM fake model clients. No API key and no network access are required.
REM The workflow that runs in CI lives at .github\workflows\perf-bench.yml -
REM this script mirrors its steps so contributors can reproduce the
REM published numbers locally.
REM
REM Pre-reqs:
REM   * dev\install.ps1 has run successfully (creates backend\.venv).

setlocal enableextensions enabledelayedexpansion

set "REPO_ROOT=%~dp0.."
pushd "%REPO_ROOT%"

set "VENV_PY=%REPO_ROOT%\backend\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [error] backend\.venv\Scripts\python.exe not found.
    echo         Run dev\install.ps1 first to create the venv.
    popd
    exit /b 1
)

if not exist "%REPO_ROOT%\benchmarks" mkdir "%REPO_ROOT%\benchmarks"

echo ==^> Running perf scenarios
"%VENV_PY%" benchmarks\perf\run_perf.py --scenario all --output benchmarks\perf_results.json
if errorlevel 1 (
    echo [error] perf bench failed or a threshold gate was breached.
    echo         See benchmarks\perf_thresholds.json for the gates and
    echo         benchmarks\perf_results.json for the measured numbers.
    popd
    exit /b 1
)

echo [ok] Perf bench complete. See benchmarks\perf_results.json.
popd
endlocal
