#!/usr/bin/env python3
"""
GoPro Connection Manager - Pure Python Implementation
Replaces the shell scripts with Python code for better integration
"""

import subprocess
import time
import os
import json
import requests
from pathlib import Path
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class GoProConnectionManager:
    def __init__(self):
        # GoPro configurations
        self.gopros = {
            "gopro3": {
                "mac": "D0:21:F8:9C:FF:80",
                "ssid": "HERO8 Achim 3",
                "psk": "5d3-QNv-MTm",
                "ip": "10.5.5.9",
                "interface": "wlan0",
                "name": "GoPro3"
            },
            "gopro1": {
                "mac": "C8:52:0D:A5:9A:39",
                "ssid": "HERO8 Achim 1", 
                "psk": "vDh-p7g-TDj",
                "ip": "10.5.5.9",
                "interface": "wlan1",
                "name": "GoPro1"
            }
        }
        
        # Paths
        self.wpa_conf_dir = "/etc/wpa_supplicant"
        self.python_bin = "/home/pi/gopro-ble-py/gopro-ble-py/venv/bin/python"
        self.ble_tool = "/home/pi/gopro-ble-py/gopro-ble-py/main.py"
        
    def reset_bluetooth(self):
        """Reset Bluetooth adapter"""
        try:
            logger.info("Resetting Bluetooth adapter...")
            subprocess.run(["sudo", "hciconfig", "hci0", "down"], check=True, capture_output=True)
            subprocess.run(["sudo", "hciconfig", "hci0", "up"], check=True, capture_output=True)
            time.sleep(3)
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to reset Bluetooth: {e}")
            return False
    
    def activate_gopro_wifi_ble(self, gopro_id, max_retries=3):
        """Activate GoPro Wi-Fi via BLE with retries"""
        config = self.gopros[gopro_id]
        mac = config["mac"]
        name = config["name"]
        
        logger.info(f"Activating {name} Wi-Fi via BLE...")
        
        for attempt in range(1, max_retries + 1):
            logger.info(f"BLE attempt {attempt}/{max_retries} for {name}...")
            
            try:
                # Wait for BLE advertising
                time.sleep(8)
                
                # Execute BLE command
                result = subprocess.run([
                    self.python_bin, self.ble_tool,
                    "--interactive", "true",
                    "--address", mac,
                    "--command", "wifi on"
                ], timeout=30, capture_output=True, text=True)
                
                if result.returncode == 0:
                    logger.info(f"BLE command succeeded for {name}")
                    return True
                else:
                    logger.warning(f"BLE attempt {attempt} failed for {name}: {result.stderr}")
                    
            except subprocess.TimeoutExpired:
                logger.warning(f"BLE attempt {attempt} timed out for {name}")
            except Exception as e:
                logger.warning(f"BLE attempt {attempt} error for {name}: {e}")
            
            if attempt < max_retries:
                logger.info("Waiting 5 seconds before retry...")
                time.sleep(5)
                self.reset_bluetooth()
        
        logger.error(f"All BLE attempts failed for {name}")
        return False
    
    def create_wpa_supplicant_config(self, interface, ssid, psk):
        """Create or update wpa_supplicant configuration"""
        config_file = os.path.join(self.wpa_conf_dir, f"wpa_supplicant_{interface}.conf")
        
        try:
            # Create base config if it doesn't exist
            if not os.path.exists(config_file):
                logger.info(f"Creating {config_file}")
                base_config = """ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=DE
"""
                with open(config_file, 'w') as f:
                    f.write(base_config)
            
            # Check if network already exists
            with open(config_file, 'r') as f:
                content = f.read()
            
            if f'ssid="{ssid}"' not in content:
                logger.info(f"Adding {ssid} to {config_file}")
                network_block = f"""
network={{
    ssid="{ssid}"
    psk="{psk}"
    key_mgmt=WPA-PSK
}}
"""
                with open(config_file, 'a') as f:
                    f.write(network_block)
            
            return config_file
            
        except Exception as e:
            logger.error(f"Failed to create wpa_supplicant config: {e}")
            return None
    
    def reset_network_interface(self, interface):
        """Reset network interface"""
        try:
            logger.info(f"Resetting network interface {interface}...")
            
            # Kill existing processes for this interface
            subprocess.run(["sudo", "pkill", "-9", "-f", f"wpa_supplicant.*{interface}"], 
                         capture_output=True)
            subprocess.run(["sudo", "pkill", "-9", "-f", f"dhclient.*{interface}"], 
                         capture_output=True)
            
            # Remove control interface file
            control_file = f"/var/run/wpa_supplicant/{interface}"
            if os.path.exists(control_file):
                subprocess.run(["sudo", "rm", "-f", control_file], capture_output=True)
            
            # Reset interface
            subprocess.run(["sudo", "ip", "link", "set", interface, "down"], check=True)
            subprocess.run(["sudo", "ip", "addr", "flush", "dev", interface], capture_output=True)
            time.sleep(2)
            subprocess.run(["sudo", "ip", "link", "set", interface, "up"], check=True)
            time.sleep(3)
            
            return True
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to reset interface {interface}: {e}")
            return False
    
    def connect_wifi(self, interface, config_file):
        """Connect to Wi-Fi using wpa_supplicant"""
        try:
            logger.info(f"Starting wpa_supplicant for {interface}...")
            
            # Start wpa_supplicant
            subprocess.run([
                "sudo", "wpa_supplicant", "-B", "-i", interface, "-c", config_file
            ], check=True, capture_output=True)
            
            # Wait for connection
            time.sleep(5)
            
            # Request DHCP lease
            logger.info(f"Requesting DHCP lease for {interface}...")
            result = subprocess.run([
                "timeout", "15", "sudo", "dhclient", interface
            ], capture_output=True)
            
            return True
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to connect Wi-Fi on {interface}: {e}")
            return False
    
    def assign_static_ip(self, interface, ip_suffix):
        """Assign static IP if DHCP fails"""
        try:
            # Check if we got an IP
            result = subprocess.run([
                "ip", "addr", "show", interface
            ], capture_output=True, text=True)
            
            if "inet 10.5.5." not in result.stdout:
                logger.info(f"DHCP failed for {interface}, assigning static IP...")
                static_ip = f"10.5.5.{ip_suffix}/24"
                subprocess.run([
                    "sudo", "ip", "addr", "add", static_ip, "dev", interface
                ], check=True)
                return True
            
            return True
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to assign static IP to {interface}: {e}")
            return False
    
    def add_route(self, interface, target_ip):
        """Add route to GoPro"""
        try:
            subprocess.run([
                "sudo", "ip", "route", "replace", f"{target_ip}/32", "dev", interface
            ], capture_output=True)
            return True
        except:
            return False
    
    def test_gopro_connection(self, gopro_id, timeout=10):
        """Test GoPro HTTP API connection"""
        config = self.gopros[gopro_id]
        ip = config["ip"]
        interface = config["interface"]
        name = config["name"]
        
        try:
            # Use requests with a custom adapter for interface binding
            session = requests.Session()
            
            # For interface binding, we'll use curl as it's more reliable
            # In production, you might want to use a library like requests-unixsocket
            # or configure routing tables differently
            
            for attempt in range(1, 6):
                try:
                    result = subprocess.run([
                        "curl", "-m", str(timeout), "--interface", interface,
                        "-s", "-o", "/dev/null", "-w", "%{http_code}",
                        f"http://{ip}/gp/gpControl/status"
                    ], capture_output=True, text=True, timeout=timeout+5)
                    
                    if result.stdout.strip() == "200":
                        logger.info(f"Success: {name} API reachable with HTTP 200")
                        return True
                    else:
                        logger.warning(f"Attempt {attempt}/5: HTTP={result.stdout.strip()}, retrying...")
                        
                except subprocess.TimeoutExpired:
                    logger.warning(f"Attempt {attempt}/5: Timeout, retrying...")
                
                if attempt < 5:
                    time.sleep(3)
            
            logger.error(f"{name} connection failed after 5 attempts")
            return False
            
        except Exception as e:
            logger.error(f"Error testing {name} connection: {e}")
            return False
    
    def connect_single_gopro(self, gopro_id):
        """Connect to a single specific GoPro"""
        config = self.gopros[gopro_id]
        interface = config["interface"]
        name = config["name"]
        
        logger.info(f"=== Connecting to {name} only ===")
        
        # Check if interface exists
        result = subprocess.run(["ip", "link", "show", interface], capture_output=True)
        if result.returncode != 0:
            logger.error(f"Interface {interface} not found")
            return False
        
        # Reset Bluetooth
        if not self.reset_bluetooth():
            return False
        
        # Reset network interface
        if not self.reset_network_interface(interface):
            return False
        
        # Activate GoPro Wi-Fi via BLE
        if not self.activate_gopro_wifi_ble(gopro_id):
            return False
        
        # Wait for Wi-Fi to start
        logger.info(f"Waiting 5s for {name} Wi-Fi to start...")
        time.sleep(5)
        
        # Create wpa_supplicant config
        config_file = self.create_wpa_supplicant_config(
            interface, config["ssid"], config["psk"]
        )
        if not config_file:
            return False
        
        # Connect to Wi-Fi
        if not self.connect_wifi(interface, config_file):
            return False
        
        # Assign IP (static if DHCP fails)
        ip_suffix = "100" if interface == "wlan0" else "101"
        if not self.assign_static_ip(interface, ip_suffix):
            return False
        
        # Add route
        self.add_route(interface, config["ip"])
        
        # Test connection
        return self.test_gopro_connection(gopro_id)
    
    def connect_dual_gopros_sequential(self):
        """Connect to both GoPros sequentially"""
        logger.info("Starting SEQUENTIAL dual GoPro connection process...")
        
        # Check if both interfaces exist
        for gopro_id, config in self.gopros.items():
            interface = config["interface"]
            result = subprocess.run(["ip", "link", "show", interface], capture_output=True)
            if result.returncode != 0:
                logger.error(f"Interface {interface} not found for {config['name']}")
                return False
        
        # Clean up existing processes
        logger.info("Cleaning up existing network processes...")
        subprocess.run(["sudo", "pkill", "-9", "-f", "wpa_supplicant.*wlan"], capture_output=True)
        subprocess.run(["sudo", "pkill", "-9", "-f", "dhclient.*wlan"], capture_output=True)
        
        # Clean up control interface files
        for interface in ["wlan0", "wlan1"]:
            control_file = f"/var/run/wpa_supplicant/{interface}"
            subprocess.run(["sudo", "rm", "-f", control_file], capture_output=True)
        
        # Reset Bluetooth
        logger.info("Resetting Bluetooth to clear stuck connections...")
        subprocess.run(["sudo", "systemctl", "restart", "bluetooth"], capture_output=True)
        time.sleep(3)
        
        # Reset both interfaces
        for interface in ["wlan0", "wlan1"]:
            subprocess.run(["sudo", "ip", "link", "set", interface, "down"], capture_output=True)
        time.sleep(2)
        for interface in ["wlan0", "wlan1"]:
            subprocess.run(["sudo", "ip", "link", "set", interface, "up"], capture_output=True)
        time.sleep(3)
        
        # Connect GoPros sequentially
        results = {}
        
        # First GoPro3
        logger.info("STEP 1: Connecting to GoPro3 first...")
        results["gopro3"] = self.connect_single_gopro("gopro3")
        
        # Then GoPro1
        logger.info("STEP 2: Now connecting to GoPro1...")
        time.sleep(3)  # Small delay between connections
        results["gopro1"] = self.connect_single_gopro("gopro1")
        
        # Report results
        logger.info("=== CONNECTION SUMMARY ===")
        if all(results.values()):
            logger.info("SUCCESS: Both GoPros connected successfully!")
            logger.info("GoPro3: Connected via wlan0")
            logger.info("GoPro1: Connected via wlan1")
            return True
        elif results.get("gopro3"):
            logger.warning("PARTIAL: Only GoPro3 connected successfully via wlan0")
            logger.error("GoPro1: Connection failed")
            return False
        elif results.get("gopro1"):
            logger.warning("PARTIAL: Only GoPro1 connected successfully via wlan1")
            logger.error("GoPro3: Connection failed")
            return False
        else:
            logger.error("FAILURE: Both GoPro connections failed")
            return False
    
    def is_gopro_connected(self, gopro_id, timeout=2):
        """Check if a specific GoPro is connected"""
        config = self.gopros[gopro_id]
        ip = config["ip"]
        interface = config["interface"]
        name = config["name"]
        
        try:
            result = subprocess.run([
                "curl", f"-m{timeout}", "--interface", interface,
                "-s", "-o", "/dev/null", "-w", "%{http_code}",
                f"http://{ip}/gp/gpControl/status"
            ], capture_output=True, text=True)
            
            is_connected = result.stdout.strip() == "200"
            logger.debug(f"{interface} -> {ip}: HTTP {result.stdout.strip()}, Connected: {is_connected}")
            
            return is_connected
            
        except Exception as e:
            logger.debug(f"{interface} connection test error: {e}")
            return False
    
    def check_all_gopros_connected(self):
        """Check connection status of all GoPros"""
        connected_gopros = {}
        
        for gopro_id, config in self.gopros.items():
            if self.is_gopro_connected(gopro_id):
                logger.info(f"{config['name']} is connected")
                connected_gopros[gopro_id] = True
            else:
                logger.warning(f"{config['name']} is not connected")
                connected_gopros[gopro_id] = False
        
        all_connected = all(connected_gopros.values())
        return all_connected, connected_gopros


