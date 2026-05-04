#!/bin/bash
# setup_ap.sh — create a WiFi hotspot if no internet/WiFi is available.
#
# Usage:
#   ./setup_ap.sh          # auto: check connectivity, create AP if offline
#   ./setup_ap.sh start    # force-create AP
#   ./setup_ap.sh stop     # tear down AP
#
# The hotspot SSID / password are set below.

SSID="PropellerTeststand"
PASS="teststand123"
CON_NAME="teststand-ap"
IFACE="wlan0"

NMCLI="nmcli"
# When run as non-root, prefix nmcli with sudo
[ "$(id -u)" -ne 0 ] && NMCLI="sudo nmcli"

check_connectivity() {
  # Returns 0 if we have a WiFi connection with internet, 1 otherwise
  $NMCLI -t -f TYPE,STATE device | grep -q "^wifi:connected" && return 0
  return 1
}

start_ap() {
  # Remove stale profile from a previous run (survives reboots, blocks re-creation)
  $NMCLI connection delete "$CON_NAME" 2>/dev/null || true

  echo "Starting hotspot: SSID=$SSID on $IFACE"
  $NMCLI device wifi hotspot \
    ifname "$IFACE" \
    ssid   "$SSID" \
    password "$PASS" \
    con-name "$CON_NAME"

  if [ $? -eq 0 ]; then
    PI_IP=$(ip -4 addr show "$IFACE" | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
    echo ""
    echo "Hotspot active. Connect to:"
    echo "  SSID    : $SSID"
    echo "  Password: $PASS"
    echo "  Web UI  : http://${PI_IP:-10.42.0.1}:5000"
  else
    echo "ERROR: failed to start hotspot. Check nmcli and wlan0."
    exit 1
  fi
}

stop_ap() {
  $NMCLI connection down "$CON_NAME" 2>/dev/null
  $NMCLI connection delete "$CON_NAME" 2>/dev/null
  echo "Hotspot stopped."
}

case "${1:-auto}" in
  start) start_ap ;;
  stop)  stop_ap  ;;
  auto)
    if check_connectivity; then
      PI_IP=$(hostname -I | awk '{print $1}')
      echo "WiFi connected. Web UI: http://${PI_IP}:5000"
    else
      echo "No WiFi connection found — creating hotspot."
      start_ap
    fi
    ;;
  *) echo "Usage: $0 [start|stop|auto]"; exit 1 ;;
esac
