param(
    [Parameter(Mandatory = $true)][string]$RuntimeDir,
    [string]$AppRoot,
    [string]$CheckoutRoot,
    [switch]$Relocate,
    [string]$Label = "runtime"
)

# Fail-closed relocation verification for the bundled Windows runtime.
#
# Checks the artifact itself (official signed interpreters, full stdlib/DLLs,
# wheel metadata, no venv/editable/build-host paths), then runs an isolated
# import/subprocess/GUI-module smoke with the bundled interpreter while the
# checkout source is hidden and ambient Python environment variables are gone.
#
# -Relocate copies the runtime to a temporary path containing spaces first;
# without it the runtime is verified in place (used for the silent-install
# check after Inno Setup has placed it under a path with spaces).

$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw "verify-runtime-relocation ($Label): $Message"
}

function Assert-File([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Fail "missing $Description at $Path"
    }
}

function Assert-Dir([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        Fail "missing $Description at $Path"
    }
}

function Assert-OfficialSignature([string]$ExePath) {
    Assert-File $ExePath "signed interpreter executable"
    $signature = Get-AuthenticodeSignature -LiteralPath $ExePath
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        Fail "$ExePath does not have a valid Authenticode signature (status: $($signature.Status))."
    }
    if (-not $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        Fail "$ExePath is signed by '$($signature.SignerCertificate.Subject)', not the Python Software Foundation."
    }
}

if ($env:OS -ne "Windows_NT") {
    Fail "this verifier only runs on Windows."
}

