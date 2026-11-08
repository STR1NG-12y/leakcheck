@echo off
rem ===================================================================
rem  Same launcher as the project root, kept here so this folder works
rem  on its own. ASCII only - see the note in ../launch explanation.
rem ===================================================================
set "ROOT=%~dp0.."
cd /d "%ROOT%"

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
echo    Please install Python 3.9 or newer and add it to PATH.
echo.
pause
exit /b 1
