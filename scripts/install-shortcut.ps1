# 在桌面创建「OPC 速记」快捷方式，并绑定全局热键 Ctrl+Alt+O
$ErrorActionPreference = "Stop"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "OPC 速记.lnk"
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath = "wscript.exe"
$scriptDir = $PSScriptRoot
$sc.Arguments = '"' + $scriptDir + '\quick-capture.vbs"'
$sc.WorkingDirectory = (Split-Path -Parent $PSScriptRoot)
$sc.IconLocation = "shell32.dll,167"
$sc.Hotkey = "CTRL+ALT+O"
$sc.Description = "OPC 速记：写一句话，自动提炼进第二大脑"
$sc.Save()
Write-Host "已创建：$lnk" -ForegroundColor Green
Write-Host "全局热键：Ctrl+Alt+O（若被其它软件占用，可在快捷方式属性里改）" -ForegroundColor Cyan
