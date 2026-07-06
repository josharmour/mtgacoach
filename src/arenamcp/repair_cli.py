"""`mtgacoach-repair`: the Repair tab without the GUI.

Audit blocker #2: when the venv/Qt is broken, the desktop app can't paint
the Repair tab that would fix it. This entry point needs only the stdlib
plus this package — it runs the same engine and prints the checklist.
"""

from __future__ import annotations

import sys


def main() -> int:
    from arenamcp.repair_engine import RepairEngine, set_license_key

    if sys.argv[1:2] == ["--update"]:
        # Audit #25: the old "Quick update" silently no-op'd into Program
        # Files. Under the pip model an update is just pip against the
        # interpreter that runs the app.
        import subprocess

        print("Updating mtgacoach…")
        rc = subprocess.call(
            [sys.executable, "-m", "pip", "install", "--upgrade", "arenamcp"]
        )
        if rc != 0:
            print("Update failed — see pip output above.")
            return rc
        print("Updated. Re-checking the install…\n")
        # fall through to the checklist so the new plugin DLL deploys too

    if len(sys.argv) >= 3 and sys.argv[1] == "--set-license":
        result = set_license_key(sys.argv[2])
        print(f"[{result.status}] {result.label}: {result.detail}")
        return 0 if result.status == "ok" else 1

    print("mtgacoach repair — checking everything…\n")
    report = RepairEngine().run(progress=lambda n: print(f"  … {n.replace('_', ' ')}"))
    print()
    glyphs = {"ok": "✓", "fixed": "✦", "action_needed": "⚠", "error": "✗"}
    for r in report.results:
        print(f" {glyphs.get(r.status, '•')} {r.label}: {r.detail}")
        if r.action_hint:
            print(f"     → {r.action_hint}")
    print(f"\n{report.summary()}")
    if any(r.key == "license" and r.status == "action_needed" for r in report.results):
        print("Set your license key with: mtgacoach-repair --set-license sk-...")
    return 0 if report.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
