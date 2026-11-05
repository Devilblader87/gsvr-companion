; Inno Setup script for the GSVR companion (Windows). Per-user install, no admin rights:
;   %LOCALAPPDATA%\Programs\GSVR  (the self-updater can swap files there without elevation)
; Start menu entry, optional desktop icon, optional autostart, uninstaller. Built by build_windows.bat:
;   ISCC /DAppVersion=<version> /DDistDir=<dist\GSVR> /DOutDir=<release> companion\packaging\gsvr_setup.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef DistDir
  #define DistDir "..\build\dist\GSVR"
#endif
#ifndef OutDir
  #define OutDir "..\build\release"
#endif

[Setup]
AppId={{6C0E5D4B-3F1A-4B8E-9A62-5E7C2D9B1A47}
AppName=GSVR Companion
AppVersion={#AppVersion}
AppVerName=GSVR Companion {#AppVersion}
AppPublisher=GSVR (fan project)
AppPublisherURL=https://devilblader87.github.io/gsvr-companion/
AppSupportURL=https://devilblader87.github.io/gsvr-companion/
DefaultDirName={localappdata}\Programs\GSVR
DisableDirPage=yes
DisableProgramGroupPage=yes
DefaultGroupName=GSVR Companion
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=GSVR-Setup
SetupIconFile=gsvr.ico
UninstallDisplayIcon={app}\GSVR.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "de"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "autostart"; Description: "Start GSVR Companion with Windows (recommended: it is needed while you play)"; Flags: checkedonce
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\GSVR Companion"; Filename: "{app}\GSVR.exe"
Name: "{userdesktop}\GSVR Companion"; Filename: "{app}\GSVR.exe"; Tasks: desktopicon
Name: "{userstartup}\GSVR Companion"; Filename: "{app}\GSVR.exe"; Parameters: "--minimized"; Tasks: autostart

[Run]
Filename: "{app}\GSVR.exe"; Description: "{cm:LaunchProgram,GSVR Companion}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; self-update leftovers (*.gsvr-old) and the staging folder
Type: filesandordirs; Name: "{app}\.gsvr-update"
Type: files; Name: "{app}\*.gsvr-old"
Type: files; Name: "{app}\_internal\*.gsvr-old"
