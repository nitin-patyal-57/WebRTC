param(
    [int]$Port = 3000,
    [switch]$NoNgrok,
    [int]$MaxSeconds = 0   # 0 = run until Ctrl+C; >0 = self-stop (testing)
)

# Keeps the backend alive so the ngrok tunnel never serves a 502.
# The 502 happens when uvicorn stops while the tunnel stays up - this
# script owns uvicorn, restarts it automatically on crash, and reuses
# (or starts) the ngrok tunnel.
#
# Usage:  powershell -ExecutionPolicy Bypass -File .\run_dev.ps1
# Stop:   Ctrl+C  (kills the supervised uvicorn; ngrok keeps running)

$ErrorActionPreference = "Stop"
$serverRoot = $PSScriptRoot
Set-Location $serverRoot

# --- pick uvicorn binary (venv preferred, system python fallback) ---
$uvExe = Join-Path $serverRoot ".venv\Scripts\uvicorn.exe"
if (Test-Path $uvExe) {
    $uvFile = $uvExe
    $uvArgs = @("main:app", "--host", "0.0.0.0", "--port", "$Port", "--log-level", "info")
} else {
    $uvFile = "python"
    $uvArgs = @("-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "$Port", "--log-level", "info")
}

function Stop-PortListener([int]$port) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object {
            Write-Host "[dev] stopping stale listener pid $($_.OwningProcess) on port $port"
            Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
        }
}

function Get-TunnelUrl {
    $api = curl.exe -s --max-time 2 http://127.0.0.1:4040/api/tunnels
    if ($api -and $api -match '"public_url"\s*:\s*"(https://[^"]+)"') { return $Matches[1] }
    return $null
}

function Find-Ngrok {
    $cmd = Get-Command ngrok -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    try {
        $found = Get-ChildItem "C:\Program Files\WindowsApps\ngrok.ngrok_*_x64__*\ngrok.exe" -ErrorAction Stop |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    } catch {}
    $running = Get-CimInstance Win32_Process -Filter "Name='ngrok.exe'" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($running) { return $running.ExecutablePath }
    return $null
}

function Ensure-Ngrok([int]$port) {
    $url = Get-TunnelUrl
    if ($url) { return $url }
    $ngrok = Find-Ngrok
    if (-not $ngrok) {
        Write-Host "[dev] ngrok not running and not found - start it manually."
        Write-Host "[dev] (server will still be supervised; 502s only stop once it runs)"
        return $null
    }
    Write-Host "[dev] starting ngrok -> http://$port"
    Start-Process -FilePath $ngrok -ArgumentList "http", "$port" -WindowStyle Hidden | Out-Null
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $url = Get-TunnelUrl
        if ($url) { return $url }
    }
    Write-Host "[dev] ngrok started but no tunnel URL yet - check ngrok dashboard :4040"
    return $null
}

# --- main supervisor loop ---
$uvProc = $null
$global:devUvPid = $null
$firstHealthy = $false
$startedAt = Get-Date

# Safety net: if the console dies hard, still stop the supervised child.
Register-EngineEvent PowerShell.Exiting -Action {
    if ($global:devUvPid) { Stop-Process -Id $global:devUvPid -Force -ErrorAction SilentlyContinue }
} | Out-Null

try {
    if (-not $NoNgrok) {
        $tunnel = Ensure-Ngrok $Port
        if ($tunnel) { Write-Host "[dev] tunnel: $tunnel" }
    }

    while ($true) {
        if ($MaxSeconds -gt 0 -and ((Get-Date) - $startedAt).TotalSeconds -gt $MaxSeconds) {
            Write-Host "[dev] MaxSeconds reached - stopping"
            break
        }
        Stop-PortListener $Port
        $uvProc = Start-Process -FilePath $uvFile -ArgumentList $uvArgs `
            -WorkingDirectory $serverRoot -WindowStyle Hidden -PassThru
        $global:devUvPid = $uvProc.Id
        $firstHealthy = $false
        Write-Host "[dev] uvicorn running (pid $($uvProc.Id)) - Ctrl+C to stop"

        while (-not $uvProc.HasExited) {
            Start-Sleep 2
            if ($MaxSeconds -gt 0 -and ((Get-Date) - $startedAt).TotalSeconds -gt $MaxSeconds) {
                Write-Host "[dev] MaxSeconds reached - stopping"
                Stop-Process -Id $uvProc.Id -Force -ErrorAction SilentlyContinue
                break
            }
            $h = $null
            try { $h = curl.exe -s --max-time 2 "http://127.0.0.1:$Port/health" } catch {}
            if ($h -match '"status"\s*:\s*"ok"') {
                if (-not $firstHealthy) {
                    $firstHealthy = $true
                    Write-Host "[dev] backend healthy ($(Get-Date -Format HH:mm:ss))"
                }
            } elseif ($firstHealthy) {
                $firstHealthy = $false
                Write-Host "[dev] backend unhealthy - waiting for restart"
            }
        }

        Write-Host "[dev] uvicorn exited (code $($uvProc.ExitCode)) - restarting in 2s"
        Start-Sleep 2
    }
} finally {
    $global:devUvPid = $null
    if ($uvProc -and -not $uvProc.HasExited) {
        Write-Host "[dev] stopping uvicorn (pid $($uvProc.Id))"
        Stop-Process -Id $uvProc.Id -Force -ErrorAction SilentlyContinue
    }
    Write-Host "[dev] supervisor stopped (ngrok left running)"
}
