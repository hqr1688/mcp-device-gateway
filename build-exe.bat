@echo off
:: ================================================================
::  build-exe.bat
::  Build a standalone mcp-device-gateway.exe via PyInstaller.
::
::  Usage:
::    build-exe.bat [options]
::
::  Options:
::    --python  <exe>    Python executable (default: auto-detect .venv)
::    --onefile          Bundle into a single .exe (default)
::    --onedir           Bundle as exe + _internal directory
::    --clean            Remove build\ and dist\ before building
::
::  Output:
::    One-file (default): dist\mcp-device-gateway.exe
::    One-dir          : dist\mcp-device-gateway\mcp-device-gateway.exe
::
::  Note: --onefile is truly single-file portable; startup is usually
::  ~2-3s slower because it self-extracts to %TEMP% on each launch.
::  Use --onedir if you prioritize startup speed.
:: ================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0"

:: ── defaults ────────────────────────────────────────────────────
set "PYTHON_EXE=python"
set "ONEFILE=1"
set "DO_CLEAN=0"

:: ── parse args ──────────────────────────────────────────────────
:parse_args
if "%~1"=="" goto :done_args
if /i "%~1"=="--python"   ( set "PYTHON_EXE=%~2" & shift & shift & goto :parse_args )
if /i "%~1"=="--onefile"  ( set "ONEFILE=1"      & shift & goto :parse_args )
if /i "%~1"=="--onedir"   ( set "ONEFILE=0"      & shift & goto :parse_args )
if /i "%~1"=="--clean"    ( set "DO_CLEAN=1"     & shift & goto :parse_args )
echo [WARN] Unknown argument: %~1
shift & goto :parse_args
:done_args

:: ── locate Python ───────────────────────────────────────────────
if "!PYTHON_EXE!"=="python" (
    if exist "%~dp0.venv\Scripts\python.exe" (
        set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
    )
)
echo Python: !PYTHON_EXE!

:: ── install PyInstaller if missing ──────────────────────────────
echo.
echo =^> Checking PyInstaller
"!PYTHON_EXE!" -c "import PyInstaller" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   Installing PyInstaller...
    "!PYTHON_EXE!" -m pip install pyinstaller
    "!PYTHON_EXE!" -c "import PyInstaller" >nul 2>&1
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Failed to install PyInstaller
        exit /b 1
    )
)
echo   PyInstaller OK

:: ── optional clean ──────────────────────────────────────────────
if "!DO_CLEAN!"=="1" (
    echo.
    echo =^> Cleaning previous artifacts
    if exist build ( rmdir /s /q build & echo   Removed build\ )
    if exist dist  ( rmdir /s /q dist  & echo   Removed dist\ )
)

:: ── remove stale output dir to avoid PyInstaller PermissionError ─
::  PyInstaller --noconfirm calls os.remove() on the existing output
::  directory, which fails when any file inside is locked. Kill the
::  process first, then delete so PyInstaller starts with a clean slate.
taskkill /f /im "mcp-device-gateway.exe" >nul 2>&1
if exist "dist\mcp-device-gateway" (
    rmdir /s /q "dist\mcp-device-gateway" 2>nul
    :: Retry once in case taskkill needed a moment to release handles
    if exist "dist\mcp-device-gateway" (
        timeout /t 2 /nobreak >nul
        rmdir /s /q "dist\mcp-device-gateway"
        if %ERRORLEVEL% neq 0 (
            echo [ERROR] Cannot remove dist\mcp-device-gateway - a process may still be locking it.
            echo         Close any running mcp-device-gateway.exe and retry.
            exit /b 1
        )
    )
    echo   Removed stale dist\mcp-device-gateway\
)
if exist "dist\mcp-device-gateway.exe" (
    del /f /q "dist\mcp-device-gateway.exe"
    echo   Removed stale dist\mcp-device-gateway.exe
)

:: ── run PyInstaller ─────────────────────────────────────────────
echo.
echo =^> Running PyInstaller

:: Build the argument list progressively so the line stays readable.
set "PI_ARGS=--name mcp-device-gateway"
set "PI_ARGS=!PI_ARGS! --paths src"
set "PI_ARGS=!PI_ARGS! --noconfirm"

:: Collect complete packages that use dynamic imports or C extensions.
set "PI_ARGS=!PI_ARGS! --collect-all mcp_device_gateway"
set "PI_ARGS=!PI_ARGS! --collect-all mcp"
set "PI_ARGS=!PI_ARGS! --collect-all paramiko"
set "PI_ARGS=!PI_ARGS! --collect-all cryptography"
set "PI_ARGS=!PI_ARGS! --collect-all pydantic"
set "PI_ARGS=!PI_ARGS! --collect-all pydantic_core"

:: Single-module hidden imports not found by static analysis.
set "PI_ARGS=!PI_ARGS! --hidden-import mcp_device_gateway.server"
set "PI_ARGS=!PI_ARGS! --hidden-import yaml"
set "PI_ARGS=!PI_ARGS! --hidden-import bcrypt"
set "PI_ARGS=!PI_ARGS! --hidden-import anyio"
set "PI_ARGS=!PI_ARGS! --hidden-import anyio._backends._asyncio"
set "PI_ARGS=!PI_ARGS! --hidden-import anyio._backends._trio"

:: Bundle mode
if "!ONEFILE!"=="1" (
    set "PI_ARGS=!PI_ARGS! --onefile"
) else (
    set "PI_ARGS=!PI_ARGS! --onedir"
)

"!PYTHON_EXE!" -m PyInstaller !PI_ARGS! entrypoint.py
if %ERRORLEVEL% neq 0 (
    echo [ERROR] PyInstaller failed
    exit /b 1
)

:: ── report output ───────────────────────────────────────────────
echo.
echo =^> Build complete
if "!ONEFILE!"=="1" (
    echo   Output: dist\mcp-device-gateway.exe
    echo   This file can run on target machines without a Python installation.
) else (
    echo   Output: dist\mcp-device-gateway\mcp-device-gateway.exe
    echo   IMPORTANT: do not copy only exe; distribute the entire folder including _internal\.
)

:: ── usage hint ──────────────────────────────────────────────────
echo.
echo Usage example:
if "!ONEFILE!"=="1" (
    echo   set MCP_DEVICE_CONFIG=devices.yaml
    echo   dist\mcp-device-gateway.exe
) else (
    echo   set MCP_DEVICE_CONFIG=devices.yaml
    echo   dist\mcp-device-gateway\mcp-device-gateway.exe
)
echo.
echo VS Code mcp.json command field:
if "!ONEFILE!"=="1" (
    echo   "command": "dist\\mcp-device-gateway.exe"
) else (
    echo   "command": "dist\\mcp-device-gateway\\mcp-device-gateway.exe"
)

endlocal
