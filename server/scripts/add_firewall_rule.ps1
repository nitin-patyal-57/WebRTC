$ErrorActionPreference = "SilentlyContinue"

netsh advfirewall firewall delete rule name="AI Voice Server 8443" | Out-Null
netsh advfirewall firewall add rule name="AI Voice Server 8443" dir=in action=allow protocol=TCP localport=8443 profile=any | Out-Null

# ICE/STUN/RTP use ephemeral UDP ports owned by the server process. Without
# these, inbound UDP is dropped once the NAT 4-tuple changes (e.g. cellular
# CGNAT rebind), which shows up as "STUN keepalive unanswered" on the client
# and consent expiry / call drops on the server.
$scripts = $PSScriptRoot
$venv = Join-Path (Split-Path $scripts -Parent) ".venv\Scripts"
foreach ($exe in @("python.exe", "uvicorn.exe")) {
    $path = Join-Path $venv $exe
    if (Test-Path $path) {
        $rule = "AI Voice Server ICE UDP ($exe)"
        netsh advfirewall firewall delete rule name="$rule" | Out-Null
        netsh advfirewall firewall add rule name="$rule" dir=in action=allow program="$path" protocol=UDP enable=yes profile=any | Out-Null
    }
}

netsh advfirewall firewall show rule name="AI Voice Server 8443" | Out-File "$env:TEMP\fw_voice_result.txt" -Encoding utf8
netsh advfirewall firewall show rule name="AI Voice Server ICE UDP (python.exe)" | Out-File "$env:TEMP\fw_voice_result.txt" -Append -Encoding utf8
