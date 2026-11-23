@echo off
rem ===================================================================
rem  Build leakcheck.exe with PyInstaller.
rem  Usage: double-click, or run from cmd:  打包.bat
rem  Requires:  pip install pyinstaller
rem  Output:    dist\leakcheck\leakcheck.exe  (onedir, recommended)
rem             dist\leakcheck.exe           (onefile, slower start)
rem ===================================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found.
    pause
    exit /b 1
)

python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller not installed. Run:
    echo         pip install pyinstaller
    pause
    exit /b 1
)

echo == 1/2 build onedir ==
python -m PyInstaller --noconfirm --clean --onedir --name leakcheck ^
    --paths . --distpath dist --workpath build_exe --specpath build_exe launcher.py
if errorlevel 1 goto failed

echo == 2/2 copy data files ==
copy /y "..\关键词.txt" "dist\leakcheck\" >nul 2>&1
copy /y "..\我的规则.json" "dist\leakcheck\" >nul 2>&1
if exist "..\使用说明.txt" copy /y "..\使用说明.txt" "dist\leakcheck\" >nul 2>&1

echo.
echo Done:  dist\leakcheck\leakcheck.exe
echo.
pause
exit /b 0

:failed
echo.
echo [ERROR] build failed, see the log above.
pause
exit /b 1
