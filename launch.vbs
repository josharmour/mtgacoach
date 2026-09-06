Option Explicit

Dim shell, fso, scriptDir, cmd, i, windowStyle, runtimeRoot, venvPython
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = """" & scriptDir & "\launch.bat"""

For i = 0 To WScript.Arguments.Count - 1
    cmd = cmd & " " & QuoteArg(WScript.Arguments(i))
Next

' Hidden window for normal launches (the venv pythonw GUI needs no console).
' But on first run -- no venv yet -- launch.bat falls back to the console
' setup wizard, and hiding THAT means an invisible setup (or an invisible
' error prompt). Mirror launch.bat's venv resolution and show the window
' when provisioning is still needed.
runtimeRoot = shell.ExpandEnvironmentStrings("%MTGACOACH_RUNTIME_ROOT%")
If runtimeRoot = "%MTGACOACH_RUNTIME_ROOT%" Then
    runtimeRoot = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
    If runtimeRoot = "%LOCALAPPDATA%" Then
        runtimeRoot = scriptDir & "\runtime"
    Else
        runtimeRoot = runtimeRoot & "\mtgacoach"
    End If
End If
venvPython = runtimeRoot & "\venv\Scripts\python.exe"

windowStyle = 0
If Not fso.FileExists(venvPython) Then
    windowStyle = 1
End If

shell.Run cmd, windowStyle, False

Function QuoteArg(value)
    QuoteArg = """" & Replace(value, """", """""") & """"
End Function
