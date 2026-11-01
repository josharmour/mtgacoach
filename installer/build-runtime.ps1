param(
    [Parameter(Mandatory = $true)][string]$WheelPath,
    [Parameter(Mandatory = $true)][string]$PythonHome,
    [string]$RuntimeDir,
    [string]$RepoRoot,
    [string]$PythonVersion = "3.11"
)

# Build the relocatable runtime shipped by mtgacoach.iss.
#
# The runtime is a complete official CPython base distribution copied from an
# official python.org installation (CI passes actions/setup-python's install;
# local builds pass an explicit home or the py launcher's base_prefix). It is
# NOT a venv: python.exe/pythonw.exe live at the runtime root next to Lib/,
# DLLs/, and the interpreter DLL, so the directory can be moved anywhere after
# the wheel is installed.
#
# The source installation is never modified: it is copied first, its global
# site-packages/Scripts are discarded, pip is restored from the bundled wheels,
# and the release wheel is installed into the copy with a normal (non-editable)
# pip install.

$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw "build-runtime: $Message"
}

function Assert-OfficialSignature([string]$ExePath) {
    if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
        Fail "missing signed interpreter executable: $ExePath"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $ExePath
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        Fail "$ExePath does not have a valid Authenticode signature (status: $($signature.Status)). Use an official python.org Windows installation."
    }
    if (-not $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        Fail "$ExePath is signed by '$($signature.SignerCertificate.Subject)', not the Python Software Foundation."
    }
}

