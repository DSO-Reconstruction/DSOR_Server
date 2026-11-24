@echo off
setlocal
cd /d "%~dp0"

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
if errorlevel 1 (
    echo Python 3.12 or newer is required. Install Python with Tcl/Tk support and try again.
    pause
    exit /b 1
)

echo Installing launcher build requirements...
py -3 -m pip install -r requirements-launcher-build.txt
if errorlevel 1 (
    echo Could not install the build requirements.
    pause
    exit /b 1
)

echo Building the standalone launcher...
py -3 -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name DSORLauncher ^
    --distpath "%CD%" ^
    --workpath "%CD%\build\pyinstaller" ^
    --specpath "%CD%\build" ^
    --hidden-import setup_client_assets ^
    --hidden-import tools.unpack_dso_bundles ^
    launcher_gui.py
if errorlevel 1 (
    echo Launcher build failed.
    pause
    exit /b 1
)

echo.
echo Build complete: %CD%\DSORLauncher.exe
pause
