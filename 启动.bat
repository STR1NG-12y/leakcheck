@echo off
rem ===================================================================
rem  leakcheck launcher
rem  ASCII only on purpose: cmd parses .bat files byte by byte, and
rem  non-ASCII text here can split a command across read boundaries.
rem  The Chinese UI lives in Python (leakcheck/menu.py).
rem ===================================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto no_python

python -m leakcheck.menu %*
if errorlevel 1 pause
exit /b 0

:no_python
color 0C
echo.
echo    [ERROR] Python not found.
echo.
echo    Please install Python 3.9 or newer and check
echo    "Add Python to PATH" during installation.
echo.
pause
exit /b 1
