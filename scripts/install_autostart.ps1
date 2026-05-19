# =====================================================================
#  Friday — Auto-start installer
#
#  Drops a shortcut to launch_friday.vbs into the Windows Startup folder
#  so Friday boots up automatically when Sir logs in. Idempotent —
#  re-running it overwrites the shortcut with a fresh copy.
#
#  USAGE (PowerShell, no admin needed — it writes to per-user Startup):
#      .\scripts\install_autostart.ps1
#
#  TO UNINSTALL:
#      .\scripts\install_autostart.ps1 -Uninstall
# =====================================================================

[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

# Resolve the project root from this script's location.
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$Launcher   = Join-Path $ScriptDir 'launch_friday.vbs'

$StartupFolder = [Environment]::GetFolderPath('Startup')
$ShortcutPath  = Join-Path $StartupFolder 'Friday.lnk'

if ($Uninstall) {
    if (Test-Path $ShortcutPath) {
        Remove-Item $ShortcutPath -Force
        Write-Host "Removed Startup shortcut: $ShortcutPath" -ForegroundColor Green
    } else {
        Write-Host "No Friday shortcut found in Startup folder." -ForegroundColor Yellow
    }
    exit 0
}

if (-not (Test-Path $Launcher)) {
    Write-Error "Launcher not found at $Launcher. Make sure scripts/launch_friday.vbs exists."
    exit 1
}

# Create the .lnk via WScript.Shell COM object — the same mechanism
# Windows itself uses for shortcuts, so no dependencies.
$wsh      = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($ShortcutPath)
$shortcut.TargetPath       = 'wscript.exe'
$shortcut.Arguments        = "//B //Nologo `"$Launcher`""
$shortcut.WorkingDirectory = $ProjectDir
$shortcut.Description      = 'Friday — start voice assistant in the background'
$shortcut.WindowStyle      = 7   # minimised, no flash
$shortcut.IconLocation     = "$ProjectDir\frontend\public\favicon.ico,0"
$shortcut.Save()

Write-Host "Installed Friday autostart:" -ForegroundColor Green
Write-Host "  shortcut : $ShortcutPath"
Write-Host "  launcher : $Launcher"
Write-Host ""
Write-Host "Friday will now start automatically when you log in." -ForegroundColor Cyan
Write-Host "Run with -Uninstall to remove."
