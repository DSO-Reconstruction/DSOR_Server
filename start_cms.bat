@echo off
setlocal
pushd "%~dp0" || exit /b 1

set "DSO_PY="
py -3-64 -c "import struct,sys; sys.exit(0 if sys.version_info >= (3,12) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 set "DSO_PY=py -3-64"
if not defined DSO_PY (
    python -c "import struct,sys; sys.exit(0 if sys.version_info >= (3,12) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
    if not errorlevel 1 set "DSO_PY=python"
)
if not defined DSO_PY (
    echo ERROR: 64-bit Python 3.12 or newer was not found.
    echo Install it or configure the py launcher or PATH.
    popd
    exit /b 1
)

echo Starting the CMS only. Game and account services will stay stopped.
%DSO_PY% -u run_cms.py
set "DSO_EXIT=%errorlevel%"
popd
exit /b %DSO_EXIT%
