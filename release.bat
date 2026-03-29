@echo off
:: ================================================================
::  release.bat
::  Build and publish mcp-device-gateway.
::
::  Usage:
::    release.bat [options]
::
::  Options:
::    --version   <X.Y.Z>           Explicit target version (skips auto-bump)
::    --bump      patch|minor|major  Auto-increment type (default: patch)
::    --python    <exe>              Python executable (default: auto-detect .venv)
::    --publish                      Upload to PyPI after build
::    --test-pypi                    Upload to TestPyPI after build
::    --skip-tag                     Do not create a Git tag
::    --skip-build                   Skip clean+build steps (use existing dist\)
::
::  Examples:
::    release.bat                          Build only, patch bump
::    release.bat --bump minor             Build only, minor bump
::    release.bat --version 1.0.0          Build only, explicit version
::    release.bat --publish                Build and publish to PyPI
::    release.bat --test-pypi --skip-tag   Publish to TestPyPI, no tag
:: ================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0"

:: ── defaults ────────────────────────────────────────────────────
set "PYTHON_EXE=python"
set "BUMP=patch"
set "ARG_VERSION="
set "DO_PUBLISH=0"
set "DO_TESTPYPI=0"
set "SKIP_TAG=0"
set "SKIP_BUILD=0"

:: ── parse args ──────────────────────────────────────────────────
:parse_args
if "%~1"=="" goto :done_args
if /i "%~1"=="--version"    ( set "ARG_VERSION=%~2"  & shift & shift & goto :parse_args )
if /i "%~1"=="--bump"       ( set "BUMP=%~2"         & shift & shift & goto :parse_args )
if /i "%~1"=="--python"     ( set "PYTHON_EXE=%~2"   & shift & shift & goto :parse_args )
if /i "%~1"=="--publish"    ( set "DO_PUBLISH=1"     & shift & goto :parse_args )
if /i "%~1"=="--test-pypi"  ( set "DO_TESTPYPI=1"   & shift & goto :parse_args )
if /i "%~1"=="--skip-tag"   ( set "SKIP_TAG=1"       & shift & goto :parse_args )
if /i "%~1"=="--skip-build" ( set "SKIP_BUILD=1"     & shift & goto :parse_args )
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

:: ── check build tools ───────────────────────────────────────────
echo.
echo =^> Checking build tools
"!PYTHON_EXE!" -c "import build" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   Installing build...
    "!PYTHON_EXE!" -m pip install --quiet build
    if %ERRORLEVEL% neq 0 ( echo [ERROR] Failed to install build & exit /b 1 )
)
echo   build OK

if "!DO_PUBLISH!"=="1" goto :check_twine
if "!DO_TESTPYPI!"=="1" goto :check_twine
goto :after_twine

:check_twine
"!PYTHON_EXE!" -c "import twine" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   Installing twine...
    "!PYTHON_EXE!" -m pip install --quiet twine
    if %ERRORLEVEL% neq 0 ( echo [ERROR] Failed to install twine & exit /b 1 )
)
echo   twine OK

:after_twine

:: ── read current version from pyproject.toml ────────────────────
::  The version line is exactly: version = "X.Y.Z"
::  ~11 strips the leading 11 chars: v,e,r,s,i,o,n,' ','=',' ','"'
::  ~0,-1 strips the trailing '"'
echo.
echo =^> Reading version from pyproject.toml

set "VLINE="
for /f "tokens=*" %%L in ('findstr /b "version = " pyproject.toml') do set "VLINE=%%L"
if "!VLINE!"=="" (
    echo [ERROR] Cannot find version line in pyproject.toml
    exit /b 1
)
set "_V=!VLINE:~11!"
set "CURRENT_VERSION=!_V:~0,-1!"
echo   Current version: !CURRENT_VERSION!

:: ── compute new version ─────────────────────────────────────────
if not "!ARG_VERSION!"=="" (
    set "NEW_VERSION=!ARG_VERSION!"
    goto :version_ready
)

for /f "tokens=1,2,3 delims=." %%a in ("!CURRENT_VERSION!") do (
    set /a VER_MAJOR=%%a
    set /a VER_MINOR=%%b
    set /a VER_PATCH=%%c
)

if /i "!BUMP!"=="major" (
    set /a VER_MAJOR+=1
    set "VER_MINOR=0"
    set "VER_PATCH=0"
)
if /i "!BUMP!"=="minor" (
    set /a VER_MINOR+=1
    set "VER_PATCH=0"
)
if /i "!BUMP!"=="patch" (
    set /a VER_PATCH+=1
)
set "NEW_VERSION=!VER_MAJOR!.!VER_MINOR!.!VER_PATCH!"

:version_ready
echo   New version:     !NEW_VERSION!

