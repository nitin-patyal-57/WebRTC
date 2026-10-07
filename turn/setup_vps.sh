#!/usr/bin/env bash
# Install coturn on a fresh Debian/Ubuntu VPS.
#
#   sudo bash turn/setup_vps.sh
#
# Afterwards, in server/.env:
#   TURN_URL=turn:<VPS_PUBLIC_IP>:3478
#   TURN_USERNAME=soundbox
#   TURN_CREDENTIAL=<same as turn/turnserver.conf user line>
#
# Then verify from your PC:
#   .venv/Scripts/python.exe scripts/check_turn.py
set -euo pipefail

CONF_SRC="$(cd "$(dirname "$0")" && pwd)/turnserver.conf"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash $0" >&2
    exit 1
fi

apt-get update
apt-get install -y coturn

# 600: config contains the TURN password; coturn warns on world-readable.
install -m 600 "$CONF_SRC" /etc/turnserver.conf

# Debian/Ubuntu package ships /etc/default/coturn with AUTOSTART=false.
if [ -f /etc/default/coturn ]; then
    sed -i 's/^#\?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn
    grep -q '^TURNSERVER_ENABLED=' /etc/default/coturn || echo 'TURNSERVER_ENABLED=1' >> /etc/default/coturn
fi

# VPS has a directly bound public IP: external-ip stays commented in the
# config (coturn uses the interface address).
systemctl enable coturn
systemctl restart coturn

sleep 1
systemctl --no-pager -l status coturn | head -n 5 || true
ss -lntup | grep -E ':(3478|40[0-9]{3})\b' || true

# On most VPS providers UDP 3478 + 40000-40100 are open by default; if you
# run a host firewall (ufw/iptables), open them:
#   ufw allow 3478/udp && ufw allow 3478/tcp && ufw allow 40000:40100/udp
echo
echo "Done. Set TURN_URL=turn:<this-server-public-ip>:3478 in server/.env"
