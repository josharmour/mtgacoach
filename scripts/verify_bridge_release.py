"""Fail release packaging when the staged Windows bridge does not match its source.

The bridge is compiled on a Windows build host with Arena's managed assemblies.
Hosted packaging runners verify that build's manifest; they do not redistribute
Arena's reference assemblies or claim to compile without them.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PROJECT = Path("bepinex-plugin/MtgaCoachBridge")
DLL = Path("src/arenamcp/resources/MtgaCoachBridge.dll")
MANIFEST = Path("src/arenamcp/resources/MtgaCoachBridge.build.json")
REFERENCES = frozenset(
    {
        "Assembly-CSharp.dll",
        "Core.dll",
        "SharedClientCore.dll",
        "Wizards.MDN.GreProtobuf.dll",
        "UnityEngine.dll",
        "UnityEngine.CoreModule.dll",
        "UnityEngine.PhysicsModule.dll",
        "Newtonsoft.Json.dll",
        "Google.Protobuf.dll",
        "BepInEx.dll",
        "0Harmony.dll",
    }
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(
            path.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for path in sorted((root / PROJECT).rglob("*"))
        if path.suffix in {".cs", ".csproj", ".props", ".targets"}
        and not {"bin", "obj"}.intersection(path.relative_to(root / PROJECT).parts)
    }


def verify(root: Path) -> dict:
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        raise ValueError(
            "Bridge build manifest missing: rebuild from current source using Arena's managed references"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported bridge build manifest schema")
    actual_sources = source_hashes(root)
    if not actual_sources or manifest.get("source_sha256") != actual_sources:
        raise ValueError("Bridge source changed since the recorded build; rebuild the Windows bridge")
    if manifest.get("dll_sha256") != sha256(root / DLL):
        raise ValueError("Packaged bridge DLL does not match the recorded build")
    if (
        not manifest.get("compiler")
        or not manifest.get("built_at_utc")
        or not manifest.get("reference_sha256")
    ):
        raise ValueError("Bridge build provenance is incomplete")
    references = manifest["reference_sha256"]
    if not isinstance(references, dict) or set(references) != REFERENCES:
        raise ValueError("Bridge build reference inventory is incomplete")
    if any(
        not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
        for value in references.values()
    ):
        raise ValueError("Bridge build reference hashes are invalid")
    return manifest


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    if sys.argv[1:] == ["--source-hashes"]:
        print(json.dumps(source_hashes(root), sort_keys=True))
        return 0
    if sys.argv[1:]:
        print("Usage: verify_bridge_release.py [--source-hashes]", file=sys.stderr)
        return 2
    try:
        manifest = verify(root)
    except (OSError, ValueError) as error:
        print(f"Bridge release verification failed: {error}", file=sys.stderr)
        return 1
    print(
        f"Verified Windows bridge SHA256 {manifest['dll_sha256']} against {len(manifest['source_sha256'])} source files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
