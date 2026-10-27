#!/usr/bin/env bash
# One-time setup: let the (unprivileged) ODU Tracker app bind :53 (DNS
# interception) and :80 (captive-portal claim page).
#
# Why: Linux blocks ports < 1024 for unprivileged processes
# (net.ipv4.ip_unprivileged_port_start = 1024). The dashboard, DNS proxy and
# portal all run as your normal user, so without this the proxy reports
# "cannot bind UDP 53" in the Enforcement card instead of serving.
#
# Two options — run ONE of them:
#
#   A) Recommended: raise the unprivileged port floor to 53 (covers :53 and
#      :80 for every process, survives venv/python rebuilds):
#        sudo ./tools/enable_priv_ports.sh
#
#   B) File capability on just this project's Python:
#        sudo setcap cap_net_bind_service=+ep "$(readlink -f .venv/bin/python)"
#      Caveat: must be re-run after upgrading Python/the venv, and grants the
#      capability to any script run by that interpreter.
#
# Revert A:  sudo sysctl -w net.ipv4.ip_unprivileged_port_start=1024
#            and delete /etc/sysctl.d/50-odu-unpriv-ports.conf
set -euo pipefail

if [[ "${1:-}" == "--revert" ]]; then
    sudo sysctl -w net.ipv4.ip_unprivileged_port_start=1024
    sudo rm -f /etc/sysctl.d/50-odu-unpriv-ports.conf
    echo "Reverted: unprivileged ports start at 1024 again."
    exit 0
fi

echo "Setting net.ipv4.ip_unprivileged_port_start = 53 ..."
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=53
echo 'net.ipv4.ip_unprivileged_port_start = 53' | \
    sudo tee /etc/sysctl.d/50-odu-unpriv-ports.conf >/dev/null
echo "Persisted in /etc/sysctl.d/50-odu-unpriv-ports.conf"
echo
echo "Now :53 and :80 are bindable without root. Restart ODU Tracker"
echo "(or hit 'Arm portal' in the Enforcement card) to pick it up."
echo "Revert anytime with: $0 --revert"
