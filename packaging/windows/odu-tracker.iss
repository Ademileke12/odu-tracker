; ODU Tracker — Inno Setup script (Windows installer)
; Build: ISCC.exe packaging\windows\odu-tracker.iss  (or build_windows.bat)

#define MyAppName "ODU Tracker"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "anakincoco"
#define MyAppURL "mailto:srimpkoyonko@gmail.com"
#define MyAppExeName "ODU-Tracker.exe"

[Setup]
AppId={{8E17C1A9-52D4-4B6B-9B0F-C552E0A12f88}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\ODU Tracker
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist\installer
OutputBaseFilename=ODU-Tracker-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\ODU-Tracker.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Start ODU Tracker when Windows starts"; Flags: checkedonce

[Files]
Source: "..\..\dist\ODU-Tracker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\ODU-Tracker.exe"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\ODU-Tracker.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ODU Tracker"; ValueData: """{app}\ODU-Tracker.exe"" --port 8770"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\ODU-Tracker.exe"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\*"