$RuntimeDir = [IO.Path]::GetFullPath($RuntimeDir)
if (-not $AppRoot) {
    $AppRoot = Split-Path -Parent $RuntimeDir
}
$AppRoot = [IO.Path]::GetFullPath($AppRoot)
if ($CheckoutRoot) {
    $CheckoutRoot = [IO.Path]::GetFullPath($CheckoutRoot)
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("mtgacoach relocation " + [guid]::NewGuid().ToString("N"))
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ("mtgacoach smoke " + [guid]::NewGuid().ToString("N"))
$hiddenSrc = $null
$originalSrc = $null
$savedEnv = @{}

function Set-ProcessEnv([string]$Name, $Value) {
    if (-not $savedEnv.ContainsKey($Name)) {
        $savedEnv[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

try {
    if ($Relocate) {
        New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
        $relocatedRuntime = Join-Path $tempRoot "runtime"
        & robocopy $RuntimeDir $relocatedRuntime /E /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) {
            Fail "robocopy failed with exit code $LASTEXITCODE while relocating the runtime."
        }
        Write-Host "[$Label] relocated runtime to path with spaces: $relocatedRuntime"
        $RuntimeDir = $relocatedRuntime
        # The relocated staging tree contains only runtime/. get_app_root() must
        # recognize the installed-runtime landmark without checkout markers.
        $AppRoot = $tempRoot
    }

    Assert-File (Join-Path $RuntimeDir "python.exe") "python.exe at the runtime root"
    Assert-File (Join-Path $RuntimeDir "pythonw.exe") "pythonw.exe at the runtime root"
    Assert-File (Join-Path $RuntimeDir "Lib\os.py") "stdlib landmark Lib\os.py"
    Assert-Dir (Join-Path $RuntimeDir "DLLs") "DLLs directory"
    Assert-Dir (Join-Path $RuntimeDir "Lib\site-packages") "site-packages directory"
    Assert-File (Join-Path $RuntimeDir "Lib\site-packages\arenamcp\__init__.py") "installed arenamcp package"
    Assert-File (Join-Path $RuntimeDir "runtime-manifest.json") "runtime manifest"

    # Official CPython Windows distributions keep the ABI DLL (python311.dll)
    # next to python.exe; DLLs/ holds extension modules. The stable-ABI
    # python3.dll is not the interpreter DLL and must not drive the ._pth name.
    # The exact name is derived from the manifest's python_version below.

    # The old installer shipped a venv at runtime\Scripts. It must never survive
    # an upgrade or be selected by find_python_executable().
    if (Test-Path -LiteralPath (Join-Path $RuntimeDir "Scripts\python.exe")) {
        Fail "legacy venv interpreter still present at Scripts\python.exe."
    }
    if (Test-Path -LiteralPath (Join-Path $RuntimeDir "pyvenv.cfg")) {
        Fail "runtime contains pyvenv.cfg; it is a venv, not a relocatable base distribution."
    }
    if (Get-ChildItem -LiteralPath (Join-Path $RuntimeDir "Lib\site-packages") -Filter "__editable__*" -File | Select-Object -First 1) {
        Fail "editable-install marker found in site-packages."
    }

    Assert-OfficialSignature (Join-Path $RuntimeDir "python.exe")
    Assert-OfficialSignature (Join-Path $RuntimeDir "pythonw.exe")

    try {
        $manifest = Get-Content -LiteralPath (Join-Path $RuntimeDir "runtime-manifest.json") -Raw | ConvertFrom-Json
    }
    catch {
        Fail "runtime-manifest.json is not valid JSON: $_"
    }
    if (-not $manifest.python_version -or -not $manifest.wheel_sha256) {
        Fail "runtime-manifest.json is missing python_version/wheel_sha256."
    }
    if ($manifest.layout -ne "official-cpython-base") {
        Fail "runtime-manifest.json layout is '$($manifest.layout)', expected 'official-cpython-base'."
    }
    $versionParts = ([string]$manifest.python_version).Split(".")
    if ($versionParts.Count -lt 2) {
        Fail "runtime-manifest.json python_version '$($manifest.python_version)' is not major.minor."
    }
    $expectedDllName = "python" + $versionParts[0] + $versionParts[1] + ".dll"
    Assert-File (Join-Path $RuntimeDir $expectedDllName) "$expectedDllName next to python.exe"
    $expectedPthName = [IO.Path]::GetFileNameWithoutExtension($expectedDllName) + "._pth"

    # The local ._pth is the artifact-level isolation contract: it keeps imports
    # inside this copy and ignores ambient PYTHONPATH/PYTHONHOME/user site.
    $pthFiles = @(Get-ChildItem -LiteralPath $RuntimeDir -Filter "python3*._pth" -File)
    if ($pthFiles.Count -ne 1) {
        Fail "expected exactly one python3*._pth isolation file next to python.exe, found $($pthFiles.Count)."
    }
    if ($pthFiles[0].Name -ne $expectedPthName) {
        Fail "isolation file is '$($pthFiles[0].Name)', expected '$expectedPthName' for $expectedDllName."
    }
    if ($manifest.isolation -ne $pthFiles[0].Name) {
        Fail "runtime-manifest.json isolation '$($manifest.isolation)' does not match '$($pthFiles[0].Name)'."
    }
    foreach ($requiredLine in @(".", "Lib", "Lib\site-packages", "DLLs", "import site")) {
        if (-not ((Get-Content -LiteralPath $pthFiles[0].FullName) -contains $requiredLine)) {
            Fail "$($pthFiles[0].Name) is missing required line '$requiredLine'."
        }
    }

    # Hide the checkout source so imports cannot silently fall back to it.
    if ($CheckoutRoot) {
        $originalSrc = Join-Path $CheckoutRoot "src"
        if (Test-Path -LiteralPath $originalSrc -PathType Container) {
            if (Get-ChildItem -LiteralPath $CheckoutRoot -Filter "src.__relocation_hidden_*" -Force | Select-Object -First 1) {
                Fail "a previous hidden checkout source already exists under $CheckoutRoot."
            }
            $hiddenName = "src.__relocation_hidden_" + [guid]::NewGuid().ToString("N")
            Rename-Item -LiteralPath $originalSrc -NewName $hiddenName
            $hiddenSrc = Join-Path $CheckoutRoot $hiddenName
            Write-Host "[$Label] hid checkout source at $originalSrc during smoke"
        }
    }

    foreach ($sub in @("home", "localappdata", "appdata", "tmp", "runtime-root", "userbase")) {
        New-Item -ItemType Directory -Force -Path (Join-Path $sandbox $sub) | Out-Null
    }
    # Hostile decoys: if ._pth isolation is broken, a normal (non -I) launch
    # would import these instead of the bundled package.
    $decoyPath = Join-Path $sandbox "hostile-pythonpath"
    New-Item -ItemType Directory -Force -Path (Join-Path $decoyPath "arenamcp") | Out-Null
    Set-Content -LiteralPath (Join-Path $decoyPath "arenamcp\__init__.py") -Value 'raise RuntimeError("hostile PYTHONPATH was imported")' -Encoding utf8
    $decoyUserSite = Join-Path $sandbox "userbase\site-packages\arenamcp"
    New-Item -ItemType Directory -Force -Path $decoyUserSite | Out-Null
    Set-Content -LiteralPath (Join-Path $decoyUserSite "__init__.py") -Value 'raise RuntimeError("hostile user site was imported")' -Encoding utf8

    # Isolate the child from ambient Python installs, user settings, and the
    # build checkout. Only the bundled runtime plus Windows system DLLs remain.
    # PYTHONPATH/user-site are deliberately hostile: the runtime's ._pth must
    # ignore them even without -I.
    Set-ProcessEnv "PATH" "$RuntimeDir;$env:SystemRoot\System32;$env:SystemRoot"
    Set-ProcessEnv "PYTHONPATH" $decoyPath
    Set-ProcessEnv "PYTHONHOME" ""
    Set-ProcessEnv "PYTHONSTARTUP" ""
    Set-ProcessEnv "PYTHONUSERBASE" (Join-Path $sandbox "userbase")
    Set-ProcessEnv "PYTHONNOUSERSITE" ""
    Set-ProcessEnv "PYTHONDONTWRITEBYTECODE" "1"
    Set-ProcessEnv "PYTHONUTF8" "1"
    Set-ProcessEnv "VIRTUAL_ENV" ""
    Set-ProcessEnv "CONDA_PREFIX" ""
    Set-ProcessEnv "MTGACOACH_APP_ROOT" ""
    Set-ProcessEnv "MTGACOACH_RUNTIME_ROOT" (Join-Path $sandbox "runtime-root")
    Set-ProcessEnv "HOME" (Join-Path $sandbox "home")
    Set-ProcessEnv "USERPROFILE" (Join-Path $sandbox "home")
    Set-ProcessEnv "LOCALAPPDATA" (Join-Path $sandbox "localappdata")
    Set-ProcessEnv "APPDATA" (Join-Path $sandbox "appdata")
    Set-ProcessEnv "TEMP" (Join-Path $sandbox "tmp")
    Set-ProcessEnv "TMP" (Join-Path $sandbox "tmp")
    Set-ProcessEnv "QT_QPA_PLATFORM" "offscreen"
    Set-ProcessEnv "MTGACOACH_SMOKE_RUNTIME" $RuntimeDir
    Set-ProcessEnv "MTGACOACH_SMOKE_SANDBOX" $sandbox
    Set-ProcessEnv "MTGACOACH_SMOKE_APP_ROOT" $AppRoot
    Set-ProcessEnv "MTGACOACH_SMOKE_CHECKOUT" $(if ($CheckoutRoot) { $CheckoutRoot } else { "" })
    Set-ProcessEnv "MTGACOACH_SMOKE_DECOY" $decoyPath
    Set-ProcessEnv "MTGACOACH_SMOKE_LABEL" "$Label"

    $smokePy = Join-Path $sandbox "smoke.py"
    $smokeCode = @'
import ctypes
import decimal
import hashlib
import json
import os
import sqlite3
import ssl
import subprocess
import sys
import zlib
from encodings import idna as encodings_idna
from importlib.metadata import distribution, version
from pathlib import Path

errors = []
checks = 0


def check(condition, message):
    global checks
    checks += 1
    if not condition:
        errors.append(message)


runtime = Path(os.environ["MTGACOACH_SMOKE_RUNTIME"]).resolve()
app_root = Path(os.environ["MTGACOACH_SMOKE_APP_ROOT"]).resolve()
checkout_text = os.environ.get("MTGACOACH_SMOKE_CHECKOUT", "").strip()
checkout = Path(checkout_text).resolve() if checkout_text else None

check(Path(sys.executable).resolve() == (runtime / "python.exe").resolve(), f"sys.executable={sys.executable!r}, expected {runtime / 'python.exe'}")
check(Path(sys.prefix).resolve() == runtime, f"sys.prefix={sys.prefix!r}, expected {runtime}")
check(Path(sys.base_prefix).resolve() == runtime, f"sys.base_prefix={sys.base_prefix!r}, expected {runtime}")
check(sys.flags.isolated == 1, f"interpreter is not isolated: sys.flags.isolated={sys.flags.isolated}")
check(sys.flags.utf8_mode == 1, f"interpreter is not in UTF-8 mode: sys.flags.utf8_mode={sys.flags.utf8_mode}")
check((runtime / "pythonw.exe").is_file(), f"missing {runtime / 'pythonw.exe'}")
check((runtime / "Lib" / "os.py").is_file(), f"missing {runtime / 'Lib' / 'os.py'}")
expected_dll = f"python{sys.version_info[0]}{sys.version_info[1]}.dll"
expected_pth = expected_dll[:-4] + "._pth"
check((runtime / expected_dll).is_file(), f"missing {runtime / expected_dll}")
check((runtime / expected_pth).is_file(), f"missing {runtime / expected_pth}")
check(not (runtime / "Scripts" / "python.exe").exists(), f"legacy venv interpreter present at {runtime / 'Scripts' / 'python.exe'}")
check(not (runtime / "pyvenv.cfg").exists(), f"pyvenv.cfg present at {runtime}")

decoy_text = os.environ.get("MTGACOACH_SMOKE_DECOY", "").strip()
decoy = Path(decoy_text).resolve() if decoy_text else None
for entry in sys.path:
    resolved = Path(entry or os.getcwd()).resolve()
    if checkout is not None and (resolved == checkout or checkout in resolved.parents):
        errors.append(f"sys.path contains checkout path: {resolved}")
        checks += 1
    if decoy is not None and (resolved == decoy or decoy in resolved.parents):
        errors.append(f"sys.path contains hostile PYTHONPATH decoy: {resolved}")
        checks += 1
    if resolved.name == "site-packages" and runtime not in resolved.parents:
        errors.append(f"sys.path contains foreign site-packages: {resolved}")
        checks += 1

import arenamcp  # noqa: E402
import numpy  # noqa: E402
import onnxruntime  # noqa: E402
import PySide6  # noqa: E402
import soundfile  # noqa: E402
from PySide6.QtCore import QLibraryInfo  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402
from arenamcp import tts as tts_module  # noqa: E402
from arenamcp.commentary import bootstrap as commentary_bootstrap  # noqa: E402
from arenamcp.desktop import app as desktop_app  # noqa: E402
from arenamcp.desktop import runtime as desktop_runtime  # noqa: E402
from arenamcp.desktop import tts_worker  # noqa: E402
import arenamcp.standalone as standalone_engine  # noqa: E402

for module in (
    arenamcp,
    PySide6,
    commentary_bootstrap,
    desktop_app,
    desktop_runtime,
    tts_module,
    tts_worker,
    standalone_engine,
):
    module_file = Path(module.__file__).resolve()
    check(runtime in module_file.parents, f"{module.__name__} imported from {module_file}, expected under {runtime}")

# Stdlib extension modules prove the copied DLLs/ directory is complete.
# ``zlib`` is built into python.exe on Windows and has no ``__file__``; its
# round-trip below proves the codec works without a path to check.
for stdlib_module in (encodings_idna, ssl, sqlite3, ctypes, decimal):
    module_file = Path(stdlib_module.__file__).resolve()
    check(runtime in module_file.parents, f"stdlib {stdlib_module.__name__} imported from {module_file}, expected under {runtime}")
check(zlib.decompress(zlib.compress(b"relocation")) == b"relocation", "zlib round-trip failed")

# Native third-party dependencies must load from the bundled site-packages.
for dependency in (numpy, onnxruntime, soundfile):
    module_file = Path(dependency.__file__).resolve()
    check(runtime in module_file.parents, f"{dependency.__name__} imported from {module_file}, expected under {runtime}")

check(callable(desktop_app.main), f"{desktop_app.__name__}.main is not callable")

# Qt must find its platform plugin and construct a real offscreen widget; a bare
# PySide6 import does not prove the Qt DLL/plugin payload survived relocation.
try:
    plugins_path = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
except AttributeError:
    plugins_path = Path(QLibraryInfo.location(QLibraryInfo.PluginsPath))
offscreen_plugins = list((plugins_path / "platforms").glob("qoffscreen.*"))
check(bool(offscreen_plugins), f"Qt offscreen platform plugin missing under {plugins_path / 'platforms'}")
try:
    qt_app = QApplication.instance() or QApplication([])
    check(qt_app.platformName() == "offscreen", f"Qt platform is {qt_app.platformName()!r}, expected 'offscreen'")
    window = QWidget()
    window.setWindowTitle("mtgacoach smoke")
    window.resize(320, 200)
    window.show()
    qt_app.processEvents()
    check(window.isVisible(), "offscreen QWidget did not become visible")
    window.close()
    qt_app.processEvents()
except Exception as exc:
    errors.append(f"offscreen Qt construction failed: {exc}")
    checks += 1

# The wheel carries the bridge DLL/BepInEx bundle as package data; the installer
# deploys them from there because raw checkout source is not shipped.
resources_dir = runtime / "Lib" / "site-packages" / "arenamcp" / "resources"
bridge_dll = resources_dir / "MtgaCoachBridge.dll"
bridge_manifest = resources_dir / "MtgaCoachBridge.build.json"
bepinex_bundle = resources_dir / "bepinex"
check(bridge_dll.is_file(), f"missing packaged bridge DLL at {bridge_dll}")
check(bridge_manifest.is_file(), f"missing packaged bridge manifest at {bridge_manifest}")
if bridge_dll.is_file() and bridge_manifest.is_file():
    bridge_meta = json.loads(bridge_manifest.read_text(encoding="utf-8-sig"))
    actual_hash = hashlib.sha256(bridge_dll.read_bytes()).hexdigest()
    check(actual_hash == bridge_meta.get("dll_sha256"), f"bridge DLL hash {actual_hash} != manifest {bridge_meta.get('dll_sha256')!r}")
check((bepinex_bundle / "BepInEx").is_dir(), f"missing packaged BepInEx bundle at {bepinex_bundle}")
check((bepinex_bundle / "winhttp.dll").is_file(), f"missing packaged BepInEx winhttp.dll at {bepinex_bundle}")
found_plugin = desktop_runtime.find_plugin_dll()
check(found_plugin is not None and Path(found_plugin).resolve() == bridge_dll.resolve(), f"find_plugin_dll()={found_plugin!r}, expected packaged {bridge_dll}")
found_bundle = desktop_runtime.find_bepinex_bundle()
check(found_bundle is not None and Path(found_bundle).resolve() == bepinex_bundle.resolve(), f"find_bepinex_bundle()={found_bundle!r}, expected packaged {bepinex_bundle}")
assets_dir = app_root / "assets"
if assets_dir.is_dir():
    check((assets_dir / "BepInEx").is_dir(), f"missing installer BepInEx assets at {assets_dir / 'BepInEx'}")
    check((assets_dir / "winhttp.dll").is_file(), f"missing installer winhttp.dll at {assets_dir / 'winhttp.dll'}")
    check((assets_dir / "doorstop_config.ini").is_file(), f"missing installer doorstop_config.ini at {assets_dir / 'doorstop_config.ini'}")

installed_version = version("arenamcp")
check(installed_version == arenamcp.__version__, f"metadata version {installed_version!r} != package version {arenamcp.__version__!r}")
dist_path = Path(distribution("arenamcp")._path).resolve()
check(runtime in dist_path.parents, f"arenamcp dist-info is outside the runtime: {dist_path}")
check(not (dist_path / "direct_url.json").exists(), f"direct_url.json shipped in {dist_path}")

os.environ.pop("MTGACOACH_APP_ROOT", None)
detected_root = Path(desktop_runtime.get_app_root()).resolve()
check(detected_root == app_root, f"get_app_root()={detected_root}, expected installed app root {app_root}")

found_python, python_source = desktop_runtime.find_python_executable()
check(found_python is not None and Path(found_python).resolve() == Path(sys.executable).resolve(), f"find_python_executable()={found_python!r}, expected bundled {sys.executable!r}")
check(python_source == "app_runtime", f"find_python_executable() source={python_source!r}, expected 'app_runtime'")

child_code = (
    "import sys, arenamcp, PySide6\n"
    "print(sys.executable)\n"
    "print(arenamcp.__file__)\n"
    "print(sys.flags.isolated)\n"
)
child = subprocess.run([sys.executable, "-X", "utf8", "-c", child_code], capture_output=True, text=True, timeout=120)
check(child.returncode == 0, f"bundled-interpreter subprocess failed ({child.returncode}): {child.stderr.strip()}")
if child.returncode == 0:
    lines = child.stdout.splitlines()
    check(len(lines) >= 3 and Path(lines[0]).resolve() == Path(sys.executable).resolve(), f"subprocess used unexpected interpreter: {child.stdout!r}")
    check(len(lines) >= 3 and str(runtime).lower() in lines[1].lower(), f"subprocess imported arenamcp outside runtime: {child.stdout!r}")
    check(len(lines) >= 3 and lines[2].strip() == "1", f"subprocess was not isolated: {child.stdout!r}")

# Exercise pythonw.exe with the real TTS worker entrypoint/protocol while a fake
# VoiceOutput prevents model downloads, audio devices, or network access.
sandbox = Path(os.environ["MTGACOACH_SMOKE_SANDBOX"]).resolve()
worker_harness = sandbox / "worker_smoke.py"
worker_code = """
import json
import sys
import types
from pathlib import Path

import arenamcp
import PySide6

print(json.dumps({
    "type": "probe",
    "executable": sys.executable,
    "prefix": sys.prefix,
    "arenamcp": arenamcp.__file__,
    "pyside6": PySide6.__file__,
    "isolated": sys.flags.isolated,
}))


class FakeVoice:
    def __init__(self, render_only=False):
        self.render_only = render_only
        self.speed = 1.5
        self.current_voice = ("af_heart", "Heart")

    def warmup(self):
        return None

    def render_to_wav_file(self, text):
        path = Path(__import__("os").environ["TEMP"]) / "smoke-ready.wav"
        path.write_bytes(b"RIFF")
        return str(path), 0.1

    def set_voice(self, voice_id):
        return None

    def set_speed(self, speed):
        self.speed = float(speed)


fake = types.ModuleType("arenamcp.tts")
fake.VoiceOutput = FakeVoice
sys.modules["arenamcp.tts"] = fake

from arenamcp.desktop import tts_worker

raise SystemExit(tts_worker.main())
"""
worker_harness.write_text(worker_code, encoding="utf-8")
pythonw_exe = runtime / "pythonw.exe"
for worker_label, worker_input in (("eof", ""), ("shutdown", '{"cmd":"shutdown"}\n')):
    try:
        worker_run = subprocess.run(
            [str(pythonw_exe), "-I", "-X", "utf8", str(worker_harness)],
            input=worker_input,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        errors.append(f"pythonw worker ({worker_label}) failed to start: {exc}")
        checks += 1
        continue
    check(worker_run.returncode == 0, f"pythonw worker ({worker_label}) exited {worker_run.returncode}: {worker_run.stderr.strip()}")
    worker_events = []
    for line in worker_run.stdout.splitlines():
        try:
            worker_events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    probe_events = [event for event in worker_events if event.get("type") == "probe"]
    check(bool(probe_events), f"pythonw worker ({worker_label}) did not emit interpreter probe: stdout={worker_run.stdout!r} stderr={worker_run.stderr!r}")
    if probe_events:
        probe = probe_events[0]
        check(Path(probe.get("executable", "")).resolve() == pythonw_exe.resolve(), f"pythonw worker ({worker_label}) executable={probe.get('executable')!r}, expected {pythonw_exe}")
        check(Path(probe.get("prefix", "")).resolve() == runtime, f"pythonw worker ({worker_label}) prefix={probe.get('prefix')!r}, expected {runtime}")
        check(runtime in Path(probe.get("arenamcp", "")).resolve().parents, f"pythonw worker ({worker_label}) imported arenamcp outside runtime: {probe.get('arenamcp')!r}")
        check(runtime in Path(probe.get("pyside6", "")).resolve().parents, f"pythonw worker ({worker_label}) imported PySide6 outside runtime: {probe.get('pyside6')!r}")
        check(probe.get("isolated") == 1, f"pythonw worker ({worker_label}) was not isolated: {probe.get('isolated')!r}")
    ready_events = [event for event in worker_events if event.get("type") == "ready"]
    check(bool(ready_events), f"pythonw worker ({worker_label}) did not emit ready: stdout={worker_run.stdout!r} stderr={worker_run.stderr!r}")
    if ready_events:
        check(ready_events[0].get("speed") == 1.5, f"pythonw worker ({worker_label}) speed={ready_events[0].get('speed')!r}, expected 1.5")

site_packages = runtime / "Lib" / "site-packages"
for pth in site_packages.glob("*.pth"):
    text = pth.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidate = Path(stripped)
        if candidate.is_absolute():
            check(runtime in candidate.resolve().parents or candidate.resolve() == runtime, f"{pth.name} points outside the runtime: {stripped}")
for metadata_file in site_packages.glob("*.dist-info/*"):
    if metadata_file.is_file() and metadata_file.suffix.lower() in {".json", ".txt", ".pth", ".cfg"}:
        text = metadata_file.read_text(encoding="utf-8", errors="replace")
        if checkout is not None:
            check(str(checkout).lower() not in text.lower(), f"{metadata_file} references the build checkout")

result = {
    "ok": not errors,
    "label": os.environ.get("MTGACOACH_SMOKE_LABEL", ""),
    "checks": checks,
    "errors": errors,
}
print(json.dumps(result))
raise SystemExit(0 if not errors else 1)
'@

[IO.File]::WriteAllText($smokePy, ($smokeCode + [Environment]::NewLine), (New-Object System.Text.UTF8Encoding($false)))

$smokeLog = Join-Path $sandbox ("smoke-" + ($Label -replace '[^A-Za-z0-9_.-]', '_') + ".log")
Push-Location -LiteralPath $AppRoot
try {
    & (Join-Path $RuntimeDir "python.exe") "-B" "-u" "-X" utf8 $smokePy *> $smokeLog
}
finally {
 Pop-Location
}
$smokeExitCode = $LASTEXITCODE

if (Test-Path -LiteralPath $smokeLog) {
 Get-Content -LiteralPath $smokeLog | ForEach-Object { Write-Host $_ }
}
if ($smokeExitCode -ne 0) {
 Fail "$Label isolated smoke failed with exit code ${smokeExitCode}; see output above."
}

Write-Host "[$Label] relocation verification passed."
}
finally {
 try {
   if ($hiddenSrc) {
     if (Test-Path -LiteralPath $originalSrc) {
       Fail "$originalSrc was recreated while its original contents were hidden at ${hiddenSrc}; refusing to overwrite it."
     }
     Rename-Item -LiteralPath $hiddenSrc -NewName "src"
     Write-Host "[$Label] restored checkout source at $originalSrc"
   }
 }
 finally {
   foreach ($key in @($savedEnv.Keys)) {
     if ($null -eq $savedEnv[$key]) {
       Remove-Item -LiteralPath ("Env:" + [string]$key) -ErrorAction SilentlyContinue
     }
     else {
       [Environment]::SetEnvironmentVariable($key, [string]$savedEnv[$key], "Process")
     }
   }
   Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
   Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
 }
}
