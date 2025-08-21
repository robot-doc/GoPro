#!/usr/bin/env bash
# connect_gopro.sh
# Copy to /usr/local/bin and set executable:
#   sudo mv connect_gopro.sh /usr/local/bin/
#   sudo chmod +x /usr/local/bin/connect_gopro.sh
# Run: sudo connect_gopro.sh

# Config
GOPRO_MAC="D0:21:F8:9C:FF:80"
GOPRO_SSID="HERO8 Black Achim 3"
GOPRO_PSK="5d3-QNv-MTm"
WPA_CONF="/etc/wpa_supplicant/wpa_supplicant.conf"
GOPRO_IP="10.5.5.9"
PYTHON_BIN="/home/pi/gopro-ble-py/gopro-ble-py/venv/bin/python"
BLE_TOOL="/home/pi/gopro-ble-py/gopro-ble-py/main.py"

# 1. Ensure BLE advertising
echo "Waiting 3s for GoPro to advertise BLE..."
sleep 3

# 2. Enable Wi-Fi via Python BLE client
echo "Activating GoPro Wi-Fi via BLE Python client..."
"${PYTHON_BIN}" "${BLE_TOOL}" --interactive true --address "${GOPRO_MAC}" --command "wifi on"
if [ $? -ne 0 ]; then
  echo "Error: BLE Wi-Fi ON command failed." >&2
  exit 1
fi

# 3. Configure wlan0
echo "Configuring wlan0 for GoPro AP..."
sudo killall wpa_supplicant dhclient >/dev/null 2>&1 || true
sudo ip link set wlan0 down; sleep 1; sudo ip link set wlan0 up
# Ensure network block in wpa_supplicant.conf
if ! grep -q "ssid=\"${GOPRO_SSID}\"" "${WPA_CONF}"; then
  echo "Adding GoPro SSID to wpa_supplicant.conf"
  sudo tee -a "${WPA_CONF}" >/dev/null <<EOF
network={
  ssid="${GOPRO_SSID}"
  psk="${GOPRO_PSK}"
  key_mgmt=WPA-PSK
}
EOF
fi
sudo wpa_supplicant -B -i wlan0 -c "${WPA_CONF}"
echo "Requesting DHCP lease (10s timeout)..."
timeout 10 sudo dhclient wlan0
if ! ip addr show wlan0 | grep -q "inet 10\.5\.5\."; then
  echo "DHCP failed, assigning static IP..."
  sudo ip addr add 10.5.5.100/24 dev wlan0
fi

# 4. Add route
echo "Adding route to GoPro..."
sudo ip route replace ${GOPRO_IP}/32 dev wlan0 || true

# 5. Verify HTTP
echo "Verifying GoPro HTTP API..."
sleep 3
for i in 1 2 3; do
  code=$(curl -m5 --interface wlan0 -s -o /dev/null -w "%{http_code}" "http://${GOPRO_IP}/gp/gpControl/status")
  if [ "$code" = "200" ]; then
    echo "Success: GoPro API reachable with HTTP $code"
    exit 0
  fi
  echo "Attempt $i: HTTP=$code, retrying..."
  sleep 2
done

echo "Erro"