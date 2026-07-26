#define MyAppName "mtgacoach"
#define MyAppPublisher "Josh Armour"
#define MyAppURL "https://github.com/josharmour/mtgacoach"

; AppVersion is supplied by the caller (/DAppVersion=X.Y.Z):
;   installer\build-installer.ps1   - local release builds
;   .github/workflows/installer.yml - CI release builds
; Both read the single source of truth, src\arenamcp\__init__.py (pyproject.toml
; uses hatch dynamic versioning and carries no literal version).
;
; There is deliberately NO hardcoded fallback here. There used to be one, and a
; hardcoded literal silently stamps the PREVIOUS version onto every installer
; built after a version bump: a wrong number in Add/Remove Programs and on the
; release asset, with nothing failing to warn you. Fail loudly instead.
#ifndef AppVersion
  #error AppVersion is not defined. Build with installer\build-installer.ps1, or pass /DAppVersion=X.Y.Z matching __version__ in src\arenamcp\__init__.py.
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
; Remove obsolete launcher files from prior installs (v2.0.1 and earlier).
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
; Repair-audit blocker #1: the DLL now ships INSIDE the Python package
; (src/arenamcp/resources/) so pip installs always have it. The installer
; copy is a hard requirement — no skipifsourcedoesntexist: a DLL-less
; installer build must FAIL, not silently ship a broken bridge.
Source: "..\src\arenamcp\resources\MtgaCoachBridge.dll"; DestDir: "{app}\src\arenamcp\resources"; Flags: ignoreversion

; BepInEx bundles for repair/install
Source: "..\assets\BepInEx\*"; DestDir: "{app}\assets\BepInEx"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\assets\winhttp.dll"; DestDir: "{app}\assets"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\assets\doorstop_config.ini"; DestDir: "{app}\assets"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\mtgacoach"; Filename: "{app}\launch.vbs"; WorkingDir: "{app}"; IconFilename: "{app}\mtga_coach.ico"
Name: "{autodesktop}\mtgacoach"; Filename: "{app}\launch.vbs"; WorkingDir: "{app}"; IconFilename: "{app}\mtga_coach.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\launch.bat"; Description: "Launch mtgacoach (first run installs the Python environment)"; Flags: postinstall skipifsilent runasoriginaluser shellexec
