Option Explicit

Dim shell, fso, scriptDir, cmd, i
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = """" & scriptDir & "\launch.bat"""

For i = 0 To WScript.Arguments.Count - 1
    cmd = cmd & " " & QuoteArg(WScript.Arguments(i))
Next

shell.Run cmd, 0, False

Function QuoteArg(value)
    QuoteArg = """" & Replace(value, """", """""") & """"
End Function
