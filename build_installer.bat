@echo off
rem Build dist\KestrelSetup.exe from the PyInstaller output (run build.bat first).
rem Requires Inno Setup 6:  winget install -e --id JRSoftware.InnoSetup
setlocal
cd /d "%~dp0"
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
echo Building KestrelSetup.exe ...
"%ISCC%" Kestrel.iss
echo.
echo Done. Installer at: dist\KestrelSetup.exe
endlocal
