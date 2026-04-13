#define MyAppName "mtgacoach"
#define MyAppPublisher "Josh Armour"
#define MyAppURL "https://github.com/josharmour/mtgacoach"

#ifndef AppVersion
  #define AppVersion "2.0.1"
#endif

[Setup]
AppId={{9A97A86B-1A9D-4577-AB21-3F6C1F64B3AB}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\mtgacoach
DefaultGroupName=mtgacoach
DisableProgramGroupPage=yes
AllowNoIcons=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=mtgacoach-Setup
SetupIconFile=..\mtga_coach.ico
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
UninstallDisplayIcon={app}\mtga_coach.ico
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional icons:"

[Dirs]
Name: "{localappdata}\mtgacoach"

[InstallDelete]
Type: filesandordirs; Name: "{app}\launcher"

[Files]
; PySide desktop app release payload staged by scripts/build_release.ps1
Source: "..\dist\desktop-release\app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\mtgacoach"; Filename: "{app}\runtime\Scripts\pythonw.exe"; Parameters: """{app}\scripts\launch_installed.py"""; WorkingDir: "{app}"; IconFilename: "{app}\mtga_coach.ico"
Name: "{autodesktop}\mtgacoach"; Filename: "{app}\runtime\Scripts\pythonw.exe"; Parameters: """{app}\scripts\launch_installed.py"""; WorkingDir: "{app}"; IconFilename: "{app}\mtga_coach.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\Scripts\pythonw.exe"; Parameters: """{app}\scripts\launch_installed.py"""; Description: "Launch mtgacoach"; Flags: nowait postinstall skipifsilent
