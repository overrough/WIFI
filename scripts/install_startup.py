"""
Install Jarvis to Windows Startup — run once.

This script creates a shortcut in the Windows Startup folder
so Jarvis launches automatically every time you log in.

Usage:
    python install_startup.py          # install
    python install_startup.py --remove # uninstall

What it does:
  1. Creates a shortcut to launch_jarvis.vbs in shell:startup
  2. The VBS script starts backend + voice agent silently
  3. On every login, Jarvis is ready — just say "Jarvis"
"""

import argparse
import os
import sys
from pathlib import Path


def get_startup_folder() -> Path:
    """Get the Windows Startup folder path."""
    startup = Path(os.environ.get(
        "APPDATA", os.path.expanduser("~\\AppData\\Roaming")
    )) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return startup


def get_project_dir() -> Path:
    """Get the project root directory."""
    return Path(__file__).resolve().parent.parent


def install():
    """Create a Jarvis startup shortcut."""
    startup = get_startup_folder()
    project = get_project_dir()
    vbs_path = project / "scripts" / "launch_jarvis.vbs"

    if not vbs_path.exists():
        print(f"❌ Launcher not found: {vbs_path}")
        sys.exit(1)

    shortcut_path = startup / "Jarvis.lnk"

    try:
        # Use PowerShell to create a proper .lnk shortcut
        import subprocess
        ps_script = f'''
$WShell = New-Object -ComObject WScript.Shell
$Shortcut = $WShell.CreateShortcut("{shortcut_path}")
$Shortcut.TargetPath = "wscript.exe"
$Shortcut.Arguments = '"{vbs_path}"'
$Shortcut.WorkingDirectory = "{project}"
$Shortcut.Description = "Jarvis AI Voice Assistant"
$Shortcut.IconLocation = "shell32.dll,130"
$Shortcut.Save()
'''
        subprocess.run(
            ["powershell", "-Command", ps_script],
            check=True,
            capture_output=True,
        )

        print("✅ Jarvis installed to Windows Startup!")
        print(f"   Shortcut: {shortcut_path}")
        print(f"   Launcher: {vbs_path}")
        print()
        print("   Jarvis will now start automatically when you log in.")
        print("   Say 'Jarvis' or double-clap to activate.")
        print()
        print("   To remove: python install_startup.py --remove")

    except Exception as exc:
        print(f"❌ Failed to create shortcut: {exc}")
        print()
        print("   Manual alternative:")
        print(f"   1. Press Win+R → type 'shell:startup' → Enter")
        print(f"   2. Create a shortcut to: {vbs_path}")
        sys.exit(1)


def remove():
    """Remove the Jarvis startup shortcut."""
    startup = get_startup_folder()
    shortcut_path = startup / "Jarvis.lnk"

    if shortcut_path.exists():
        shortcut_path.unlink()
        print("✅ Jarvis removed from Windows Startup.")
        print("   It will no longer start automatically on login.")
    else:
        print("ℹ️  No Jarvis startup shortcut found. Nothing to remove.")


def main():
    parser = argparse.ArgumentParser(
        description="Install/remove Jarvis from Windows Startup"
    )
    parser.add_argument(
        "--remove", action="store_true",
        help="Remove Jarvis from startup instead of installing"
    )
    args = parser.parse_args()

    if sys.platform != "win32":
        print("⚠️  This script is for Windows only.")
        sys.exit(1)

    if args.remove:
        remove()
    else:
        install()


if __name__ == "__main__":
    main()