# Integration class for the main recording script
class GoProHTTPController:
    """HTTP-based GoPro controller to replace curl commands"""
    
    def __init__(self, ip, interface, name):
        self.ip = ip
        self.interface = interface
        self.name = name
        self.base_url = f"http://{ip}"
        self.cam_port = 8080
    
    def _make_request(self, endpoint, timeout=5):
        """Make HTTP request using curl with interface binding"""
        try:
            url = f"{self.base_url}{endpoint}"
            result = subprocess.run([
                "curl", f"-m{timeout}", "--interface", self.interface,
                "-s", "-o", "/dev/null", "-w", "%{http_code}", url
            ], capture_output=True, text=True)
            
            return result.stdout.strip() == "200"
        except:
            return False
    
    def start_recording(self):
        """Start recording"""
        return self._make_request("/gp/gpControl/command/shutter?p=1")
    
    def stop_recording(self):
        """Stop recording"""
        return self._make_request("/gp/gpControl/command/shutter?p=0")
    
    def get_media_list(self):
        """Get media list from GoPro"""
        try:
            result = subprocess.run([
                "curl", "-m10", "--interface", self.interface,
                "-s", f"{self.base_url}/gp/gpMediaList"
            ], capture_output=True, text=True)
            
            if result.returncode == 0:
                return json.loads(result.stdout)
            return None
        except:
            return None
    
    def delete_file(self, filename):
        """Delete file from GoPro"""
        delete_url = f"/gp/gpControl/command/storage/delete?p=/100GOPRO/{filename}"
        return self._make_request(delete_url, timeout=10)
    
    def download_file(self, filename, local_path):
        """Download file from GoPro"""
        try:
            camera_url = f"http://{self.ip}:{self.cam_port}/videos/DCIM/100GOPRO/{filename}"
            
            result = subprocess.run([
                "curl", "-#", "-m600", "--connect-timeout", "30",
                "--speed-time", "60", "--speed-limit", "1024",
                "--interface", self.interface,
                "-o", local_path, camera_url
            ], capture_output=False)  # Show progress
            
            return result.returncode == 0 and os.path.exists(local_path)
        except:
            return False


