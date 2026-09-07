' ATIP auto-start wrapper for the Windows Startup folder.
'
' Why a .vbs rather than putting start_atip.bat straight into Startup:
'   - a .bat in Startup opens a full console window that steals focus at logon;
'     this launches it MINIMISED instead (window style 7).
'   - it still runs in a real window, deliberately: ATIP is a foreground process
'     you need to be able to see and Ctrl+C. A fully hidden process would leave
'     no way to stop it and no visible errors.
'
' It resolves the project folder from this script's own location, so the pair
' (atip_autostart.vbs + start_atip.bat) works wherever the project lives.
' The Startup folder gets a one-line .vbs that points here, so a git pull keeps
' the launcher up to date without re-editing anything in Startup.
'
' NOTE: the Startup folder gives no automatic restart if ATIP crashes. For that,
' use Windows Task Scheduler with "Restart on failure" - see PATCH_NOTES.md.

Option Explicit

Dim fso, shell, scriptDir, batPath

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = fso.BuildPath(scriptDir, "start_atip.bat")

If Not fso.FileExists(batPath) Then
    MsgBox "ATIP auto-start failed: start_atip.bat not found in" & vbCrLf & _
           scriptDir & vbCrLf & vbCrLf & _
           "The Startup entry is pointing at a folder that no longer contains " & _
           "the project.", 16, "ATIP"
    WScript.Quit 1
End If

' 7 = minimised, no focus.  False = don't block.
shell.CurrentDirectory = scriptDir
shell.Run """" & batPath & """", 7, False
