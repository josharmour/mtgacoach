"""Build a native Velopack distribution containing Coach's relocatable runtime."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / "dist" / "managed"
RELEASES = ROOT / "dist" / "feed"
OUTPUT = ROOT / "dist" / "publish"
CACHE = Path(os.environ.get("MTGACOACH_BUILD_CACHE", ROOT / ".build-cache"))
COMMENTARY_CORE_COMMIT = "20d87e8c5034f247f551bef326a4ac4d7983eff9"


def install_commentary_core(exe):
    # Core only: speech stays with Coach's Kokoro or hosted Chatterbox path.
    run(
        "uv",
        "pip",
        "install",
        "--break-system-packages",
        "--python",
        exe,
        f"https://github.com/josharmour/arenaonair/archive/{COMMENTARY_CORE_COMMIT}.zip",
    )


def verify_commentary_core(exe):
    """Reject packages whose generated commentary depends on a developer checkout."""
    with tempfile.TemporaryDirectory(prefix="Coach commentary smoke ") as temp:
        run(
            exe,
            "-I",
            "-c",
            """
import pathlib, sys, time
import arenaonair
from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.headless import HeadlessBooth
root = pathlib.Path(arenaonair.__file__).resolve()
if not root.is_relative_to(pathlib.Path(sys.prefix).resolve()):
    raise SystemExit("Commentary core resolved outside packaged Python: " + str(root))
HeadlessBooth(CommentaryConfig(mode="commentary", generative=True), lambda *args: None, time.monotonic)
print("Packaged generated commentary core initialized successfully")
""",
            cwd=temp,
            timeout=30,
        )


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def cache_key(rid, paths, extra=""):
    digest = hashlib.sha256((rid + extra).encode())
    for path in paths:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def save_runtime_cache(runtime, target):
    staging = target.with_name(target.name + ".tmp")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(runtime, staging, symlinks=True)
    staging.replace(target)


def install_current_wheel(exe, wheel, rid):
    # The package version can stay unchanged across source commits. Always
    # replace arenamcp, including its RECORD-tracked deleted files.
    run(
        "uv",
        "pip",
        "install",
        "--break-system-packages",
        "--python",
        exe,
        "--reinstall-package",
        "arenamcp",
        "--no-deps",
        wheel,
    )
    for path in (
        exe.parent.parent.rglob("direct_url.json")
        if not rid.startswith("win")
        else exe.parent.rglob("direct_url.json")
    ):
        path.unlink()
    if rid.startswith("win"):
        for path in (exe.parent / "Scripts").glob("*.exe"):
            path.unlink()


def verify_installed_wheel(exe, wheel):
    """A cached runtime must contain this wheel's code and resources."""
    run(
        exe,
        "-I",
        "-c",
        """
import importlib.util, pathlib, sys, zipfile
root = pathlib.Path(importlib.util.find_spec("arenamcp").submodule_search_locations[0])
with zipfile.ZipFile(sys.argv[1]) as wheel:
    for name in wheel.namelist():
        if name.startswith("arenamcp/") and not name.endswith("/"):
            actual = root / name.removeprefix("arenamcp/")
            if actual.read_bytes() != wheel.read(name):
                raise SystemExit("Packaged runtime does not match current wheel: " + name)
""",
        wheel,
    )


def build_mac_bridge(rid):
    """Compile and qualify the bridge shipped beside the managed Mac runtime."""
    arch = "arm64" if rid == "osx-arm64" else "x86_64"
    source = ROOT / "spikes/mac-il2cpp/probe.cpp"
    library = STAGE / "native/libmtgacoach_probe.dylib"
    library.parent.mkdir(parents=True, exist_ok=True)
    compiler = subprocess.check_output(["clang++", "--version"], text=True)
    sdk = subprocess.check_output(["xcrun", "--show-sdk-version"], text=True)
    key = cache_key(
        rid, [source, source.parent / "tests/probe_batch_test.cpp", Path(__file__)], compiler + sdk
    )
    cached = CACHE / "native" / key / library.name
    if cached.is_file():
        shutil.copy2(cached, library)
        run("codesign", "--verify", "--strict", library)
        print("Reused qualified Mac bridge", flush=True)
        return
    harness = ROOT / "dist/probe_batch_test"
    run(
        "clang++",
        "-std=c++17",
        "-O1",
        "-arch",
        arch,
        source.parent / "tests/probe_batch_test.cpp",
        "-o",
        harness,
    )
    run(harness)
    run(
        "clang++",
        "-std=c++17",
        "-O1",
        "-shared",
        "-fPIC",
        "-fvisibility=hidden",
        "-arch",
        arch,
        source,
        "-o",
        library,
    )
    run("codesign", "--force", "--sign", "-", library)
    run("codesign", "--verify", "--strict", library)
    cached.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(library, cached)


