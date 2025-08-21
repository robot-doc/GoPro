#!/usr/bin/env bash
# connect_dual_gopro_sequential.sh
# Modified to handle two GoPros with two Wi-Fi adapters - SEQUENTIAL CONNECTION
# Copy to /usr/local/bin and set executable:
#   sudo mv connect_dual_gopro_sequential.sh /usr/local/bin/
#   sudo chmod +x /usr/local/bin/connect_dual_gopro_sequential.sh
# Run: sudo connect_dual_gopro_sequential.sh

# GoPro 1 Config
GOPRO1_MAC="C8:52:0D:A5:9A:39"
GOPRO1_SSID="HERO8 Achim 1"
GOPRO1_PSK="vDh-p7g-TDj"
GOPRO1_IP="10.5.5.9"
WLAN2_INTERFACE="wlan1"

# GoPro 3 Config
GOPRO3_MAC="D0:21:F8:9C:FF:80"
GOPRO3_SSID="HERO8 Achim 3"
GOPRO3_PSK="5d3-QNv-MTm"
GOPRO3_IP="10.5.5.9"
WLAN1_INTERFACE="wlan0"

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
    echo "Waiting 8s for $GOPRO_NAME to advertise BLE..."
    sleep 8

    # 2. Enable Wi-Fi via Python BLE client with retries
    echo "Activating $GOPRO_NAME Wi-Fi via BLE Python client..."
    
    # Try BLE connection up to 3 times
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
                # Reset Bluetooth between attempts
                sudo hciconfig hci0 down && sudo hciconfig hci0 up
                sleep 2
            fi
        fi
    done
    
    if [ $BLE_SUCCESS -eq 0 ]; then
        echo "Error: All BLE Wi-Fi ON attempts failed for $GOPRO_NAME." >&2
        return 1
    fi

    # 3. Wait a moment for GoPro to start Wi-Fi
    echo "Waiting 5s for $GOPRO_NAME Wi-Fi to start..."
    sleep 5

    # 4. Configure wlan interface
    echo "Configuring $WLAN_INTERFACE for $GOPRO_NAME AP..."
    
    # Kill any existing processes for this specific interface
    sudo pkill -9 -f "wpa_supplicant.*$WLAN_INTERFACE" || true
    sudo pkill -9 -f "dhclient.*$WLAN_INTERFACE" || true
    
    # Remove control interface file for this specific interface
    sudo rm -f "/var/run/wpa_supplicant/$WLAN_INTERFACE" || true
    
    # Reset interface completely
    sudo ip link set $WLAN_INTERFACE down
    sudo ip addr flush dev $WLAN_INTERFACE || true
    sleep 2
    sudo ip link set $WLAN_INTERFACE up
    sleep 3
    
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
    
    # Start wpa_supplicant for this interface
    echo "Starting wpa_supplicant for $WLAN_INTERFACE..."
    sudo wpa_supplicant -B -i $WLAN_INTERFACE -c "${WPA_CONF}"
    
    # Wait for connection
    echo "Waiting for WiFi connection to establish..."
    sleep 5
    
    # Request DHCP lease
    echo "Requesting DHCP lease for $WLAN_INTERFACE (15s timeout)..."
    timeout 15 sudo dhclient $WLAN_INTERFACE
    
    # Check if we got IP, if not assign static
    if ! ip addr show $WLAN_INTERFACE | grep -q "inet 10\.5\.5\."; then
        echo "DHCP failed for $WLAN_INTERFACE, assigning static IP..."
        # Use different static IPs to avoid conflicts
        if [ "$WLAN_INTERFACE" = "wlan0" ]; then
            sudo ip addr add 10.5.5.100/24 dev $WLAN_INTERFACE
        else
            sudo ip addr add 10.5.5.101/24 dev $WLAN_INTERFACE
        fi
    fi

    # 5. Add route
    echo "Adding route to $GOPRO_NAME..."
    sudo ip route replace ${GOPRO_IP}/32 dev $WLAN_INTERFACE || true

    # 6. Verify HTTP
    echo "Verifying $GOPRO_NAME HTTP API..."
    sleep 3
    for i in 1 2 3 4 5; do
        code=$(curl -m10 --interface $WLAN_INTERFACE -s -o /dev/null -w "%{http_code}" "http://${GOPRO_IP}/gp/gpControl/status")
        if [ "$code" = "200" ]; then
            echo "Success: $GOPRO_NAME API reachable with HTTP $code"
            return 0
        fi
        echo "Attempt $i/5: HTTP=$code, retrying in 3s..."
        sleep 3
    done

    echo "Error: $GOPRO_NAME connection failed after 5 attempts" >&2
    return 1
}

# Main execution
echo "Starting SEQUENTIAL dual GoPro connection process..."

# Check if both interfaces exist
if ! ip link show $WLAN1_INTERFACE >/dev/null 2>&1; then
    echo "Error: $WLAN1_INTERFACE not found. Please ensure your first Wi-Fi adapter is connected." >&2
    exit 1
fi

if ! ip link show $WLAN2_INTERFACE >/dev/null 2>&1; then
    echo "Error: $WLAN2_INTERFACE not found. Please ensure your second Wi-Fi adapter is connected." >&2
    exit 1
fi

# Kill any existing wpa_supplicant and dhclient processes to start fresh
echo "Cleaning up existing network processes..."
sudo pkill -9 -f "wpa_supplicant.*wlan" || true
sudo pkill -9 -f "dhclient.*wlan" || true

# Clean up control interface files
sudo rm -f /var/run/wpa_supplicant/wlan0 || true
sudo rm -f /var/run/wpa_supplicant/wlan1 || true

# Reset Bluetooth to clear any stuck connections
echo "Resetting Bluetooth to clear stuck connections..."
sudo systemctl restart bluetooth
sleep 3

# Bring both interfaces down and up to reset them
sudo ip link set wlan0 down || true
sudo ip link set wlan1 down || true
sleep 2
sudo ip link set wlan0 up || true
sudo ip link set wlan1 up || true
sleep 3

# Connect to GoPros SEQUENTIALLY (one after the other)
echo ""
echo "STEP 1: Connecting to GoPro3 first..."
connect_gopro "$GOPRO3_MAC" "$GOPRO3_SSID" "$GOPRO3_PSK" "$GOPRO3_IP" "$WLAN1_INTERFACE" "$WPA_CONF1" "GoPro3"
RESULT1=$?

echo ""
echo "STEP 2: Now connecting to GoPro1..."
sleep 3  # Small delay between connections
connect_gopro "$GOPRO1_MAC" "$GOPRO1_SSID" "$GOPRO1_PSK" "$GOPRO1_IP" "$WLAN2_INTERFACE" "$WPA_CONF2" "GoPro1"
RESULT2=$?

# Report final results
echo ""
echo "=== CONNECTION SUMMARY ==="
if [ $RESULT1 -eq 0 ] && [ $RESULT2 -eq 0 ]; then
    echo "SUCCESS: Both GoPros connected successfully!"
    echo "GoPro3: Connected via $WLAN1_INTERFACE"
    echo "GoPro1: Connected via $WLAN2_INTERFACE"
    exit 0
elif [ $RESULT1 -eq 0 ]; then
    echo "PARTIAL: Only GoPro3 connected successfully via $WLAN1_INTERFACE"
    echo "GoPro1: Connection failed"
    exit 1
elif [ $RESULT2 -eq 0 ]; then
    echo "PARTIAL: Only GoPro1 connected successfully via $WLAN2_INTERFACE"
    echo "GoPro1: Connection failed"
    exit 1
else
    echo "FAILURE: Both GoPro connections failed"
    exit 1
fi