:: ── update pyproject.toml ───────────────────────────────────────
::  Use Python inline: \x3d='='  \x22='"'
::  Non-raw string so hex escapes are evaluated by Python.
if not "!NEW_VERSION!"=="!CURRENT_VERSION!" (
    "!PYTHON_EXE!" -c "import re;c=open('pyproject.toml',encoding='utf-8').read();c=re.sub('version \x3d \x22[0-9.]+\x22','version \x3d \x22!NEW_VERSION!\x22',c,count=1);open('pyproject.toml','w',encoding='utf-8').write(c)"
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Failed to update pyproject.toml
        exit /b 1
    )
    echo   pyproject.toml updated to !NEW_VERSION!
) else (
    echo   Version unchanged, skipping pyproject.toml update
)

set "RELEASE_TAG=v!NEW_VERSION!"

:: ── clean old build artifacts ───────────────────────────────────
if "!SKIP_BUILD!"=="0" (
    echo.
    echo =^> Cleaning old build artifacts
    if exist dist  ( rmdir /s /q dist  & echo   Removed dist\ )
    if exist build ( rmdir /s /q build & echo   Removed build\ )
    for /d %%D in (src\*.egg-info) do (
        rmdir /s /q "%%D"
        echo   Removed %%D
    )
)

:: ── build wheel and sdist ───────────────────────────────────────
if "!SKIP_BUILD!"=="0" (
    echo.
    echo =^> Building wheel and sdist
    "!PYTHON_EXE!" -m build --outdir dist
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Build failed
        exit /b 1
    )
    echo   Build artifacts:
    for %%f in (dist\*) do echo     %%f
)

:: ── verify artifacts ────────────────────────────────────────────
echo.
echo =^> Verifying artifacts
if not exist dist (
    echo [ERROR] dist\ not found. Run without --skip-build first.
    exit /b 1
)

set "FOUND_WHL=0"
set "FOUND_TGZ=0"
for %%f in (dist\*.whl)   do set "FOUND_WHL=1"
for %%f in (dist\*.tar.gz) do set "FOUND_TGZ=1"
if "!FOUND_WHL!"=="0" ( echo [ERROR] No .whl file in dist\ & exit /b 1 )
if "!FOUND_TGZ!"=="0" ( echo [ERROR] No .tar.gz file in dist\ & exit /b 1 )

if "!DO_PUBLISH!"=="1" (
    "!PYTHON_EXE!" -m twine check dist\*
    if %ERRORLEVEL% neq 0 ( echo [WARN] twine check reported issues )
)
if "!DO_TESTPYPI!"=="1" (
    "!PYTHON_EXE!" -m twine check dist\*
    if %ERRORLEVEL% neq 0 ( echo [WARN] twine check reported issues )
)
echo   Artifacts OK

:: ── create Git tag ──────────────────────────────────────────────
if "!SKIP_TAG!"=="0" (
    echo.
    echo =^> Creating Git tag !RELEASE_TAG!

    set "TAG_EXISTS="
    for /f "tokens=*" %%t in ('git tag -l "!RELEASE_TAG!" 2^>nul') do set "TAG_EXISTS=%%t"

    if "!TAG_EXISTS!"=="!RELEASE_TAG!" (
        echo   Tag !RELEASE_TAG! already exists, skipping
    ) else (
        :: Check whether pyproject.toml has uncommitted changes
        set "GIT_DIRTY="
        git status --porcelain pyproject.toml > "%TEMP%\mcp_gst.tmp" 2>nul
        for /f %%s in ('type "%TEMP%\mcp_gst.tmp" 2^>nul') do set "GIT_DIRTY=1"
        del "%TEMP%\mcp_gst.tmp" >nul 2>&1

        if "!GIT_DIRTY!"=="1" (
            echo   Committing pyproject.toml version bump...
            git add pyproject.toml
            git commit -m "chore: release version !NEW_VERSION!"
            if %ERRORLEVEL% neq 0 (
                echo [ERROR] Git commit failed
                exit /b 1
            )
        )

        git tag -a "!RELEASE_TAG!" -m "Release !NEW_VERSION!"
        if %ERRORLEVEL% neq 0 (
            echo [ERROR] Failed to create Git tag
            exit /b 1
        )
        echo   Tag !RELEASE_TAG! created
        echo   To push: git push origin !RELEASE_TAG!
    )
)

:: ── publish ─────────────────────────────────────────────────────
if "!DO_TESTPYPI!"=="1" (
    echo.
    echo =^> Publishing to TestPyPI
    "!PYTHON_EXE!" -m twine upload --repository testpypi dist\*
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] TestPyPI upload failed
        exit /b 1
    )
    echo   Published to TestPyPI
    echo   Verify: pip install --index-url https://test.pypi.org/simple/ mcp-device-gateway==!NEW_VERSION!
    goto :done
)

if "!DO_PUBLISH!"=="1" (
    echo.
    echo =^> Publishing to PyPI
    "!PYTHON_EXE!" -m twine upload dist\*
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] PyPI upload failed
        exit /b 1
    )
    echo   Published to PyPI
    echo   Verify: pip install mcp-device-gateway==!NEW_VERSION!
)

:done
echo.
echo Release complete: !NEW_VERSION!
endlocal