def prepare_icon(rid):
    if rid.startswith("win"):
        return ROOT / "mtga_coach.ico"
    png = ROOT / "assets/icon.png"
    if rid.startswith("linux"):
        return png
    icons = ROOT / "dist/Coach.iconset"
    icons.mkdir(exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        for scale, suffix in ((1, ""), (2, "@2x")):
            run(
                "sips",
                "-z",
                size * scale,
                size * scale,
                png,
                "--out",
                icons / f"icon_{size}x{size}{suffix}.png",
                stdout=subprocess.DEVNULL,
            )
    icon = ROOT / "dist/Coach.icns"
    run("iconutil", "-c", "icns", icons, "-o", icon)
    return icon


def previous_release(lane, channel):
    # A first installation has no previous package on which to base a delta.
    feed = f"coach-{lane}-feed"
    gh = ["gh", "release", "view", feed, "--json", "assets"]
    response = subprocess.run(gh, capture_output=True, text=True)
    if response.returncode:
        if "release not found" in response.stderr.lower() or "not found" in response.stderr.lower():
            return
        raise RuntimeError(response.stderr)
    names = {asset["name"] for asset in json.loads(response.stdout)["assets"]}
    manifest = f"releases.{channel}.json"
    if manifest not in names:
        return
    run("gh", "release", "download", feed, "-p", manifest, "-D", RELEASES)
    assets = json.loads((RELEASES / manifest).read_text(encoding="utf-8-sig"))["Assets"]
    full = next((a for a in assets if a["Type"] == "Full"), None)
    if full is None or full["FileName"] not in names:
        raise RuntimeError(f"Missing previous full package for {channel}")
    run("gh", "release", "download", feed, "-p", full["FileName"], "-D", RELEASES)
    return full["FileName"]


def verify_launcher(rid):
    """Launch the delivered host outside the checkout without starting Arena or a desktop session."""
    with tempfile.TemporaryDirectory(prefix="Coach package smoke ") as temp:
        temp = Path(temp)
        if rid.startswith("linux"):
            image = next(RELEASES.glob("*.AppImage"))
            image.chmod(0o755)
            run(image, "--appimage-extract", cwd=temp, stdout=subprocess.DEVNULL)
            launcher = temp / "squashfs-root/AppRun"
        elif rid.startswith("osx"):
            run("ditto", "-x", "-k", next(RELEASES.glob("*Portable.zip")), temp)
            launcher = next(temp.glob("*.app/Contents/MacOS/MTGACoach"))
            bridge = launcher.parent / "native/libmtgacoach_probe.dylib"
            if bridge.read_bytes() != (STAGE / "native/libmtgacoach_probe.dylib").read_bytes():
                raise RuntimeError("Packaged Mac bridge differs from the qualified build")
            run("codesign", "--verify", "--strict", bridge)
        else:
            with zipfile.ZipFile(next(RELEASES.glob("*Portable.zip"))) as archive:
                archive.extractall(temp)
            launcher = next(temp.rglob("MTGACoach.exe"))
            if (launcher.parent / "mtga_coach.ico").read_bytes() != (ROOT / "mtga_coach.ico").read_bytes():
                raise RuntimeError("Packaged desktop window icon differs from the Coach icon")
        run(launcher, "--version", cwd=temp, timeout=90)


def make_per_user_mac_installer(pkg: Path) -> None:
    """Install under ~/Applications so Coach can replace its bundle as the current user."""
    with tempfile.TemporaryDirectory(dir=pkg.parent) as temp:
        expanded = Path(temp) / "expanded"
        run("pkgutil", "--expand", pkg, expanded)
        distribution = expanded / "Distribution"
        xml = ET.parse(distribution)
        domains = xml.find("domains")
        if domains is None or domains.get("enable_currentUserHome") != "true":
            raise RuntimeError("Velopack installer does not support per-user installs")
        domains.set("enable_anywhere", "false")
        domains.set("enable_localSystem", "false")
        xml.write(distribution, encoding="utf-8", xml_declaration=True)
        # The stock component requests root privileges and its postinstall uses
        # sudo. Both are unnecessary for a package confined to the user domain.
        (component,) = expanded.glob("*.pkg/PackageInfo")
        info = ET.parse(component)
        info.getroot().set("auth", "none")
        bundle_paths = (Path(b.attrib["path"]) for b in info.findall("bundle"))
        apps = [path for path in bundle_paths if path.parent == Path(".") and path.suffix == ".app"]
        if len(apps) != 1:
            raise RuntimeError(f"Expected one top-level Velopack app, found {apps}")
        app_path = apps[0]
        info.write(component, encoding="utf-8", xml_declaration=True)
        postinstall = component.parent / "Scripts/postinstall"
        postinstall.write_text(
            '#!/bin/sh\nif [ -z "${COMMAND_LINE_INSTALL:-}" ]; then\n'
            f'    VELOPACK_FIRSTRUN=1 /usr/bin/open "$2"/{shlex.quote(app_path.name)}\nfi\nexit 0\n'
        )
        output = Path(temp) / pkg.name
        run("pkgutil", "--flatten", expanded, output)
        choices = subprocess.check_output(["/usr/sbin/installer", "-dominfo", "-pkg", str(output)], text=True)
        if choices.split() != ["CurrentUserHomeDirectory"]:
            raise RuntimeError(f"Unexpected Mac installer destinations: {choices}")
        output.replace(pkg)


def main():
    rid, lane, version = sys.argv[1:4]
    channel = f"{lane}-{rid}"
    if lane not in ("master", "stable"):
        raise ValueError(lane)
    if rid not in ("win-x64", "linux-x64", "osx-arm64"):
        raise ValueError(rid)
    for folder in (STAGE, RELEASES, OUTPUT):
        if folder.exists():
            shutil.rmtree(folder)
    RELEASES.mkdir(parents=True, exist_ok=True)
    STAGE.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    wheel = next((ROOT / "dist").glob("arenamcp-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        metadata = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        if BytesParser().parsebytes(archive.read(metadata))["Version"] != version:
            raise RuntimeError(
                "App and package versions differ; stamp the release version before building the wheel"
            )
    run(
        "dotnet",
        "publish",
        ROOT / "installer/velopack/MTGACoach.csproj",
        "-c",
        "Release",
        "-r",
        rid,
        "-o",
        STAGE,
    )
    runtime = STAGE / "runtime"
    key = cache_key(
        rid, [ROOT / "pyproject.toml", ROOT / "installer/build-runtime.ps1", Path(__file__)], sys.version
    )
    cached_runtime = CACHE / "runtime" / key
    reused_runtime = cached_runtime.is_dir()
    if reused_runtime:
        shutil.copytree(cached_runtime, runtime, symlinks=True)
        print("Reused qualified Python runtime and dependencies", flush=True)
    if rid.startswith("osx"):
        build_mac_bridge(rid)
    if rid.startswith("win"):
        shutil.copyfile(ROOT / "mtga_coach.ico", STAGE / "mtga_coach.ico")
        if not reused_runtime:
            run(
                "pwsh",
                "-NoProfile",
                "-File",
                ROOT / "installer/build-runtime.ps1",
                "-WheelPath",
                wheel,
                "-PythonHome",
                sys.base_prefix,
                "-RuntimeDir",
                runtime,
                "-RepoRoot",
                ROOT,
            )
        exe = runtime / "python.exe"
    else:
        if not reused_runtime:
            target = ROOT / "dist" / "cpython"
            run("uv", "python", "install", "3.11", "--no-bin", "--install-dir", target)
            portable = next(p for p in target.glob("cpython-*") if p.is_dir())
            shutil.copytree(portable, runtime, symlinks=True)
            run(
                "uv",
                "pip",
                "install",
                "--break-system-packages",
                "--python",
                runtime / "bin/python3",
                f"{wheel}[full]",
            )
        exe = runtime / "bin/python3"
    if not reused_runtime:
        install_commentary_core(exe)
    install_current_wheel(exe, wheel, rid)
    verify_installed_wheel(exe, wheel)
    verify_commentary_core(exe)
    if rid.startswith("win"):
        run(
            "pwsh",
            "-NoProfile",
            "-File",
            ROOT / "installer/verify-runtime-relocation.ps1",
            "-RuntimeDir",
            runtime,
            "-AppRoot",
            STAGE,
            "-CheckoutRoot",
            ROOT,
            "-Relocate",
            "-Label",
            "velopack",
        )
    if not reused_runtime:
        cached_runtime.parent.mkdir(parents=True, exist_ok=True)
        save_runtime_cache(runtime, cached_runtime)
    # Compare runtime dependencies and native bridge inputs across releases.
    deps = subprocess.check_output(
        [
            str(exe),
            "-I",
            "-c",
            "import importlib.metadata as m;print(sorted((d.metadata['Name'].lower(),d.version) for d in m.distributions() if d.metadata['Name'].lower() != 'arenamcp'))",
        ],
        text=True,
    )
    signature = hashlib.sha256(deps.encode())
    signature.update(COMMENTARY_CORE_COMMIT.encode())
    if rid.startswith("osx"):
        # Native code updates require the explicit bridge/runtime update path.
        signature.update((ROOT / "spikes/mac-il2cpp/probe.cpp").read_bytes())
    signature.update(subprocess.check_output([str(exe), "-I", "-c", "import sys;print(sys.version)"]))
    for path in [
        ROOT / "installer/build-runtime.ps1",
        *sorted((ROOT / "src/arenamcp/resources").rglob("*.dll")),
    ]:
        signature.update(path.read_bytes())
    compat = signature.hexdigest()
    (STAGE / "coach-release.json").write_text(
        json.dumps(
            dict(
                channel=channel,
                version=version,
                compatibility=compat,
                commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                commentary_core_commit=COMMENTARY_CORE_COMMIT,
            )
        )
    )
    notes = ROOT / "dist/compatibility.md"
    notes.write_text("coach-runtime-" + compat + "\n")
    main_exe = "MTGACoach.exe" if rid.startswith("win") else "MTGACoach"
    run(exe, "-I", "-m", "arenamcp.desktop", "--version", cwd=STAGE)
    previous = previous_release(lane, channel)
    run(
        "vpk",
        "pack",
        "--packId",
        "MTGACoach" + lane.title(),
        "--packTitle",
        "MTGA Coach" + (" (master)" if lane == "master" else ""),
        "--packVersion",
        version,
        "--mainExe",
        main_exe,
        "--channel",
        channel,
        "--runtime",
        rid,
        "--packDir",
        STAGE,
        "--outputDir",
        RELEASES,
        "--icon",
        prepare_icon(rid),
        "--releaseNotes",
        notes,
        *(["--instWelcome", ROOT / "installer/velopack/mac-welcome.txt"] if rid.startswith("osx-") else []),
    )
    if rid.startswith("osx-"):
        make_per_user_mac_installer(next(RELEASES.glob("*-Setup.pkg")))
    verify_launcher(rid)
    # Only the bootstrap, packages and feed index are needed by clients.
    for path in RELEASES.iterdir():
        if path.name != previous and (
            path.suffix in {".nupkg", ".AppImage", ".exe", ".pkg"} or path.name == f"releases.{channel}.json"
        ):
            shutil.copyfile(path, OUTPUT / path.name)
    if previous and not list(OUTPUT.glob("*-delta.nupkg")):
        raise RuntimeError("Previous release was available, but no delta was built")


if __name__ == "__main__":
    main()
