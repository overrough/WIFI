' ============================================================
' JARVIS — Silent Launcher (no terminal window visible)
' Use this for Windows startup so Jarvis runs in background.
'
' May 10 2026 — fix: backend was silently dying because pythonw.exe
' swallowed all stderr. Now we use python.exe + cmd /c with redirect
' to a logfile so any backend crash is visible at logs\backend.log.
' Sleep raised from 4 → 12 seconds for cold-boot reliability (mic
' subsystem and disk are slow right after Windows wakes up).
' ============================================================

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

' Get the script's own directory
ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
ProjectDir = FSO.GetParentFolderName(ScriptDir)
LogDir     = ProjectDir & "\logs"

' Make sure the logs folder exists
If Not FSO.FolderExists(LogDir) Then
    FSO.CreateFolder(LogDir)
End If

' --- Start Backend API ---------------------------------------------
' Use python.exe (not pythonw.exe) wrapped in a hidden cmd window so
' that stderr and stdout BOTH land in logs\backend.log. If Jarvis
' isn't responding after boot, that file is the first place to look.
BackendPython = """" & ProjectDir & "\backend\.venv\Scripts\python.exe"""
BackendLog    = """" & LogDir & "\backend.log"""
BackendCmd = "cmd /c """ & BackendPython & " -m uvicorn main:app --host 127.0.0.1 --port 8000 >" & BackendLog & " 2>&1"""
WshShell.CurrentDirectory = ProjectDir & "\backend"
WshShell.Run BackendCmd, 0, False

' Wait 12 seconds for backend to fully come up — Whisper/embedding
' models cold-load the first time and the old 4 s window was a race.
WScript.Sleep 12000

' --- Start Voice Agent ---------------------------------------------
' HUD is the UI, so voice agent stays hidden via pythonw.
' Same pattern — log to file in case the voice agent crashes early.
VoicePython = """" & ProjectDir & "\backend\.venv\Scripts\pythonw.exe"""
VoiceLog    = """" & LogDir & "\voice_agent.log"""
VoiceCmd = "cmd /c """ & VoicePython & " main.py >" & VoiceLog & " 2>&1"""
WshShell.CurrentDirectory = ProjectDir & "\voice_agent"
WshShell.Run VoiceCmd, 0, False
