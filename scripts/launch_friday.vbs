' ============================================================
' FRIDAY — Silent launcher with health-checked startup.
'
' This script supersedes launch_jarvis.vbs. Drop a shortcut to
' THIS file in shell:startup to make Friday auto-start at login.
'
' What changed (May 18 2026):
'   • The old 12-second blind sleep was a race. On cold boots the
'     embedding model + Whisper take longer; on warm boots that wait
'     was wasted. New behaviour: poll /health every 500 ms for up
'     to 45 s, start the voice agent the moment the backend is up.
'   • Sets FRIDAY_AUTOBOOT=1 so the voice agent speaks a greeting
'     instead of sitting silent waiting for activation.
'   • Aborts cleanly with a popup if the backend never comes up.
' ============================================================

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

' Resolve project root from this script's location.
ScriptDir  = FSO.GetParentFolderName(WScript.ScriptFullName)
ProjectDir = FSO.GetParentFolderName(ScriptDir)
LogDir     = ProjectDir & "\logs"

If Not FSO.FolderExists(LogDir) Then
    FSO.CreateFolder(LogDir)
End If

' --- Start backend ----------------------------------------------------
' python.exe wrapped in `cmd /c` so stdout+stderr both go to the log.
' If Friday isn't responding after boot, logs\backend.log is the first
' place to look.
BackendPython = """" & ProjectDir & "\backend\.venv\Scripts\python.exe"""
BackendLog    = """" & LogDir & "\backend.log"""
BackendCmd    = "cmd /c """ & BackendPython & " -m uvicorn main:app --host 127.0.0.1 --port 8000 >" & BackendLog & " 2>&1"""

WshShell.CurrentDirectory = ProjectDir & "\backend"
WshShell.Run BackendCmd, 0, False

' --- Wait for backend /health to return 200 ---------------------------
' Up to 45 seconds (90 attempts × 500 ms). On warm boots this typically
' completes in 3-5 seconds; first cold boot after install can take
' 30+ as Whisper and the embedding model load.
backendReady = False
attempts     = 0
maxAttempts  = 90

Do While Not backendReady And attempts < maxAttempts
    On Error Resume Next
    Set http = CreateObject("MSXML2.XMLHTTP.6.0")
    http.Open "GET", "http://127.0.0.1:8000/health", False
    http.Send
    If Err.Number = 0 And http.Status = 200 Then
        backendReady = True
    End If
    On Error GoTo 0
    If Not backendReady Then
        WScript.Sleep 500
        attempts = attempts + 1
    End If
Loop

If Not backendReady Then
    ' Backend never came up. Don't silently launch the voice agent —
    ' it would just sit there failing every call. Tell Sir, then exit.
    MsgBox "Friday backend failed to start after " & (maxAttempts / 2) & "s. " & vbCrLf & _
           "See logs\backend.log for details.", _
           vbExclamation, "Friday Startup"
    WScript.Quit 1
End If

' --- Start voice agent ------------------------------------------------
' pythonw.exe (no console). FRIDAY_AUTOBOOT=1 makes the voice agent
' speak the briefing as soon as it's wired up.
VoicePython = """" & ProjectDir & "\backend\.venv\Scripts\pythonw.exe"""
VoiceLog    = """" & LogDir & "\voice_agent.log"""
VoiceCmd    = "cmd /c ""set FRIDAY_AUTOBOOT=1&& " & VoicePython & " main.py >" & VoiceLog & " 2>&1"""

WshShell.CurrentDirectory = ProjectDir & "\voice_agent"
WshShell.Run VoiceCmd, 0, False
