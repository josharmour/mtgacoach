"""Install a Mac source launcher without replacing the managed application."""

from __future__ import annotations

import argparse
import json
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path


def install(
    checkout: Path, python: Path, destination: Path, icon: Path | None = None, sign_identity: str = "-"
) -> Path:
    checkout = checkout.resolve()
    python = python.absolute()  # Keep the venv interpreter symlink, not its base interpreter.
    if not (checkout / "src/arenamcp/desktop/app.py").is_file() or not python.is_file():
        raise ValueError("Choose a Coach source checkout and its working Python environment.")
    identifier = "com.mtgacoach.source-dev"
    info = destination / "Contents/Info.plist"
    if destination.exists():
        if not info.is_file() or plistlib.loads(info.read_bytes()).get("CFBundleIdentifier") != identifier:
            raise ValueError(f"Refusing to replace another application: {destination}")
    binary = destination / "Contents/MacOS/CoachDev"
    binary.parent.mkdir(parents=True, exist_ok=True)
    resources = destination / "Contents/Resources"
    resources.mkdir(exist_ok=True)
    properties = {
        "CFBundleName": "MTGA Coach (Dev)",
        "CFBundleDisplayName": "MTGA Coach (Dev)",
        "CFBundleIdentifier": identifier,
        "CFBundleExecutable": binary.name,
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1",
        "NSHighResolutionCapable": True,
        # This parent has no UI. Qt in the child owns the window and Dock icon;
        # treating the waiting C supervisor as a GUI app leaves its icon bouncing.
        "LSUIElement": True,
        "NSScreenCaptureUsageDescription": "Include the Arena window in screenshots you request.",
    }
    if icon:
        if icon.resolve() != (resources / "Coach.icns").resolve():
            shutil.copyfile(icon, resources / "Coach.icns")
    if (resources / "Coach.icns").is_file():
        properties["CFBundleIconFile"] = "Coach.icns"
    info.write_bytes(plistlib.dumps(properties))
    source = Path(__file__).resolve().parents[1] / "installer/mac-dev/launcher.c"
    with tempfile.TemporaryDirectory(prefix="coach-dev-launcher-") as temp:
        built = Path(temp) / binary.name
        subprocess.run(
            [
                "/usr/bin/clang",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-O2",
                source,
                "-DCOACH_REPO=" + json.dumps(str(checkout)),
                "-DCOACH_PYTHON=" + json.dumps(str(python)),
                "-DCOACH_APP=" + json.dumps(str(destination.absolute())),
                "-o",
                built,
            ],
            check=True,
        )
        shutil.copy2(built, binary.with_suffix(".new"))
        binary.with_suffix(".new").replace(binary)
    subprocess.run(
        ["/usr/bin/codesign", "--force", "--sign", sign_identity, "--identifier", identifier, destination],
        check=True,
    )
    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", destination], check=True)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--destination", type=Path, default=Path.home() / "Applications/MTGA Coach (Dev).app")
    parser.add_argument("--icon", type=Path)
    parser.add_argument(
        "--sign-identity",
        default="-",
        help="Apple Development identity for stable local screen-capture permission",
    )
    args = parser.parse_args()
    print(install(args.checkout, args.python, args.destination, args.icon, args.sign_identity))


if __name__ == "__main__":
    main()
