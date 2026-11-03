"""Build a native Velopack distribution containing Coach's relocatable runtime."""

import hashlib
import json
import shlex
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / "dist" / "managed"
RELEASES = ROOT / "dist" / "feed"
OUTPUT = ROOT / "dist" / "publish"


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


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
        else:
            with zipfile.ZipFile(next(RELEASES.glob("*Portable.zip"))) as archive:
                archive.extractall(temp)
            launcher = next(temp.rglob("MTGACoach.exe"))
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
        bundle = info.find("bundle")
        app_path = Path(bundle.attrib["path"]) if bundle is not None else Path()
        if app_path.parent != Path(".") or app_path.suffix != ".app":
            raise RuntimeError("Unexpected Velopack app bundle path")
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
    for folder in (STAGE, RELEASES, OUTPUT):
        if folder.exists():
            shutil.rmtree(folder)
    RELEASES.mkdir(parents=True, exist_ok=True)
    STAGE.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    wheel = next((ROOT / "dist").glob("arenamcp-*.whl"))
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
    if rid.startswith("win"):
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
    else:
        target = ROOT / "dist" / "cpython"
        run("uv", "python", "install", "3.11", "--no-bin", "--install-dir", target)
        portable = next(p for p in target.glob("cpython-*") if p.is_dir())
        shutil.copytree(portable, runtime, symlinks=True)
        exe = runtime / "bin/python3"
        run("uv", "pip", "install", "--break-system-packages", "--python", exe, f"{wheel}[full]")
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
                compatibility=compat,
                commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
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
        if path.name != previous and (path.suffix in {".nupkg", ".AppImage", ".exe", ".pkg"} or path.name == f"releases.{channel}.json"):
            shutil.copyfile(path, OUTPUT / path.name)
    if previous and not list(OUTPUT.glob("*-delta.nupkg")):
        raise RuntimeError("Previous release was available, but no delta was built")


if __name__ == "__main__":
    main()
