import os
from pathlib import Path
import win32com.client

desktop = Path(os.path.expanduser("~")) / "Desktop"
lnk_path = str(desktop / "mtgacoach (Dev).lnk")
repo_root = r"Y:\mtgacoach"
local_pythonw = r"C:\Program Files\mtgacoach\runtime\Scripts\pythonw.exe"
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