# Example usage and integration with the main script
def integrate_with_main_script():
    """
    Example of how to integrate this with your main recording script
    """
    
    # Replace the shell script calls in your main script with:
    connection_manager = GoProConnectionManager()
    
    # Instead of run_single_gopro_connect(gopro_id):
    def run_single_gopro_connect(gopro_id):
        return connection_manager.connect_single_gopro(gopro_id)
    
    # Instead of run_connect_script():
    def run_connect_script():
        return connection_manager.connect_dual_gopros_sequential()
    
    # Instead of is_gopro_connected(ip, interface, timeout):
    def is_gopro_connected(gopro_id, timeout=2):
        return connection_manager.is_gopro_connected(gopro_id, timeout)
    
    # Replace GoProController class methods with GoProHTTPController
    
    return connection_manager


if __name__ == "__main__":
    # Test the connection manager
    manager = GoProConnectionManager()
    
    # Test single connection
    # success = manager.connect_single_gopro("gopro3")
    
    # Test dual connection
    success = manager.connect_dual_gopros_sequential()
    
    if success:
        print("Connection successful!")
        
        # Test API calls
        all_connected, status = manager.check_all_gopros_connected()
        print(f"All connected: {all_connected}")
        print(f"Status: {status}")
    else:
        print("Connection failed!")
