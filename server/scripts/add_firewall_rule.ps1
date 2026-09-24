netsh advfirewall firewall delete rule name="AI Voice Server 8443" | Out-Null
netsh advfirewall firewall add rule name="AI Voice Server 8443" dir=in action=allow protocol=TCP localport=8443 profile=any
netsh advfirewall firewall show rule name="AI Voice Server 8443" | Out-File "$env:TEMP\fw_voice_result.txt" -Encoding utf8
