#define MyAppName "mtgacoach"
#define MyAppPublisher "Josh Armour"
#define MyAppURL "https://github.com/josharmour/mtgacoach"

#ifndef AppVersion
  #define AppVersion "2.0.4"
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

[InstallDelete]
; Remove obsolete WinUI launcher from prior installs (v2.0.1 and earlier).
Type: filesandordirs; Name: "{app}\launcher"
Type: filesandordirs; Name: "{app}\runtime"

[Files]
; PySide desktop app source
Source: "..\src\*"; DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc"
Source: "..\pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion

; Setup and installed-app launch helpers
Source: "..\setup_wizard.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\launch_installed.py"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\launch.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\launch.vbs"; DestDir: "{app}"; Flags: ignoreversion

; Docs and icons
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\INSTALL.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\mtga_coach.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon.ico"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

; Bridge plugin build output
Source: "..\bepinex-plugin\MtgaCoachBridge\bin\Release\net472\MtgaCoachBridge.dll"; DestDir: "{app}\bepinex-plugin\MtgaCoachBridge\bin\Release\net472"; Flags: ignoreversion skipifsourcedoesntexist

; BepInEx bundles for repair/install
Source: "..\assets\BepInEx\*"; DestDir: "{app}\assets\BepInEx"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\assets\winhttp.dll"; DestDir: "{app}\assets"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\assets\doorstop_config.ini"; DestDir: "{app}\assets"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\mtgacoach"; Filename: "{app}\launch.vbs"; WorkingDir: "{app}"; IconFilename: "{app}\mtga_coach.ico"
Name: "{autodesktop}\mtgacoach"; Filename: "{app}\launch.vbs"; WorkingDir: "{app}"; IconFilename: "{app}\mtga_coach.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\launch.bat"; Description: "Launch mtgacoach (first run installs the Python environment)"; Flags: postinstall skipifsilent runasoriginaluser shellexec
