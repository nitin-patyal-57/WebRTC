param(
    [int]$HttpPort = 8000
)

$ErrorActionPreference = "Stop"
$serverRoot = $PSScriptRoot
Set-Location $serverRoot

$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
if (-not (Test-Path $cloudflared)) {
    $found = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($found) { $cloudflared = $found.Source }
    else { throw "cloudflared not found" }
}

$stdout = Join-Path $serverRoot "cloud_stdout.log"
$stderr = Join-Path $serverRoot "cloud_stderr.log"
$serverOut = Join-Path $serverRoot "cloud_server_out.log"
$serverErr = Join-Path $serverRoot "cloud_server_err.log"
Remove-Item $stdout, $stderr, $serverOut, $serverErr -ErrorAction SilentlyContinue

Get-NetTCPConnection -LocalPort $HttpPort -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }

Write-Host "Starting HTTP origin on 127.0.0.1:$HttpPort ..."
$serverArgs = @(
    ".\.venv\Scripts\uvicorn.exe", "main:app",
    "--host", "127.0.0.1", "--port", "$HttpPort",
    "--log-level", "info"
)
# Keep portal open for dashboard:
Write-Host "Dashboard: http://127.0.0.1:$HttpPort/dashboard"
$serverProc = Start-Process -FilePath "powershell.exe" `
    -ArgumentList @("-NoProfile", "-Command", "& { " + ($serverArgs -join " ") + " *>> `"$serverOut`" 2>&1 }") `
    -WindowStyle Hidden -PassThru

$deadline = (Get-Date).AddSeconds(20)
$up = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 400
    try {
        $h = curl.exe -s --max-time 2 "http://127.0.0.1:$HttpPort/health"
        if ($h -match "ok") { $up = $true; break }
    } catch {}
}
if (-not $up) {
    Write-Host "Origin failed to start. Logs:"
    Get-Content $serverOut, $serverErr -ErrorAction SilentlyContinue | Select-Object -Last 30
    exit 1
}
Write-Host "Origin OK"

Write-Host "Starting Cloudflare quick tunnel ..."
$tunnelProc = Start-Process -FilePath $cloudflared `
    -ArgumentList @("tunnel", "--url", "http://127.0.0.1:$HttpPort") `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden -PassThru

$url = $null
$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    $text = ""
    foreach ($f in @($stderr, $stdout)) {
        if (Test-Path $f) { $text += (Get-Content $f -Raw -ErrorAction SilentlyContinue) }
    }
    if ($text -match "https://[a-z0-9-]+\.trycloudflare\.com") {
        $url = $Matches[0]
        break
    }
}

Write-Host ""
if ($url) {
    Write-Host "PUBLIC URL (paste in APK):"
    Write-Host "  $url"
    Write-Host ""
    Write-Host "Tunnel PID=$($tunnelProc.Id)  Server PID=$($serverProc.Id)"
    Write-Host "This URL works on mobile data. Keep this window/process running."
} else {
    Write-Host "Tunnel URL not found yet. Check logs:"
    Get-Content $stderr, $stdout -ErrorAction SilentlyContinue | Select-Object -Last 40
}
Write-Host ""
Write-Host "Cloud server log: $serverOut"
Write-Host "Tunnel log: $stderr"
