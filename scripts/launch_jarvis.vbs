' ============================================================
' JARVIS — Silent Launcher (no terminal window visible)
' Use this for Windows startup so Jarvis runs in background.
' ============================================================

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

' Get the script's own directory
ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
ProjectDir = FSO.GetParentFolderName(ScriptDir)

' Start Backend API (hidden window)
BackendCmd = """" & ProjectDir & "\backend\.venv\Scripts\pythonw.exe"" -m uvicorn main:app --host 0.0.0.0 --port 8000"
WshShell.CurrentDirectory = ProjectDir & "\backend"
WshShell.Run BackendCmd, 0, False

' Wait 4 seconds for backend to start
WScript.Sleep 4000

' Start Voice Agent — HUD is the UI, so run hidden
VoiceCmd = """" & ProjectDir & "\backend\.venv\Scripts\python.exe"" main.py"
WshShell.CurrentDirectory = ProjectDir & "\voice_agent"
WshShell.Run VoiceCmd, 0, False
