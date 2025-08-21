#!/bin/bash

# === CONFIGURATION ===
# GoPro MAC addresses (Bluetooth)
GOPRO1_MAC="DC:4A:3E:AA:BB:CC"
GOPRO2_MAC="DC:4A:3E:DD:EE:FF"

# GoPro Wi-Fi SSIDs
GOPRO1_SSID="HERO8 Black Achim 3"
GOPRO2_SSID="HERO8 Black Achim 4"

# GoPro Wi-Fi passwords
GOPRO1_PASS="password1"
GOPRO2_PASS="password2"

# Network interfaces
IFACE1="wlan0"
IFACE2="wlan1"

# === FUNCTIONS ===

connect_gopro() {
  local MAC=$1
  local IFACE=$2
  local SSID=$3
  local PASS=$4

  echo "[BLE] Waiting 3s for GoPro $MAC to advertise BLE..."
  sleep 3

  echo "[BLE] Activating GoPro Wi-Fi via BLE for $MAC..."
  ./ble_gopro_client.py --mac "$MAC"
  if [ $? -ne 0 ]; then
    echo "[ERROR] Failed BLE activation for $MAC"
    return 1
  fi

  echo "[WiFi] Connecting $IFACE to SSID '$SSID'..."
  nmcli dev wifi connect "$SSID" password "$PASS" ifname "$IFACE"
  if [ $? -ne 0 ]; then
    echo "[ERROR] Failed to connect $IFACE to $SSID"
    return 1
  fi

  echo "[NET] Requesting DHCP lease on $IFACE..."
  sudo dhclient "$IFACE"

  echo "[NET] Adding route to GoPro on $IFACE..."
  sudo ip route add 10.5.5.9 dev "$IFACE" || true

  echo "[CHECK] Verifying GoPro API over $IFACE..."
  curl --interface "$IFACE" --max-time 5 -s -o /dev/null -w "%{http_code}" http://10.5.5.9/gp/gpControl/status | grep -q 200
  if [ $? -eq 0 ]; then
    echo "[SUCCESS] GoPro API reachable on $IFACE."
  else
    echo "[ERROR] GoPro API not reachable on $IFACE."
  fi
}

# === MAIN ===

echo "[INFO] Connecting to both GoPros using BLE + WiFi..."

connect_gopro "$GOPRO1_MAC" "$IFACE1" "$GOPRO1_SSID" "$GOPRO1_PASS"
echo "----------------------------------------"
connect_gopro "$GOPRO2_MAC" "$IFACE2" "$GOPRO2_SSID" "$GOPRO2_PASS"
echo "----------------------------------------"

echo "[DONE] Both GoPros should now be reachable via Wi-Fi."
