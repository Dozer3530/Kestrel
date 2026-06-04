@echo off
rem Build Kestrel.exe (single windowed executable) with PyInstaller.
rem Requires: py -m pip install pyinstaller pyinstaller-hooks-contrib
rem Output:   dist\Kestrel.exe
setlocal
cd /d "%~dp0"
echo Building Kestrel.exe ...
py -m PyInstaller --noconfirm --clean Kestrel.spec
echo.
echo Done. Executable is at: dist\Kestrel.exe
endlocal
