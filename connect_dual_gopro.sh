#!/usr/bin/env bash
# connect_dual_gopro.sh
# Modified to handle two GoPros with two Wi-Fi adapters
# Copy to /usr/local/bin and set executable:
#   sudo mv connect_dual_gopro.sh /usr/local/bin/
#   sudo chmod +x /usr/local/bin/connect_dual_gopro.sh
# Run: sudo connect_dual_gopro.sh

# GoPro 1 Config
GOPRO3_MAC="D0:21:F8:9C:FF:80"
GOPRO3_SSID="HERO8 Achim 3"
GOPRO3_PSK="5d3-QNv-MTm"
GOPRO3_IP="10.5.5.9"
WLAN1_INTERFACE="wlan0"

# GoPro 2 Config (you'll need to update these values)
GOPRO1_MAC="C8:52:0D:A5:9A:39"  # Replace with your second GoPro's MAC
GOPRO1_SSID="HERO8 Achim 1"  # Replace with your second GoPro's SSID
GOPRO1_PSK="vDh-p7g-TDj"  # Replace with your second GoPro's password
GOPRO1_IP="10.5.5.9"  # This will be the same IP on its own network
WLAN2_INTERFACE="wlan1"  # Your second Wi-Fi adapter

# Common Config
WPA_CONF1="/etc/wpa_supplicant/wpa_supplicant_wlan0.conf"
WPA_CONF2="/etc/wpa_supplicant/wpa_supplicant_wlan1.conf"
PYTHON_BIN="/home/pi/gopro-ble-py/gopro-ble-py/venv/bin/python"
BLE_TOOL="/home/pi/gopro-ble-py/gopro-ble-py/main.py"

# Function to connect a single GoPro
connect_gopro() {
    local GOPRO_MAC=$1
    local GOPRO_SSID=$2
    local GOPRO_PSK=$3
    local GOPRO_IP=$4
    local WLAN_INTERFACE=$5
    local WPA_CONF=$6
    local GOPRO_NAME=$7

    echo "=== Connecting to $GOPRO_NAME ==="
    
    # 1. Ensure BLE advertising
    echo "Waiting 3s for $GOPRO_NAME to advertise BLE..."
    sleep 3

    # 2. Enable Wi-Fi via Python BLE client
    echo "Activating $GOPRO_NAME Wi-Fi via BLE Python client..."
    "${PYTHON_BIN}" "${BLE_TOOL}" --interactive true --address "${GOPRO_MAC}" --command "wifi on"
    if [ $? -ne 0 ]; then
        echo "Error: BLE Wi-Fi ON command failed for $GOPRO_NAME." >&2
        return 1
    fi

    # 3. Configure wlan interface
    echo "Configuring $WLAN_INTERFACE for $GOPRO_NAME AP..."
    sudo killall wpa_supplicant dhclient >/dev/null 2>&1 || true
    sudo ip link set $WLAN_INTERFACE down; sleep 1; sudo ip link set $WLAN_INTERFACE up
    
    # Create separate wpa_supplicant config if it doesn't exist
    if [ ! -f "$WPA_CONF" ]; then
        echo "Creating $WPA_CONF"
        sudo tee "$WPA_CONF" >/dev/null <<EOF
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=DE
EOF
    fi
    
    # Ensure network block in wpa_supplicant.conf
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
    
    sudo wpa_supplicant -B -i $WLAN_INTERFACE -c "${WPA_CONF}"
    echo "Requesting DHCP lease for $WLAN_INTERFACE (10s timeout)..."
    timeout 10 sudo dhclient $WLAN_INTERFACE
    
    if ! ip addr show $WLAN_INTERFACE | grep -q "inet 10\.5\.5\."; then
        echo "DHCP failed for $WLAN_INTERFACE, assigning static IP..."
        # Use different static IPs to avoid conflicts
        if [ "$WLAN_INTERFACE" = "wlan0" ]; then
            sudo ip addr add 10.5.5.100/24 dev $WLAN_INTERFACE
        else
            sudo ip addr add 10.5.5.101/24 dev $WLAN_INTERFACE
        fi
    fi

    # 4. Add route
    echo "Adding route to $GOPRO_NAME..."
    sudo ip route replace ${GOPRO_IP}/32 dev $WLAN_INTERFACE || true

    # 5. Verify HTTP
    echo "Verifying $GOPRO_NAME HTTP API..."
    sleep 3
    for i in 1 2 3; do
        code=$(curl -m5 --interface $WLAN_INTERFACE -s -o /dev/null -w "%{http_code}" "http://${GOPRO_IP}/gp/gpControl/status")
        if [ "$code" = "200" ]; then
            echo "Success: $GOPRO_NAME API reachable with HTTP $code"
            return 0
        fi
        echo "Attempt $i: HTTP=$code, retrying..."
        sleep 2
    done

    echo "Error: $GOPRO_NAME connection failed" >&2
    return 1
}

# Main execution
echo "Starting dual GoPro connection process..."

# Check if both interfaces exist
if ! ip link show $WLAN1_INTERFACE >/dev/null 2>&1; then
    echo "Error: $WLAN1_INTERFACE not found. Please ensure your second Wi-Fi adapter is connected." >&2
    exit 1
fi

if ! ip link show $WLAN2_INTERFACE >/dev/null 2>&1; then
    echo "Error: $WLAN2_INTERFACE not found. Please ensure your second Wi-Fi adapter is connected." >&2
    exit 1
fi

# Connect to both GoPros in parallel
(
    connect_gopro "$GOPRO3_MAC" "$GOPRO3_SSID" "$GOPRO3_PSK" "$GOPRO3_IP" "$WLAN1_INTERFACE" "$WPA_CONF1" "GoPro3"
) &
PID1=$!

(
    connect_gopro "$GOPRO1_MAC" "$GOPRO1_SSID" "$GOPRO1_PSK" "$GOPRO1_IP" "$WLAN2_INTERFACE" "$WPA_CONF2" "GoPro3"
) &
PID2=$!

# Wait for both connections to complete
wait $PID1
RESULT1=$?
wait $PID2
RESULT2=$?

if [ $RESULT1 -eq 0 ] && [ $RESULT2 -eq 0 ]; then
    echo "SUCCESS: Both GoPros connected successfully!"
    exit 0
elif [ $RESULT1 -eq 0 ]; then
    echo "PARTIAL: Only GoPro3 connected successfully"
    exit 1
elif [ $RESULT2 -eq 0 ]; then
    echo "PARTIAL: Only GoPro3 connected successfully"
    exit 1
else
    echo "FAILURE: Both GoPro connections failed"
    exit 1
fi