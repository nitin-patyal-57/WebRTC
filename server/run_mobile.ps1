param(
    [int]$Port = 8443,
    [string[]]$ExtraHosts = @()
)

$ErrorActionPreference = "Stop"
$serverRoot = $PSScriptRoot
Set-Location $serverRoot

$cert = Join-Path $serverRoot "certs\cert.pem"
$key = Join-Path $serverRoot "certs\key.pem"

if (-not (Test-Path $cert) -or -not (Test-Path $key)) {
    Write-Host "Generating self-signed dev certificate..."
    $ips = @()
    Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
        ForEach-Object { $ips += $_.IPAddress }
    $ips = $ips + $ExtraHosts | Select-Object -Unique
    & ".\.venv\Scripts\python.exe" "scripts\generate_dev_cert.py" @ips
}

$lanIp = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "Server URLs (same Wi-Fi as phone):"
Write-Host "  https://$lanIp`:$Port"
Write-Host "  https://127.0.0.1:$Port (this PC)"
Write-Host "Dashboard (this PC): http://127.0.0.1:9000/dashboard or https://$lanIp`:$Port/dashboard"
Write-Host ""
Write-Host "Enter the URL above in the Android app, then tap Load."
$fw = netsh advfirewall firewall show rule name="AI Voice Server ICE UDP (python.exe)" 2>&1 | Out-String
if ($fw -notmatch "AI Voice Server ICE UDP") {
    Write-Host "Adding Windows Firewall rules for TCP $Port + inbound UDP/ICE (accept UAC if prompted)..."
    try {
        Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","$PSScriptRoot\scripts\add_firewall_rule.ps1" -Verb RunAs -Wait
    } catch {
        Write-Host "Could not add firewall rules automatically."
        Write-Host "Run as Administrator:  powershell -ExecutionPolicy Bypass -File .\scripts\add_firewall_rule.ps1"
    }
}
Write-Host ""

& ".\.venv\Scripts\uvicorn.exe" main:app --host 0.0.0.0 --port $Port --ssl-certfile $cert --ssl-keyfile $key
