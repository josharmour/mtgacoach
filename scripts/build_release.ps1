param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$ScriptPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
    $MyInvocation.MyCommand.Path
)
$ScriptDir = Split-Path -Parent $ScriptPath
$RepoRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
    (Join-Path $ScriptDir "..")
)
$StageRoot = Join-Path $RepoRoot "dist\desktop-release"
$StageApp = Join-Path $StageRoot "app"
$RuntimeDir = Join-Path $StageApp "runtime"
$StagePython = Join-Path $RuntimeDir "Scripts\python.exe"
$StagePip = Join-Path $RuntimeDir "Scripts\pip.exe"

if (Test-Path $StageRoot) {
    Remove-Item -Recurse -Force $StageRoot
}
New-Item -ItemType Directory -Force -Path $StageApp | Out-Null

$VersionLine = Select-String -Path (Join-Path $RepoRoot "pyproject.toml") -Pattern '^\s*version\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $VersionLine) {
    throw "Could not read version from pyproject.toml"
}
$Version = $VersionLine.Matches[0].Groups[1].Value
Write-Host "Staging mtgacoach v$Version desktop release"

Write-Host "Creating bundled runtime..."
& $Python -m venv $RuntimeDir

Write-Host "Upgrading pip..."
& $StagePython -m pip install --upgrade pip

Write-Host "Installing packaged app dependencies..."
& $StagePip install "${RepoRoot}[full]"

Write-Host "Copying application files..."
$copyMap = @(
    @{ Source = "src"; Destination = "src" },
    @{ Source = "assets"; Destination = "assets" },
    @{ Source = "scripts\launch_installed.py"; Destination = "scripts\launch_installed.py" },
    @{ Source = "pyproject.toml"; Destination = "pyproject.toml" },
    @{ Source = "requirements.txt"; Destination = "requirements.txt" },
    @{ Source = "README.md"; Destination = "README.md" },
    @{ Source = "INSTALL.md"; Destination = "INSTALL.md" },
    @{ Source = "mtga_coach.ico"; Destination = "mtga_coach.ico" },
    @{ Source = "icon.ico"; Destination = "icon.ico" },
    @{ Source = "setup_wizard.py"; Destination = "setup_wizard.py" },
    @{ Source = "windows_integration.py"; Destination = "windows_integration.py" },
    @{ Source = "DEV.md"; Destination = "DEV.md" }
)

foreach ($entry in $copyMap) {
    $sourcePath = Join-Path $RepoRoot $entry.Source
    if (-not (Test-Path $sourcePath)) {
        continue
    }

    $destPath = Join-Path $StageApp $entry.Destination
    $destParent = Split-Path -Parent $destPath
    if ($destParent) {
        New-Item -ItemType Directory -Force -Path $destParent | Out-Null
    }

    if ((Get-Item $sourcePath) -is [System.IO.DirectoryInfo]) {
        Copy-Item -Recurse -Force $sourcePath $destPath
    } else {
        Copy-Item -Force $sourcePath $destPath
    }
}

$PluginDll = Join-Path $RepoRoot "bepinex-plugin\MtgaCoachBridge\bin\Release\net472\MtgaCoachBridge.dll"
if (Test-Path $PluginDll) {
    $PluginDest = Join-Path $StageApp "bepinex-plugin\MtgaCoachBridge\bin\Release\net472"
    New-Item -ItemType Directory -Force -Path $PluginDest | Out-Null
    Copy-Item -Force $PluginDll (Join-Path $PluginDest "MtgaCoachBridge.dll")
} else {
    Write-Warning "Bridge plugin DLL not found at $PluginDll"
}

Write-Host "Desktop release staged at $StageApp"
