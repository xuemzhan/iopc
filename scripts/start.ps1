# OPC 一键启动：拉起中枢（已在跑则复用），再打开控制台
$ErrorActionPreference = "SilentlyContinue"
$root = "<repo>"
$env:PYTHONPATH = $root
$env:PYTHONIOENCODING = "utf-8"
Set-Location $root

$alive = $false
try {
  $h = Invoke-RestMethod "http://127.0.0.1:5370/api/health" -TimeoutSec 2
  if ($h.ok) { $alive = $true }
} catch { }

if (-not $alive) {
  Write-Host "启动 OPC 中枢..." -ForegroundColor Cyan
  Start-Process -FilePath "python" -ArgumentList "-m", "opc", "serve" `
    -WorkingDirectory $root -WindowStyle Hidden
  for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    try { if ((Invoke-RestMethod "http://127.0.0.1:5370/api/health" -TimeoutSec 2).ok) { $alive = $true; break } } catch { }
  }
}

if ($alive) {
  Write-Host "中枢在线 → http://127.0.0.1:5370" -ForegroundColor Green
  Start-Process "http://127.0.0.1:5370"
} else {
  Write-Host "启动失败，请手动运行： python -m opc serve" -ForegroundColor Red
}
