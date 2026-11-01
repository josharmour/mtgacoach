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
$PythonwExe = Join-Path $RuntimeDir "pythonw.exe"

# Build the wheel from the current checkout, then populate the relocatable
# runtime with the shared builder used by CI. Never create a venv here: the
# shipped runtime must be a complete official CPython base distribution.
Push-Location $RepoRoot
try {
    & python -m build --wheel
    if ($LASTEXITCODE -ne 0) { throw "python -m build --wheel failed" }
}
finally {
    Pop-Location
}

$Wheel = Get-ChildItem -LiteralPath (Join-Path $RepoRoot "dist") -Filter "*.whl" -File |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Wheel) { throw "No wheel found under dist after python -m build --wheel" }
if ($Wheel.Name -notlike "arenamcp-$Version-*") {
    throw "Newest wheel '$($Wheel.Name)' does not match package version $Version"
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonHome = (& py -3.11 -c "import sys; print(sys.base_prefix)" | Out-String).Trim()
} else {
    $PythonHome = (& python -c "import sys; print(sys.base_prefix)" | Out-String).Trim()
}
if (-not $PythonHome) { throw "Could not locate an official Python base installation for the runtime." }

Write-Host "Populating relocatable runtime in dist\runtime from $PythonHome..."
& (Join-Path $ScriptDir "build-runtime.ps1") `
    -WheelPath $Wheel.FullName `
    -PythonHome $PythonHome `
    -RuntimeDir $RuntimeDir `
    -RepoRoot $RepoRoot
if ($LASTEXITCODE -ne 0) { throw "build-runtime.ps1 failed" }

if (-not (Test-Path $PythonwExe)) {
    throw "Runtime creation failed to produce $PythonwExe"
}

Write-Host "Verifying runtime relocation before packaging..."
& (Join-Path $ScriptDir "verify-runtime-relocation.ps1") `
    -RuntimeDir $RuntimeDir `
    -CheckoutRoot $RepoRoot `
    -Relocate `
    -Label "local-staged"
if ($LASTEXITCODE -ne 0) { throw "runtime relocation verification failed" }

Write-Host "Compiling Inno Setup installer with $InnoSetupCompiler..."
Push-Location $ScriptDir
try {
    & $InnoSetupCompiler "/DAppVersion=$Version" "mtgacoach.iss"
}
finally {
    Pop-Location
}
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed" }
