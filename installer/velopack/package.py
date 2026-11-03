"""Build a native Velopack distribution containing Coach's relocatable runtime."""

import hashlib
import json
import shutil
import subprocess
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
    )
    for path in RELEASES.iterdir():
        if path.is_file() and path.name != previous and path.name != "assets." + channel + ".json":
            shutil.copyfile(path, OUTPUT / path.name)
    if previous and not list(OUTPUT.glob("*-delta.nupkg")):
        raise RuntimeError("Previous release was available, but no delta was built")


if __name__ == "__main__":
    main()
