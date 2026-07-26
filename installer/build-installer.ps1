param(
    [string]$InnoSetupCompiler = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
)

$ErrorActionPreference = "Stop"

$ScriptPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
    $MyInvocation.MyCommand.Path
)
$ScriptDir = Split-Path -Parent $ScriptPath
$RepoRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
    (Join-Path $ScriptDir "..")
)
# Version source of truth: src/arenamcp/__init__.py.
# pyproject.toml switched to hatch DYNAMIC versioning (`dynamic = ["version"]`
# + [tool.hatch.version] path = "src/arenamcp/__init__.py"), so it no longer
# contains a literal `version = "..."` line. Parsing it here threw
# "Could not read version from ...\pyproject.toml" on every release build.
$InitPy = Join-Path $RepoRoot "src\arenamcp\__init__.py"

if (-not (Test-Path $InnoSetupCompiler)) {
    throw "Inno Setup compiler not found: $InnoSetupCompiler"
}

$VersionLine = Select-String -Path $InitPy -Pattern '^\s*__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $VersionLine) {
    throw "Could not read __version__ from $InitPy"
}

$Version = $VersionLine.Matches[0].Groups[1].Value
Write-Host "Building mtgacoach installer for v$Version"

$PluginDll = Join-Path $RepoRoot "bepinex-plugin\MtgaCoachBridge\bin\Release\net472\MtgaCoachBridge.dll"
if (-not (Test-Path $PluginDll)) {
    Write-Warning "Bridge plugin DLL not found at $PluginDll"
    Write-Warning "Build the plugin before cutting a release installer."
}

Push-Location $ScriptDir
try {
    & $InnoSetupCompiler "/DAppVersion=$Version" "mtgacoach.iss"
}
finally {
    Pop-Location
}
