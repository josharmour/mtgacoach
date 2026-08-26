#define MyAppName "mtgacoach"
#define MyAppPublisher "Josh Armour"
#define MyAppURL "https://github.com/josharmour/mtgacoach"

; AppVersion is supplied by the caller (/DAppVersion=X.Y.Z):
;   installer\build-installer.ps1   - local release builds
;   .github/workflows/installer.yml - CI release builds
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
OutputBaseFilename=mtgacoach-Setup-v2
SetupIconFile=..\mtga_coach.ico
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
UninstallDisplayIcon={app}\mtga_coach.ico
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional icons:"

[InstallDelete]
; Remove obsolete files and unsigned wrappers from prior installs
Type: files; Name: "{app}\mtgacoach.exe"
Type: files; Name: "{app}\launch.bat"
Type: files; Name: "{app}\launch.vbs"
Type: files; Name: "{app}\setup_wizard.py"
Type: filesandordirs; Name: "{app}\launcher"
Type: filesandordirs; Name: "{app}\scripts"

[Files]
; Standalone embedded Python runtime with all dependencies pre-installed (Smart App Control compliant)
Source: "..\dist\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs

; Application source and assets
Source: "..\src\*"; DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc"
Source: "..\assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\INSTALL.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\mtga_coach.ico"; DestDir: "{app}"; Flags: ignoreversion

; Bridge plugin build output
Source: "..\src\arenamcp\resources\MtgaCoachBridge.dll"; DestDir: "{app}\src\arenamcp\resources"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\mtgacoach"; Filename: "{app}\runtime\Scripts\pythonw.exe"; Parameters: "-m arenamcp.desktop"; WorkingDir: "{app}"; IconFilename: "{app}\mtga_coach.ico"
Name: "{autodesktop}\mtgacoach"; Filename: "{app}\runtime\Scripts\pythonw.exe"; Parameters: "-m arenamcp.desktop"; WorkingDir: "{app}"; IconFilename: "{app}\mtga_coach.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\Scripts\pythonw.exe"; Parameters: "-m arenamcp.desktop"; WorkingDir: "{app}"; Description: "Launch mtgacoach"; Flags: postinstall skipifsilent nowait runasoriginaluser

[Code]
// Helper function to find MTGA install folder
function FindMtgaInstallDir(): String;
var
  RegPath: String;
  Candidate: String;
begin
  Result := '';

  // 1. Check official standalone installer path
  Candidate := ExpandConstant('{autopf}\Wizards of the Coast\MTGA');
  if DirExists(Candidate) then
  begin
    Result := Candidate;
    Exit;
  end;

  // 2. Check 32-bit Program Files
  Candidate := ExpandConstant('{autopf32}\Wizards of the Coast\MTGA');
  if DirExists(Candidate) then
  begin
    Result := Candidate;
    Exit;
  end;

  // 3. Check Steam library path
  Candidate := ExpandConstant('{autopf32}\Steam\steamapps\common\MTGA');
  if DirExists(Candidate) then
  begin
    Result := Candidate;
    Exit;
  end;
  Candidate := ExpandConstant('{autopf}\Steam\steamapps\common\MTGA');
  if DirExists(Candidate) then
  begin
    Result := Candidate;
    Exit;
  end;

  // 4. Check registry
  if RegQueryStringValue(HKLM64, 'SOFTWARE\Wizards of the Coast\MTGA', 'InstallDir', RegPath) and DirExists(RegPath) then
  begin
    Result := RegPath;
    Exit;
  end;
  if RegQueryStringValue(HKLM32, 'SOFTWARE\Wizards of the Coast\MTGA', 'InstallDir', RegPath) and DirExists(RegPath) then
  begin
    Result := RegPath;
    Exit;
  end;
end;

// Directory copy helper
procedure CopyDirectory(SourceDir, DestDir: String);
var
  FindRec: TFindRec;
  SrcPath, DstPath: String;
begin
  if not DirExists(DestDir) then
    CreateDir(DestDir);

  if FindFirst(SourceDir + '\*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          SrcPath := SourceDir + '\' + FindRec.Name;
          DstPath := DestDir + '\' + FindRec.Name;
          if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
            CopyDirectory(SrcPath, DstPath)
          else
            FileCopy(SrcPath, DstPath, False);
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  MtgaDir: String;
  AppDir: String;
  PluginSrc: String;
  PluginDst: String;
begin
  if CurStep = ssPostInstall then
  begin
    MtgaDir := FindMtgaInstallDir();
    AppDir := ExpandConstant('{app}');

    if MtgaDir <> '' then
    begin
      Log('Detected MTGA install directory: ' + MtgaDir);

      // Copy BepInEx assets into MTGA
      if DirExists(AppDir + '\assets\BepInEx') then
      begin
        Log('Deploying BepInEx core into MTGA...');
        CopyDirectory(AppDir + '\assets\BepInEx', MtgaDir + '\BepInEx');
      end;

      if FileExists(AppDir + '\assets\winhttp.dll') then
        FileCopy(AppDir + '\assets\winhttp.dll', MtgaDir + '\winhttp.dll', False);

      if FileExists(AppDir + '\assets\doorstop_config.ini') then
        FileCopy(AppDir + '\assets\doorstop_config.ini', MtgaDir + '\doorstop_config.ini', False);

      // Deploy MtgaCoachBridge.dll
      PluginSrc := AppDir + '\src\arenamcp\resources\MtgaCoachBridge.dll';
      PluginDst := MtgaDir + '\BepInEx\plugins\MtgaCoachBridge.dll';
      if FileExists(PluginSrc) then
      begin
        if not DirExists(MtgaDir + '\BepInEx\plugins') then
          ForceDirectories(MtgaDir + '\BepInEx\plugins');
        Log('Deploying bridge plugin from ' + PluginSrc + ' to ' + PluginDst);
        FileCopy(PluginSrc, PluginDst, False);
      end;
    end
    else
    begin
      Log('MTGA install directory not found automatically; bridge can be installed via Repair tab.');
    end;
  end;
end;
