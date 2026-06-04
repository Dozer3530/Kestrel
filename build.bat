@echo off
rem Build the Kestrel one-folder app with PyInstaller.
rem Requires: py -m pip install pyinstaller pyinstaller-hooks-contrib
rem Output:   dist\Kestrel\Kestrel.exe  (zip the dist\Kestrel folder to distribute)
setlocal
cd /d "%~dp0"
echo Building Kestrel.exe ...
py -m PyInstaller --noconfirm --clean Kestrel.spec
echo.
echo Done. Executable is at: dist\Kestrel.exe
endlocal
