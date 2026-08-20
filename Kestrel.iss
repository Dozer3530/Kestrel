; Inno Setup script for Kestrel -> dist\KestrelSetup.exe
; Build first with build.bat (PyInstaller), then run build_installer.bat.
;
; Per-user install (no admin needed) so it works on locked-down machines.

#define AppName "Kestrel"
#define AppVersion "0.8.0"
#define AppPublisher "Dozer3530"
#define AppExe "Kestrel.exe"
#define AppURL "https://github.com/Dozer3530/Kestrel"

[Setup]
AppId={{7C9E6A4B-2F1D-4B8E-9A3C-5D6E7F801234}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=dist
OutputBaseFilename=KestrelSetup
SetupIconFile=assets\icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest

[Files]
Source: "dist\Kestrel\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--register"; Flags: runhidden
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--unregister"; Flags: runhidden
