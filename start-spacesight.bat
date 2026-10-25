@echo off
chcp 65001 >nul 2>nul
rem ===========================================================================
rem  SpaceSight - console launcher
rem  Double click to start the local service and open the browser.
rem  Closing this window stops the service.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title SPACESIGHT // Storage Cartography

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY="

rem --- 1) portable runtime shipped next to the exe ---------------------------
call :probe "%~dp0runtime\python.exe"
if defined PY goto run

rem --- 2) py launcher / PATH ------------------------------------------------
call :probe "py -3"
if defined PY goto run
call :probe "python"
if defined PY goto run

rem --- 3) per-user python.org installs --------------------------------------
if exist "%LOCALAPPDATA%\Programs\Python" (
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    call :probe "%%~fD\python.exe"
  )
)
if defined PY goto run

rem --- 4) last resort: runtime bundled with the dev harness ------------------
call :probe "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if defined PY goto run

echo.
echo   [X] Python 3 not found (a working 3.9+ interpreter is required).
echo.
echo   Install it from https://www.python.org/downloads/windows/ and make sure
echo   "Add python.exe to PATH" is checked, then double click this file again.
echo.
pause
exit /b 1

:run
echo.
echo   [i] interpreter: %PY%
%PY% -u "%~dp0run.py" %*
if errorlevel 1 (
  echo.
  echo   [!] Service exited with an error. See the message above.
  pause
)
endlocal
exit /b 0

rem ---------------------------------------------------------------------------
rem :probe <command>  -- accept the candidate only if it really runs
rem ---------------------------------------------------------------------------
:probe
if defined PY exit /b 0
%~1 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=%~1"
exit /b 0