function Normalize-Path([string]$Path) {
    $trimChars = [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    return ([IO.Path]::GetFullPath($Path)).TrimEnd($trimChars)
}

function Test-SameOrAncestor([string]$Ancestor, [string]$Descendant) {
    $ancestorFull = Normalize-Path $Ancestor
    $descendantFull = Normalize-Path $Descendant
    if ($ancestorFull.Equals($descendantFull, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $descendantFull.StartsWith(
        ($ancestorFull + [IO.Path]::DirectorySeparatorChar),
        [StringComparison]::OrdinalIgnoreCase
    )
}

if (-not $RepoRoot) {
    $RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
}
if (-not $RuntimeDir) {
    $RuntimeDir = Join-Path $RepoRoot "dist\runtime"
}

$RepoRoot = Normalize-Path $RepoRoot
$RuntimeDir = Normalize-Path $RuntimeDir
$PythonHome = Normalize-Path $PythonHome
$WheelPath = Normalize-Path $WheelPath

if (-not (Test-Path -LiteralPath $WheelPath -PathType Leaf)) {
    Fail "wheel not found: $WheelPath"
}
if (-not (Test-Path -LiteralPath (Join-Path $PythonHome "python.exe") -PathType Leaf)) {
    Fail "PythonHome does not contain python.exe: $PythonHome"
}
if (-not (Test-Path -LiteralPath (Join-Path $PythonHome "pythonw.exe") -PathType Leaf)) {
    Fail "PythonHome does not contain pythonw.exe: $PythonHome"
}
if (-not (Test-Path -LiteralPath (Join-Path $PythonHome "Lib\os.py") -PathType Leaf)) {
    Fail "PythonHome does not contain Lib\os.py: not a full CPython distribution."
}
if (-not (Test-Path -LiteralPath (Join-Path $PythonHome "DLLs") -PathType Container)) {
    Fail "PythonHome does not contain DLLs: not a full CPython distribution."
}
if (-not (Test-Path -LiteralPath (Join-Path $PythonHome "Lib\ensurepip\_bundled") -PathType Container)) {
    Fail "PythonHome does not contain Lib\ensurepip\_bundled: cannot bootstrap pip offline."
}
# Official CPython Windows distributions put the interpreter DLL next to
# python.exe at the installation root (the NuGet package does this too); DLLs/
# holds extension modules instead. Select the exact ABI DLL (python311.dll),
# never the stable-ABI python3.dll, because the ._pth name must match it.
Assert-OfficialSignature (Join-Path $PythonHome "python.exe")
Assert-OfficialSignature (Join-Path $PythonHome "pythonw.exe")

$versionOutput = & (Join-Path $PythonHome "python.exe") "-I" "-c" "import sys; print('%d.%d.%d' % sys.version_info[:3])"
if ($LASTEXITCODE -ne 0) {
    Fail "could not query the source interpreter version."
}
$versionText = ($versionOutput | Out-String).Trim()
if (-not $versionText.StartsWith("$PythonVersion.")) {
    Fail "source interpreter is Python $versionText; this release requires Python $PythonVersion.x."
}
$versionParts = $versionText.Split(".")
$interpreterDllName = "python" + $versionParts[0] + $versionParts[1] + ".dll"
$interpreterDll = Join-Path $PythonHome $interpreterDllName
if (-not (Test-Path -LiteralPath $interpreterDll -PathType Leaf)) {
    Fail "PythonHome does not contain $interpreterDllName next to python.exe."
}
if (Test-Path -LiteralPath (Join-Path $PythonHome "pyvenv.cfg")) {
    Fail "PythonHome is a virtual environment ($PythonHome\pyvenv.cfg). Pass the official base installation instead."
}

# Refuse any destination that could delete the source installation, checkout,
# wheel, or a filesystem root before Remove-Item runs below.
$runtimeRoot = Normalize-Path ([IO.Path]::GetPathRoot($RuntimeDir))
if ($RuntimeDir.Equals($runtimeRoot, [StringComparison]::OrdinalIgnoreCase)) {
    Fail "refusing to use a filesystem root as RuntimeDir: $RuntimeDir"
}
$relativeRuntime = $RuntimeDir.Substring($runtimeRoot.Length).Trim([char[]]@('\', '/'))
if (($relativeRuntime -split '[\\/]').Count -lt 2) {
    Fail "refusing shallow RuntimeDir '$RuntimeDir'; use a dedicated directory such as <repo>\dist\runtime."
}
foreach ($protected in @($PythonHome, $RepoRoot, $WheelPath)) {
    if (Test-SameOrAncestor $RuntimeDir $protected) {
        Fail "RuntimeDir '$RuntimeDir' is an ancestor of protected path '$protected'; refusing to delete it."
    }
}
if (Test-SameOrAncestor $PythonHome $RuntimeDir) {
    Fail "RuntimeDir '$RuntimeDir' is inside PythonHome '$PythonHome'; refusing to overwrite the source installation."
}
if ((Test-Path -LiteralPath $RuntimeDir) -and -not (Test-Path -LiteralPath $RuntimeDir -PathType Container)) {
    Fail "RuntimeDir exists but is not a directory: $RuntimeDir"
}

Assert-OfficialSignature (Join-Path $PythonHome "python.exe")
Assert-OfficialSignature (Join-Path $PythonHome "pythonw.exe")

Write-Host "Building relocatable runtime from Python $versionText at $PythonHome"
Write-Host "Installing wheel: $(Split-Path -Leaf $WheelPath)"

if (Test-Path -LiteralPath $RuntimeDir) {
    Remove-Item -LiteralPath $RuntimeDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

# Copy the complete base distribution except host-specific state: global
# site-packages, generated console launchers, bytecode caches, and .pyc files.
# The copy keeps Lib/os.py as the local stdlib landmark used by relocation checks.
$roboArgs = @(
    $PythonHome,
    $RuntimeDir,
    "/E",
    "/XD", (Join-Path $PythonHome "Lib\site-packages"), (Join-Path $PythonHome "Scripts"), "__pycache__",
    "/XF", "*.pyc", "*.pyo",
    "/NFL", "/NDL", "/NJH", "/NJS", "/NP"
)
& robocopy @roboArgs | Out-Null
$roboCode = $LASTEXITCODE
if ($roboCode -ge 8) {
    Fail "robocopy failed with exit code $roboCode while copying the Python distribution."
}

New-Item -ItemType Directory -Force -Path (Join-Path $RuntimeDir "Lib\site-packages") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RuntimeDir "Scripts") | Out-Null

# Build subprocesses must not inherit ambient Python configuration or user-site
# packages from the build host.
$savedEnv = @{}
function Set-BuildEnv([string]$Name, $Value) {
    if (-not $savedEnv.ContainsKey($Name)) {
        $savedEnv[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

$tempUserBase = Join-Path ([IO.Path]::GetTempPath()) ("mtgacoach build userbase " + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tempUserBase | Out-Null

try {
    Set-BuildEnv "PYTHONHOME" ""
    Set-BuildEnv "PYTHONPATH" ""
    Set-BuildEnv "PYTHONSTARTUP" ""
    Set-BuildEnv "PYTHONNOUSERSITE" "1"
    Set-BuildEnv "PYTHONUSERBASE" $tempUserBase
    # Keep only the copied runtime plus Windows system directories on PATH so a
    # missing wheel cannot silently compile against build-host toolchains.
    Set-BuildEnv "PATH" "$RuntimeDir;$env:SystemRoot\System32;$env:SystemRoot"

    # Restore pip/setuptools from the distribution's own bundled wheels so the copy
    # does not inherit packages or absolute paths from the build host.
    & (Join-Path $RuntimeDir "python.exe") "-I" "-m" ensurepip --upgrade --default-pip
    if ($LASTEXITCODE -ne 0) {
        Fail "ensurepip failed in the copied runtime."
    }

    # Normal wheel install into the copied interpreter's site-packages. No venv,
    # no editable install, no build-host paths in package metadata.
    & (Join-Path $RuntimeDir "python.exe") "-I" "-m" pip install --no-input --no-compile --disable-pip-version-check --no-warn-script-location "${WheelPath}[full]"
    if ($LASTEXITCODE -ne 0) {
        Fail "pip install of ${WheelPath}[full] failed."
    }

    # Console launchers embed absolute build-host paths in their PE headers and are
    # unused by the app/shortcuts/workers (all invoke module entrypoints through the
    # bundled interpreter), so do not ship them.
    Get-ChildItem -LiteralPath (Join-Path $RuntimeDir "Scripts") -Filter "*.exe" -File |
        Remove-Item -Force

    # A local wheel install writes direct_url.json pointing at the build checkout;
    # that path must never ship in a relocatable artifact.
    Get-ChildItem -LiteralPath (Join-Path $RuntimeDir "Lib\site-packages") -Filter "direct_url.json" -Recurse -File |
        Remove-Item -Force

    Get-ChildItem -LiteralPath $RuntimeDir -Filter "__pycache__" -Recurse -Directory |
        Remove-Item -Recurse -Force

    # A local ._pth next to python.exe makes every launch isolated from ambient
    # PYTHONPATH/PYTHONHOME/user-site state while keeping this copy's stdlib,
    # extension DLLs, and installed packages importable after relocation.
    # Launches also pass "-X utf8" because isolated mode ignores PYTHONIOENCODING;
    # see installer/mtgacoach.iss and the worker launch arguments.
    Get-ChildItem -LiteralPath $RuntimeDir -Filter "python*._pth" -File | Remove-Item -Force
    $pthName = [IO.Path]::GetFileNameWithoutExtension($interpreterDllName) + "._pth"
    $pthLines = @(".", "Lib", "Lib\site-packages", "DLLs", "import site")
    [IO.File]::WriteAllText(
        (Join-Path $RuntimeDir $pthName),
        (($pthLines -join "`r`n") + "`r`n"),
        (New-Object System.Text.UTF8Encoding($false))
    )

    # Exercise the ._pth without -I: a normal launch must still be isolated and
    # import the bundled package, not ambient PYTHONPATH/user site.
    & (Join-Path $RuntimeDir "python.exe") "-X" utf8 "-c" "import sys, arenamcp, PySide6; assert sys.flags.isolated == 1, sys.flags; print('runtime import ok:', arenamcp.__version__)"
    if ($LASTEXITCODE -ne 0) {
        Fail "the copied runtime cannot import arenamcp/PySide6 with its isolation file."
    }

    $wheelHash = (Get-FileHash -LiteralPath $WheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifest = [ordered]@{
        schema         = 1
        layout         = "official-cpython-base"
        python_version = $versionText
        isolation      = $pthName
        launch_flags   = @("-I", "-X", "utf8")
        wheel          = Split-Path -Leaf $WheelPath
        wheel_sha256   = $wheelHash
        built_utc      = [DateTime]::UtcNow.ToString("o")
    }
    [IO.File]::WriteAllText(
        (Join-Path $RuntimeDir "runtime-manifest.json"),
        (($manifest | ConvertTo-Json) + [Environment]::NewLine),
        (New-Object System.Text.UTF8Encoding($false))
    )
}
finally {
    foreach ($key in @($savedEnv.Keys)) {
        if ($null -eq $savedEnv[$key]) {
            Remove-Item -LiteralPath "Env:$key" -ErrorAction SilentlyContinue
        }
        else {
            [Environment]::SetEnvironmentVariable($key, [string]$savedEnv[$key], "Process")
        }
    }
    Remove-Item -LiteralPath $tempUserBase -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Relocatable runtime ready at $RuntimeDir"
