#!/usr/bin/env bash
# connect_single_gopro.sh
# Connect to a single specific GoPro
# Usage: sudo ./connect_single_gopro.sh [gopro3|gopro1]

GOPRO_ID=$1

if [ "$GOPRO_ID" != "gopro3" ] && [ "$GOPRO_ID" != "gopro1" ]; then
    echo "Usage: $0 [gopro3|gopro1]"
    exit 1
fi

# GoPro 1 Config
if [ "$GOPRO_ID" = "gopro3" ]; then
    GOPRO_MAC="D0:21:F8:9C:FF:80"
    GOPRO_SSID="HERO8 Achim 3"
    GOPRO_PSK="5d3-QNv-MTm"
    GOPRO_IP="10.5.5.9"
    WLAN_INTERFACE="wlan0"
    WPA_CONF="/etc/wpa_supplicant/wpa_supplicant_wlan0.conf"
    GOPRO_NAME="GoPro3"
fi

# GoPro 2 Config
if [ "$GOPRO_ID" = "gopro1" ]; then
    GOPRO_MAC="C8:52:0D:A5:9A:39"
    GOPRO_SSID="HERO8 Achim 1"
    GOPRO_PSK="vDh-p7g-TDj"
    GOPRO_IP="10.5.5.9"
    WLAN_INTERFACE="wlan1"
    WPA_CONF="/etc/wpa_supplicant/wpa_supplicant_wlan1.conf"
    GOPRO_NAME="GoPro1"
fi

# Common Config
PYTHON_BIN="/home/pi/gopro-ble-py/gopro-ble-py/venv/bin/python"
BLE_TOOL="/home/pi/gopro-ble-py/gopro-ble-py/main.py"

echo "=== Connecting to $GOPRO_NAME only ==="

# Clean up this specific interface
echo "Cleaning up $WLAN_INTERFACE..."
sudo pkill -9 -f "wpa_supplicant.*$WLAN_INTERFACE" || true
sudo pkill -9 -f "dhclient.*$WLAN_INTERFACE" || true
sudo rm -f "/var/run/wpa_supplicant/$WLAN_INTERFACE" || true

# Reset Bluetooth
echo "Resetting Bluetooth..."
sudo hciconfig hci0 down && sudo hciconfig hci0 up
sleep 3

# Reset interface
sudo ip link set $WLAN_INTERFACE down
sudo ip addr flush dev $WLAN_INTERFACE || true
sleep 2
sudo ip link set $WLAN_INTERFACE up
sleep 3

# BLE connection with retries
echo "Waiting 8s for $GOPRO_NAME to advertise BLE..."
sleep 8

echo "Activating $GOPRO_NAME Wi-Fi via BLE..."
BLE_SUCCESS=0
for attempt in 1 2 3; do
    echo "BLE attempt $attempt/3 for $GOPRO_NAME..."
    if "${PYTHON_BIN}" "${BLE_TOOL}" --interactive true --address "${GOPRO_MAC}" --command "wifi on"; then
        BLE_SUCCESS=1
        echo "BLE command succeeded for $GOPRO_NAME"
        break
    else
        echo "BLE attempt $attempt failed for $GOPRO_NAME"
        if [ $attempt -lt 3 ]; then
            echo "Waiting 5 seconds before retry..."
            sleep 5
            sudo hciconfig hci0 down && sudo hciconfig hci0 up
            sleep 2
        fi
    fi
done

if [ $BLE_SUCCESS -eq 0 ]; then
    echo "Error: All BLE attempts failed for $GOPRO_NAME" >&2
    exit 1
fi

# Wait for Wi-Fi to start
echo "Waiting 5s for $GOPRO_NAME Wi-Fi to start..."
sleep 5

# Create wpa_supplicant config
if [ ! -f "$WPA_CONF" ]; then
    echo "Creating $WPA_CONF"
    sudo tee "$WPA_CONF" >/dev/null <<EOF
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=DE
EOF
fi

# Add network if not exists
if ! grep -q "ssid=\"${GOPRO_SSID}\"" "${WPA_CONF}"; then
    echo "Adding $GOPRO_NAME SSID to $WPA_CONF"
    sudo tee -a "${WPA_CONF}" >/dev/null <<EOF
network={
    ssid="${GOPRO_SSID}"
    psk="${GOPRO_PSK}"
    key_mgmt=WPA-PSK
}
EOF
fi

# Start wpa_supplicant
echo "Starting wpa_supplicant for $WLAN_INTERFACE..."
sudo wpa_supplicant -B -i $WLAN_INTERFACE -c "${WPA_CONF}"
sleep 5

# Get IP
echo "Requesting DHCP lease for $WLAN_INTERFACE..."
timeout 15 sudo dhclient $WLAN_INTERFACE

# Check IP, assign static if needed
if ! ip addr show $WLAN_INTERFACE | grep -q "inet 10\.5\.5\."; then
    echo "DHCP failed, assigning static IP..."
    if [ "$WLAN_INTERFACE" = "wlan0" ]; then
        sudo ip addr add 10.5.5.100/24 dev $WLAN_INTERFACE
    else
        sudo ip addr add 10.5.5.101/24 dev $WLAN_INTERFACE
    fi
fi

# Add route
echo "Adding route to $GOPRO_NAME..."
sudo ip route replace ${GOPRO_IP}/32 dev $WLAN_INTERFACE || true

# Verify
echo "Verifying $GOPRO_NAME HTTP API..."
for i in 1 2 3 4 5; do
    code=$(curl -m10 --interface $WLAN_INTERFACE -s -o /dev/null -w "%{http_code}" "http://${GOPRO_IP}/gp/gpControl/status")
    if [ "$code" = "200" ]; then
        echo "SUCCESS: $GOPRO_NAME connected and reachable!"
        exit 0
    fi
    echo "Attempt $i/5: HTTP=$code, retrying..."
    sleep 3
done

echo "ERROR: $GOPRO_NAME connection failed"
exit 1