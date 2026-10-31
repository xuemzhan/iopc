# OPC 速记窗：Ctrl+Alt+O 唤起，写完回车即入库（后台自动提炼）
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$ErrorActionPreference = "Stop"
[System.Windows.Forms.Application]::EnableVisualStyles()

$hub = "http://127.0.0.1:5370"

# 中枢没起就拉起来
$alive = $false
try { if ((Invoke-RestMethod "$hub/api/health" -TimeoutSec 2).ok) { $alive = $true } } catch { }
if (-not $alive) {
    $env:PYTHONPATH = "<repo>"
    Start-Process -FilePath "python" -ArgumentList "-m", "opc", "serve" -WorkingDirectory "<repo>" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "OPC 速记"
$form.Size = New-Object System.Drawing.Size(520, 300)
$form.StartPosition = "CenterScreen"
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::FromArgb(14, 17, 22)
$form.ForeColor = [System.Drawing.Color]::FromArgb(230, 237, 247)
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false

$hint = New-Object System.Windows.Forms.Label
$hint.Text = "想到什么就写什么 —— 会自动提炼成实体 / 概念 / 主张，写进 Obsidian"
$hint.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$hint.ForeColor = [System.Drawing.Color]::FromArgb(139, 151, 168)
$hint.Location = New-Object System.Drawing.Point(16, 12)
$hint.Size = New-Object System.Drawing.Size(480, 36)
$form.Controls.Add($hint)

$box = New-Object System.Windows.Forms.TextBox
$box.Multiline = $true
$box.ScrollBars = "Vertical"
$box.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 11)
$box.BackColor = [System.Drawing.Color]::FromArgb(20, 25, 33)
$box.ForeColor = [System.Drawing.Color]::FromArgb(230, 237, 247)
$box.BorderStyle = "FixedSingle"
$box.Location = New-Object System.Drawing.Point(16, 52)
$box.Size = New-Object System.Drawing.Size(478, 150)
$form.Controls.Add($box)

$status = New-Object System.Windows.Forms.Label
$status.Text = ""
$status.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$status.ForeColor = [System.Drawing.Color]::FromArgb(94, 234, 212)
$status.Location = New-Object System.Drawing.Point(16, 210)
$status.Size = New-Object System.Drawing.Size(300, 30)
$form.Controls.Add($status)

$btnSave = New-Object System.Windows.Forms.Button
$btnSave.Text = "记下来  (Ctrl+Enter)"
$btnSave.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10)
$btnSave.BackColor = [System.Drawing.Color]::FromArgb(94, 234, 212)
$btnSave.ForeColor = [System.Drawing.Color]::FromArgb(6, 35, 31)
$btnSave.FlatStyle = "Flat"
$btnSave.Location = New-Object System.Drawing.Point(318, 208)
$btnSave.Size = New-Object System.Drawing.Size(176, 36)
$form.Controls.Add($btnSave)

$btnAsk = New-Object System.Windows.Forms.Button
$btnAsk.Text = "当问题去问"
$btnAsk.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$btnAsk.FlatStyle = "Flat"
$btnAsk.ForeColor = [System.Drawing.Color]::FromArgb(139, 151, 168)
$btnAsk.BackColor = [System.Drawing.Color]::FromArgb(20, 25, 33)
$btnAsk.Location = New-Object System.Drawing.Point(16, 208)
$btnAsk.Size = New-Object System.Drawing.Size(120, 36)
$form.Controls.Add($btnAsk)

function Save-Note {
    $text = $box.Text.Trim()
    if (-not $text) { return }
    $status.Text = "提炼中…（约 30-60 秒）"
    $btnSave.Enabled = $false
    [System.Windows.Forms.Application]::DoEvents()
    try {
        $json = @{ text = $text } | ConvertTo-Json -Compress
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
        $r = Invoke-RestMethod -Uri "$hub/api/capture" -Method Post -Body $bytes `
            -ContentType "application/json; charset=utf-8" -TimeoutSec 240
        if ($r.skipped) {
            $status.Text = "已存在，跳过"
        } else {
            $status.Text = "已入库：" + ($r.title -replace "`n", " ")
        }
        Start-Sleep -Milliseconds 1400
        $form.Close()
    } catch {
        $status.ForeColor = [System.Drawing.Color]::FromArgb(248, 113, 113)
        $status.Text = "失败：" + $_.Exception.Message
        $btnSave.Enabled = $true
    }
}

$btnSave.Add_Click({ Save-Note })
$btnAsk.Add_Click({
        $q = [uri]::EscapeDataString($box.Text.Trim())
        if ($q) { Start-Process "$hub/?q=$q" }
        $form.Close()
    })
$box.Add_KeyDown({
        if ($_.Control -and $_.KeyCode -eq "Enter") { Save-Note }
        if ($_.KeyCode -eq "Escape") { $form.Close() }
    })

$form.Add_Shown({ $box.Focus() })
[void]$form.ShowDialog()
