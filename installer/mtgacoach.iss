#define MyAppName "mtgacoach"
#define MyAppPublisher "Josh Armour"
#define MyAppURL "https://github.com/josharmour/mtgacoach"

#ifndef AppVersion
  #define AppVersion "1.7.0"
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

[Files]
; Native WinUI launcher (self-contained publish output)
Source: "MtgaCoachLauncher\bin\Release\net8.0-windows10.0.19041.0\win-x64\publish\*"; DestDir: "{app}\launcher"; Flags: ignoreversion recursesubdirs createallsubdirs

; Python coaching engine
Source: "..\src\*"; DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc"
Source: "..\pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion

; Setup and legacy launch support
Source: "..\setup_wizard.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\launcher.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\windows_integration.py"; DestDir: "{app}"; Flags: ignoreversion

; Docs and assets
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\INSTALL.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\mtga_coach.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon.ico"; DestDir: "{app}"; Flags: ignoreversion

; BepInEx plugin build output
Source: "..\bepinex-plugin\MtgaCoachBridge\bin\Release\net472\MtgaCoachBridge.dll"; DestDir: "{app}\bepinex-plugin\MtgaCoachBridge\bin\Release\net472"; Flags: ignoreversion skipifsourcedoesntexist

; BepInEx bundles (optional, for repair/install)
Source: "..\assets\BepInEx\*"; DestDir: "{app}\assets\BepInEx"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "..\third_party\BepInEx\*"; DestDir: "{app}\third_party\BepInEx"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\mtgacoach"; Filename: "{app}\launcher\MtgaCoachLauncher.exe"; WorkingDir: "{app}"; IconFilename: "{app}\mtga_coach.ico"
Name: "{autodesktop}\mtgacoach"; Filename: "{app}\launcher\MtgaCoachLauncher.exe"; WorkingDir: "{app}"; IconFilename: "{app}\mtga_coach.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\launcher\MtgaCoachLauncher.exe"; Description: "Launch mtgacoach"; Flags: nowait postinstall skipifsilent
