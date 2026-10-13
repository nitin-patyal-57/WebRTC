param(
    [int]$Port = 3000,
    [switch]$Force   # stop whatever is already listening on $Port
)

# Plain foreground run of the WebRTC server (no ngrok, no auto-restart).
#
# Usage:  powershell -ExecutionPolicy Bypass -File .\run_server.ps1
#         powershell -ExecutionPolicy Bypass -File .\run_server.ps1 -Port 3001
#         powershell -ExecutionPolicy Bypass -File .\run_server.ps1 -Force
# Stop:   Ctrl+C

$ErrorActionPreference = "Stop"
$serverRoot = $PSScriptRoot
Set-Location $serverRoot

# --- activate venv ---
$activate = Join-Path $serverRoot ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) {
    Write-Host "[server] venv not found at $activate" -ForegroundColor Red
    Write-Host "[server] create it with: python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt"
    exit 1
}
. $activate

# --- check the port ---
Write-Host "[server] checking port $Port ..."
$listeners = netstat -ano | findstr ":$Port" | findstr "LISTENING"
if ($listeners) {
    Write-Host $listeners
    $pids = $listeners | ForEach-Object { ($_ -split "\s+")[-1] } | Select-Object -Unique
    if ($Force) {
        foreach ($p in $pids) {
            Write-Host "[server] stopping pid $p on port $Port"
            Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 500
    } else {
        Write-Host "[server] port $Port is in use by pid(s): $($pids -join ', ')" -ForegroundColor Yellow
        Write-Host "[server] rerun with -Force to stop it, or pick another port with -Port"
        exit 1
    }
} else {
    Write-Host "[server] port $Port is free"
}

# --- run ---
Write-Host "[server] starting on http://0.0.0.0:$Port (Ctrl+C to stop)"
python -m uvicorn main:app --host 0.0.0.0 --port $Port
