' J.A.R.V.I.S launcher — windowless, working-directory-safe, interpreter-safe.
'
' Two failure modes are designed out here:
'
' * Silent crashes. A bare `pythonw.exe main.pyw` with no explicit working
'   directory is a classic source of them: JARVIS looks for config/ data/ and
'   assets/ relative to the current directory, so if it starts from C:\Windows
'   it can hang or vanish with no error (pythonw has no console).
'
' * The wrong interpreter. pythonw.exe on PATH and `py -3` can be completely
'   different environments — and often are. Launching main.pyw directly made
'   whichever pythonw Windows resolved first the one that decided whether
'   JARVIS had its dependencies, producing "ModuleNotFoundError: No module
'   named 'psutil'" on a machine where a fully set-up Python was sitting on
'   PATH. This launcher now starts start_jarvis.py instead: it is stdlib-only,
'   so it runs under any of the interpreters below, and it then probes them all
'   and hands the app to the one that has the packages (installing them only
'   when none does).
'
' Launch UI: use the same console-style optimizer surface as run_jarvis.bat.
' start_jarvis.py exposes --console-ui for that mode. Keep the VBS launcher
' windowless while the optimizer owns the visual surface: black/translucent HUD,
' no large title header, and the angular corner-line treatment. The optimizer
' closes when JARVIS itself starts. If the launch UI is unavailable the launch
' simply proceeds without it.
'
' Runtime crashes remain captured to data/logs/jarvis_crash.log by
' core/stability.py.
Option Explicit

Dim WshShell, FSO, base, script, q, cmd, err
Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
base = FSO.GetParentFolderName(WScript.ScriptFullName)
script = base & "\start_jarvis.py"
q = Chr(34)

If Not FSO.FileExists(script) Then
    MsgBox "start_jarvis.py was not found:" & vbCrLf & script, 16, "J.A.R.V.I.S"
    WScript.Quit 1
End If

' Run from the project root so every relative path resolves.
WshShell.CurrentDirectory = base

' Try interpreters in order of "most likely to be usable":
'  1) pythonw.exe on PATH (no console window, usually the environment the
'     user actually installed into)
'  2) pyw.exe -3 (the Windows Python launcher, GUI front-end)
'  3) py.exe -3 -w (launcher with the -w / no-console switch)
' WshShell.Run only errors when the executable cannot be launched at all, so a
' clean start exits 0 and we only move on when an interpreter is genuinely absent.
For Each cmd In Array( _
        "pythonw.exe " & q & script & q & " --console-ui", _
        "pyw.exe -3 " & q & script & q & " --console-ui", _
        "py.exe -3 -w " & q & script & q & " --console-ui")
    On Error Resume Next
    err = 0
    WshShell.Run cmd, 0, False          ' 0 = hidden window, False = do not wait
    err = Err.Number
    On Error GoTo 0
    If err = 0 Then
        WScript.Quit 0
    End If
Next

MsgBox "Could not start J.A.R.V.I.S." & vbCrLf & vbCrLf & _
       "No usable 'pythonw' was found on PATH. Install Python 3.11+ (check " & _
       "'Add Python to PATH'), run install_dependencies.bat once, then start " & _
       "JARVIS again.", _
       16, "J.A.R.V.I.S"
WScript.Quit 1