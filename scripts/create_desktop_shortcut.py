"""
Create a desktop shortcut for Jarvis — one click to launch.

Usage:
    python create_desktop_shortcut.py

Creates a "Jarvis" shortcut on your Desktop that launches the
voice assistant with a single double-click.
"""

import os
import subprocess
import sys
from pathlib import Path


def main():
    if sys.platform != "win32":
        print("⚠️  This script is for Windows only.")
        sys.exit(1)

    project_dir = Path(__file__).resolve().parent.parent
    bat_path = project_dir / "scripts" / "launch_jarvis.bat"
    desktop = Path(os.environ.get(
        "USERPROFILE", os.path.expanduser("~")
    )) / "Desktop"

    shortcut_path = desktop / "Jarvis.lnk"

    if not bat_path.exists():
        print(f"❌ Launcher not found: {bat_path}")
        sys.exit(1)

    try:
        ps_script = f'''
$WShell = New-Object -ComObject WScript.Shell
$Shortcut = $WShell.CreateShortcut("{shortcut_path}")
$Shortcut.TargetPath = "{bat_path}"
$Shortcut.WorkingDirectory = "{project_dir}\\scripts"
$Shortcut.Description = "Launch Jarvis AI Voice Assistant"
$Shortcut.IconLocation = "shell32.dll,130"
$Shortcut.Save()
'''
        subprocess.run(
            ["powershell", "-Command", ps_script],
            check=True,
            capture_output=True,
        )

        print("✅ Desktop shortcut created!")
        print(f"   Location: {shortcut_path}")
        print()
        print("   Double-click 'Jarvis' on your desktop to launch.")
        print("   Say 'Jarvis' or double-clap to activate.")

    except Exception as exc:
        print(f"❌ Failed to create shortcut: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
