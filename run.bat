@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Start this standalone DSO server directory with a 64-bit Python 3.12+.
rem Usage: run.bat [--log] [server arguments]
rem Ctrl+C stops the server cleanly.

rem Resolve the launcher and log paths from this directory, wherever invoked.
pushd "%~dp0" || exit /b 1

set "DSO_LOG_TO_FILE="
set "DSO_ARGS="

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--log" (
    set "DSO_LOG_TO_FILE=1"
    shift
    goto parse_args
)
if /i "%~1"=="--help" goto show_help
if /i "%~1"=="-h" goto show_help
if /i "%~1"=="/?" goto show_help
set "DSO_ARGS=!DSO_ARGS! %1"
shift
goto parse_args
:args_done

rem Prefer explicit 64-bit launcher versions before trying PATH's python.
set "DSO_PY="
for %%I in ("-3.12-64" "-3-64") do (
    if not defined DSO_PY (
        py %%~I -c "import struct,sys; sys.exit(0 if struct.calcsize('P')*8>=64 and sys.version_info>=(3,12) else 1)" >nul 2>&1
        if !errorlevel! equ 0 set "DSO_PY=py %%~I"
    )
)
if not defined DSO_PY (
    python -c "import struct,sys; sys.exit(0 if struct.calcsize('P')*8>=64 and sys.version_info>=(3,12) else 1)" >nul 2>&1
    if !errorlevel! equ 0 set "DSO_PY=python"
)
if not defined DSO_PY (
    echo(
    echo [run.bat] ERROR: no 64-bit Python 3.12 or newer interpreter was found.
    echo(
    echo Install Python 3.12 64-bit or newer, or make one of these resolve:
    echo   py -3.12-64
    echo   py -3-64
    echo   python        ^(must be 64-bit Python 3.12 or newer^)
    echo(
    popd
    exit /b 1
)

if defined DSO_LOG_TO_FILE (
    if not exist "data\logs" mkdir "data\logs"
    rem Use Python for a timestamp independent of the Windows locale.
    for /f %%T in ('%DSO_PY% -c "import time;print(time.strftime('%%Y%%m%%d_%%H%%M%%S'))"') do set "DSO_STAMP=%%T"
    set "DSO_OUT=data\logs\server_!DSO_STAMP!.stdout.log"
    set "DSO_ERR=data\logs\server_!DSO_STAMP!.stderr.log"
    echo [run.bat] %DSO_PY% run.py
    echo [run.bat] stdout -^> !DSO_OUT!
    echo [run.bat] stderr -^> !DSO_ERR!
    echo(
    rem Unbuffered output keeps captured logs current.
    %DSO_PY% -u run.py!DSO_ARGS! > "!DSO_OUT!" 2> "!DSO_ERR!"
) else (
    echo [run.bat] %DSO_PY% run.py
    echo(
    %DSO_PY% run.py!DSO_ARGS!
)

set "DSO_EXIT=%errorlevel%"
popd
exit /b %DSO_EXIT%

:show_help
echo(
echo Start the reconstructed DSO server.
echo(
echo   run.bat            run in the foreground, output to this console
echo   run.bat --log      capture stdout/stderr to data\logs\server_*.log
echo   run.bat --help     show this help
echo(
echo Any other arguments are passed through to python run.py.
echo Requires 64-bit Python 3.12 or newer.
echo(
echo Listens on 127.0.0.1 by default:
echo   80    authentication gateway
echo   2190  login and armed map handoff
echo   2191  built-in chat
echo   2192  character selection and gameplay map
echo(
echo Configuration is environment driven; see README.md.
echo Common overrides:
echo   set DSO_HOST=127.0.0.1        ^(0.0.0.0 to accept remote players^)
echo   set DSO_PUBLIC_HOST=203.0.113.10   ^(address remote players are redirected to^)
echo(
echo Ctrl+C stops the server cleanly.
echo(
popd
exit /b 0
