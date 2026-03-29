@echo off
:: ================================================================
::  start-mcp-device-gateway.bat
::  Start the MCP Device Gateway server.
::
::  Usage:
::    start-mcp-device-gateway.bat [options]
::
::  Options:
::    --config    <path>         Device config YAML (default: devices.example.yaml)
::    --audit     <path>         Audit log path   (default: mcp_audit.log)
::    --transport <stdio|sse>    MCP transport     (default: stdio)
::    --python    <exe>          Python executable (default: auto-detect .venv)
:: ================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0"

:: ── defaults ────────────────────────────────────────────────────
set "PYTHON_EXE=python"
set "CONFIG_PATH="
set "AUDIT_LOG="
set "TRANSPORT=stdio"

:: ── parse args ──────────────────────────────────────────────────
:parse_args
if "%~1"=="" goto :done_args
if /i "%~1"=="--config"    ( set "CONFIG_PATH=%~2"  & shift & shift & goto :parse_args )
if /i "%~1"=="--audit"     ( set "AUDIT_LOG=%~2"    & shift & shift & goto :parse_args )
if /i "%~1"=="--transport" ( set "TRANSPORT=%~2"    & shift & shift & goto :parse_args )
if /i "%~1"=="--python"    ( set "PYTHON_EXE=%~2"   & shift & shift & goto :parse_args )
echo [WARN] Unknown argument: %~1
shift & goto :parse_args
:done_args

:: ── locate Python ───────────────────────────────────────────────
if "!PYTHON_EXE!"=="python" (
    if exist "%~dp0.venv\Scripts\python.exe" (
        set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
    )
)

:: ── default config / audit paths ────────────────────────────────
if "!CONFIG_PATH!"=="" set "CONFIG_PATH=%~dp0devices.example.yaml"
if "!AUDIT_LOG!"==""   set "AUDIT_LOG=%~dp0mcp_audit.log"

:: ── set environment variables ───────────────────────────────────
set "MCP_DEVICE_CONFIG=!CONFIG_PATH!"
set "MCP_AUDIT_LOG=!AUDIT_LOG!"
set "MCP_TRANSPORT=!TRANSPORT!"

echo MCP_DEVICE_CONFIG=!MCP_DEVICE_CONFIG!
echo MCP_AUDIT_LOG=!MCP_AUDIT_LOG!
echo MCP_TRANSPORT=!MCP_TRANSPORT!
echo PYTHON_EXE=!PYTHON_EXE!

:: ── PYTHONPATH (src layout support) ─────────────────────────────
if exist "%~dp0src" (
    if defined PYTHONPATH (
        set "PYTHONPATH=%~dp0src;!PYTHONPATH!"
    ) else (
        set "PYTHONPATH=%~dp0src"
    )
    echo PYTHONPATH=!PYTHONPATH!
)

:: ── dependency check ────────────────────────────────────────────
"!PYTHON_EXE!" -c "import importlib.util,sys;mods=('mcp','paramiko','yaml');missing=[m for m in mods if importlib.util.find_spec(m) is None];print(','.join(missing));sys.exit(1 if missing else 0)"
if %ERRORLEVEL% neq 0 (
    echo [WARN] Missing dependencies detected. Running: pip install -e .
    "!PYTHON_EXE!" -m pip install -e .
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Dependency installation failed.
        echo         Run "!PYTHON_EXE! -m pip install -e ." and retry.
        exit /b 1
    )
)

:: ── launch server ───────────────────────────────────────────────
"!PYTHON_EXE!" -m mcp_device_gateway.server
