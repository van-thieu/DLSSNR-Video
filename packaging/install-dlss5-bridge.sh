#!/usr/bin/env bash
# Install the DLSS5 bridge daemon as a user service. Needed once the Firefox add-on is installed
# permanently: the temporary-add-on launcher no longer starts the daemon for you.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="$HOME/.config/systemd/user/dlss5-bridge.service"

mkdir -p "$HOME/.config/systemd/user" "$HOME/dlssnr-runtime"
install -m 644 "$HERE/dlss5-bridge.service" "$UNIT"
# The temporary-add-on launcher may have started its own daemon on port 8765; the service needs it.
pkill -f 'dlssnr_bridge.py --server' 2>/dev/null || true
sleep 1
systemctl --user daemon-reload
systemctl --user enable --now dlss5-bridge.service

echo
systemctl --user status dlss5-bridge.service --no-pager || true
echo
echo "Daemon listening on 127.0.0.1:8765 (verify with: curl -s -X POST -d '{\"cmd\":\"ping\"}' http://127.0.0.1:8765/)"
