Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""<repo>\scripts\quick-capture.ps1""", 0, False
