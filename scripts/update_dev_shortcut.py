import os
import subprocess
from pathlib import Path
import win32com.client


def _python_works(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        res = subprocess.run(
            [str(path), "-c", "import PySide6"],
            capture_output=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return res.returncode == 0
    except Exception:
        return False


def find_best_pythonw() -> str:
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    candidates = []
    if local_appdata:
        candidates.append(Path(local_appdata) / "mtgacoach" / "venv" / "Scripts" / "pythonw.exe")
    candidates.extend([
        Path(r"Y:\mtgacoach\.venv\Scripts\pythonw.exe"),
        Path(r"C:\Program Files\mtgacoach\runtime\Scripts\pythonw.exe"),
    ])
    for c in candidates:
        if _python_works(c):
            return str(c)

    import sys
    py_dir = Path(sys.executable).parent
    sys_pythonw = py_dir / "pythonw.exe"
    if _python_works(sys_pythonw):
        return str(sys_pythonw)

    return str(candidates[0])


desktop = Path(os.path.expanduser("~")) / "Desktop"
lnk_path = str(desktop / "mtgacoach (Dev).lnk")
repo_root = r"Y:\mtgacoach"
local_pythonw = find_best_pythonw()
launch_script = str(Path(repo_root) / "scripts" / "launch_desktop.py")
icon_path = str(Path(repo_root) / "mtga_coach.ico")

shell = win32com.client.Dispatch("WScript.Shell")
shortcut = shell.CreateShortCut(lnk_path)
shortcut.TargetPath = local_pythonw
shortcut.Arguments = f'"{launch_script}"'
shortcut.WorkingDirectory = repo_root
shortcut.IconLocation = icon_path
shortcut.Description = "mtgacoach Live Development"
shortcut.save()
print("Updated dev shortcut target to:", local_pythonw)
print("Arguments:", f'"{launch_script}"')

