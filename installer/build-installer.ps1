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

# Locate Inno Setup compiler if default is missing
if (-not (Test-Path $InnoSetupCompiler)) {
    $rawCandidates = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
        "C:\Program Files\Inno Setup 7\ISCC.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe"),
        ((Get-Command iscc -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)),
        ((Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1))
    )

    $foundCompiler = $null
    foreach ($c in $rawCandidates) {
        if ($c -and (Test-Path $c)) {
            $foundCompiler = $c
            break
        }
    }

    if ($foundCompiler) {
        $InnoSetupCompiler = $foundCompiler
    } else {
        throw "Inno Setup compiler (ISCC.exe) not found. Install Inno Setup 6 (e.g. 'winget install JRSoftware.InnoSetup') or specify -InnoSetupCompiler <path>."
    }
}

$VersionLine = Select-String -Path $InitPy -Pattern '^\s*__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $VersionLine) {
    throw "Could not read __version__ from $InitPy"
}

$Version = $VersionLine.Matches[0].Groups[1].Value
Write-Host "Building mtgacoach installer for v$Version with $InnoSetupCompiler"

$ResourceDll = Join-Path $RepoRoot "src\arenamcp\resources\MtgaCoachBridge.dll"
$DevPluginDll = Join-Path $RepoRoot "bepinex-plugin\MtgaCoachBridge\bin\Release\net472\MtgaCoachBridge.dll"

if (-not (Test-Path $ResourceDll)) {
    if (Test-Path $DevPluginDll) {
        Write-Host "Copying fresh plugin DLL from dev tree to resources..."
        Copy-Item -LiteralPath $DevPluginDll -Destination $ResourceDll -Force
    } else {
        throw "Bridge plugin DLL not found at $ResourceDll. Build the plugin before cutting a release installer."
    }
}

$RuntimeDir = Join-Path $RepoRoot "dist\runtime"
$PythonwExe = Join-Path $RuntimeDir "Scripts\pythonw.exe"

if (-not (Test-Path $PythonwExe) -or -not ((Get-AuthenticodeSignature $PythonwExe).Status -eq "Valid")) {
    Write-Host "Creating standalone Python runtime in dist\runtime using signed Python..."
    if (Test-Path $RuntimeDir) {
        Remove-Item -Recurse -Force $RuntimeDir
    }
    Push-Location $RepoRoot
    try {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3.11 -m venv dist/runtime
        }
        if (-not (Test-Path $PythonwExe)) {
            & python -m venv dist/runtime
        }
        if (-not (Test-Path $PythonwExe)) { throw "python -m venv failed to create dist/runtime" }
        & (Join-Path $RuntimeDir "Scripts\python.exe") -m pip install --upgrade pip
        & (Join-Path $RuntimeDir "Scripts\python.exe") -m pip install -e .[desktop,full]
        if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path $PythonwExe)) {
    throw "Runtime creation failed to produce $PythonwExe"
}

Write-Host "Compiling Inno Setup installer with $InnoSetupCompiler..."
Push-Location $ScriptDir
try {
    & $InnoSetupCompiler "/DAppVersion=$Version" "mtgacoach.iss"
}
finally {
    Pop-Location
}
