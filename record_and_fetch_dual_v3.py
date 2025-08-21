#!/usr/bin/env python3
"""
Complete Integrated record_and_fetch_dual_v2.py
No external shell scripts required - all connection management integrated
"""

import time, os, sys, json, subprocess
from datetime import datetime, timezone
import sys
from pcf8574 import PCF8574
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import tempfile
import queue
import logging
from pathlib import Path

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
RECORD_SECS    = 38
FINALIZE_SECS  = 2
DOWNLOAD_DIR   = "/home/pi/GoPro_Clips"
COMBINED_DIR   = "/home/pi/GoPro_Clips/Combined"
FALLBACK_DIR   = "/home/pi/GoPro_Clips_Backup"
CAM_PORT       = 8080
LAST_CLIP_FILE = os.path.join(DOWNLOAD_DIR, ".last_clip")

# GoPro configurations
GOPROS = {
    "gopro3": {
        "ip": "10.5.5.9",
        "interface": "wlan0",
        "name": "GoPro3",
        "download_subdir": "GoPro3"
    },
    "gopro1": {
        "ip": "10.5.5.9",
        "interface": "wlan1",
        "name": "GoPro1", 
        "download_subdir": "GoPro1"
    }
}

# I2C Configuration
I2C_BUS                 = 1
I2C_ADDR_OUTPUT         = 0x20
I2C_ADDR_INPUT          = 0x38
START_REC_INPUT_PIN     = 0
CAM_BUSY_OUTPUT_PIN     = 0 
POLL_INTERVAL           = 0.05
DEBOUNCE_MS             = 200

# Global queue for video combination tasks
combination_queue = queue.Queue()
combination_thread = None

# ============================================================================
# INTEGRATED CONNECTION MANAGER (replaces shell scripts)
# ============================================================================

class GoProConnectionManager:
    def __init__(self):
        # GoPro configurations with connection details
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
        
        print(f"Activating {name} Wi-Fi via BLE...")
        
        for attempt in range(1, max_retries + 1):
            print(f"BLE attempt {attempt}/{max_retries} for {name}...")
            
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
                    print(f"BLE command succeeded for {name}")
                    return True
                else:
                    print(f"BLE attempt {attempt} failed for {name}: {result.stderr}")
                    
            except subprocess.TimeoutExpired:
                print(f"BLE attempt {attempt} timed out for {name}")
            except Exception as e:
                print(f"BLE attempt {attempt} error for {name}: {e}")
            
            if attempt < max_retries:
                print("Waiting 5 seconds before retry...")
                time.sleep(5)
                self.reset_bluetooth()
        
        print(f"Error: All BLE attempts failed for {name}")
        return False
    
    def create_wpa_supplicant_config(self, interface, ssid, psk):
        """Create or update wpa_supplicant configuration"""
        config_file = os.path.join(self.wpa_conf_dir, f"wpa_supplicant_{interface}.conf")
        
        try:
            # Create base config if it doesn't exist
            if not os.path.exists(config_file):
                print(f"Creating {config_file}")
                base_config = """ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=DE
"""
                subprocess.run(["sudo", "tee", config_file], input=base_config, text=True, capture_output=True)
            
            # Check if network already exists
            result = subprocess.run(["sudo", "cat", config_file], capture_output=True, text=True)
            content = result.stdout
            
            if f'ssid="{ssid}"' not in content:
                print(f"Adding {ssid} to {config_file}")
                network_block = f"""
network={{
    ssid="{ssid}"
    psk="{psk}"
    key_mgmt=WPA-PSK
}}
"""
                subprocess.run(["sudo", "tee", "-a", config_file], input=network_block, text=True, capture_output=True)
            
            return config_file
            
        except Exception as e:
            print(f"Failed to create wpa_supplicant config: {e}")
            return None
    
    def reset_network_interface(self, interface):
        """Reset network interface"""
        try:
            print(f"Resetting network interface {interface}...")
            
            # Kill existing processes for this interface
            subprocess.run(["sudo", "pkill", "-9", "-f", f"wpa_supplicant.*{interface}"], 
                         capture_output=True)
            subprocess.run(["sudo", "pkill", "-9", "-f", f"dhclient.*{interface}"], 
                         capture_output=True)
            
            # Remove control interface file
            control_file = f"/var/run/wpa_supplicant/{interface}"
            subprocess.run(["sudo", "rm", "-f", control_file], capture_output=True)
            
            # Reset interface
            subprocess.run(["sudo", "ip", "link", "set", interface, "down"], check=True)
            subprocess.run(["sudo", "ip", "addr", "flush", "dev", interface], capture_output=True)
            time.sleep(2)
            subprocess.run(["sudo", "ip", "link", "set", interface, "up"], check=True)
            time.sleep(3)
            
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"Failed to reset interface {interface}: {e}")
            return False
    
    def connect_wifi(self, interface, config_file):
        """Connect to Wi-Fi using wpa_supplicant"""
        try:
            print(f"Starting wpa_supplicant for {interface}...")
            
            # Start wpa_supplicant
            subprocess.run([
                "sudo", "wpa_supplicant", "-B", "-i", interface, "-c", config_file
            ], check=True, capture_output=True)
            
            # Wait for connection
            print("Waiting for WiFi connection to establish...")
            time.sleep(5)
            
            # Request DHCP lease
            print(f"Requesting DHCP lease for {interface} (15s timeout)...")
            subprocess.run([
                "timeout", "15", "sudo", "dhclient", interface
            ], capture_output=True)
            
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"Failed to connect Wi-Fi on {interface}: {e}")
            return False
    
    def assign_static_ip(self, interface, ip_suffix):
        """Assign static IP if DHCP fails"""
        try:
            # Check if we got an IP
            result = subprocess.run([
                "ip", "addr", "show", interface
            ], capture_output=True, text=True)
            
            if "inet 10.5.5." not in result.stdout:
                print(f"DHCP failed for {interface}, assigning static IP...")
                static_ip = f"10.5.5.{ip_suffix}/24"
                subprocess.run([
                    "sudo", "ip", "addr", "add", static_ip, "dev", interface
                ], check=True)
            
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"Failed to assign static IP to {interface}: {e}")
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
            print(f"Verifying {name} HTTP API...")
            time.sleep(3)
            
            for attempt in range(1, 6):
                try:
                    result = subprocess.run([
                        "curl", f"-m{timeout}", "--interface", interface,
                        "-s", "-o", "/dev/null", "-w", "%{http_code}",
                        f"http://{ip}/gp/gpControl/status"
                    ], capture_output=True, text=True, timeout=timeout+5)
                    
                    if result.stdout.strip() == "200":
                        print(f"Success: {name} API reachable with HTTP 200")
                        return True
                    else:
                        print(f"Attempt {attempt}/5: HTTP={result.stdout.strip()}, retrying in 3s...")
                        
                except subprocess.TimeoutExpired:
                    print(f"Attempt {attempt}/5: Timeout, retrying...")
                
                if attempt < 5:
                    time.sleep(3)
            
            print(f"Error: {name} connection failed after 5 attempts")
            return False
            
        except Exception as e:
            print(f"Error testing {name} connection: {e}")
            return False
    
    def connect_single_gopro(self, gopro_id):
        """Connect to a single specific GoPro"""
        config = self.gopros[gopro_id]
        interface = config["interface"]
        name = config["name"]
        
        print(f"=== Connecting to {name} only ===")
        
        # Check if interface exists
        result = subprocess.run(["ip", "link", "show", interface], capture_output=True)
        if result.returncode != 0:
            print(f"Error: Interface {interface} not found")
            return False
        
        # Clean up this specific interface
        print(f"Cleaning up {interface}...")
        subprocess.run(["sudo", "pkill", "-9", "-f", f"wpa_supplicant.*{interface}"], capture_output=True)
        subprocess.run(["sudo", "pkill", "-9", "-f", f"dhclient.*{interface}"], capture_output=True)
        subprocess.run(["sudo", "rm", "-f", f"/var/run/wpa_supplicant/{interface}"], capture_output=True)
        
        # Reset Bluetooth
        print("Resetting Bluetooth...")
        if not self.reset_bluetooth():
            return False
        
        # Reset network interface
        if not self.reset_network_interface(interface):
            return False
        
        # Activate GoPro Wi-Fi via BLE
        print(f"Waiting 8s for {name} to advertise BLE...")
        if not self.activate_gopro_wifi_ble(gopro_id):
            return False
        
        # Wait for Wi-Fi to start
        print(f"Waiting 5s for {name} Wi-Fi to start...")
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
        print(f"Adding route to {name}...")
        self.add_route(interface, config["ip"])
        
        # Test connection
        if self.test_gopro_connection(gopro_id):
            print(f"SUCCESS: {name} connected and reachable!")
            return True
        else:
            print(f"ERROR: {name} connection failed")
            return False
    
    def connect_dual_gopros_sequential(self):
        """Connect to both GoPros sequentially"""
        print("Starting SEQUENTIAL dual GoPro connection process...")
        
        # Check if both interfaces exist
        for gopro_id, config in self.gopros.items():
            interface = config["interface"]
            result = subprocess.run(["ip", "link", "show", interface], capture_output=True)
            if result.returncode != 0:
                print(f"Error: {interface} not found. Please ensure your Wi-Fi adapter is connected.")
                return False
        
        # Clean up existing processes
        print("Cleaning up existing network processes...")
        subprocess.run(["sudo", "pkill", "-9", "-f", "wpa_supplicant.*wlan"], capture_output=True)
        subprocess.run(["sudo", "pkill", "-9", "-f", "dhclient.*wlan"], capture_output=True)
        
        # Clean up control interface files
        for interface in ["wlan0", "wlan1"]:
            subprocess.run(["sudo", "rm", "-f", f"/var/run/wpa_supplicant/{interface}"], capture_output=True)
        
        # Reset Bluetooth
        print("Resetting Bluetooth to clear stuck connections...")
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
        print("\nSTEP 1: Connecting to GoPro3 first...")
        results["gopro3"] = self.connect_single_gopro("gopro3")
        
        # Then GoPro1
        print("\nSTEP 2: Now connecting to GoPro1...")
        time.sleep(3)  # Small delay between connections
        results["gopro1"] = self.connect_single_gopro("gopro1")
        
        # Report results
        print("\n=== CONNECTION SUMMARY ===")
        if all(results.values()):
            print("SUCCESS: Both GoPros connected successfully!")
            print("GoPro3: Connected via wlan0")
            print("GoPro1: Connected via wlan1")
            return True
        elif results.get("gopro3"):
            print("PARTIAL: Only GoPro3 connected successfully via wlan0")
            print("GoPro1: Connection failed")
            return False
        elif results.get("gopro1"):
            print("PARTIAL: Only GoPro1 connected successfully via wlan1")
            print("GoPro3: Connection failed")
            return False
        else:
            print("FAILURE: Both GoPro connections failed")
            return False
    
    def is_gopro_connected(self, gopro_id, timeout=2):
        """Check if a specific GoPro is connected"""
        config = self.gopros[gopro_id]
        ip = config["ip"]
        interface = config["interface"]
        
        try:
            result = subprocess.run([
                "curl", f"-m{timeout}", "--interface", interface,
                "-s", "-o", "/dev/null", "-w", "%{http_code}",
                f"http://{ip}/gp/gpControl/status"
            ], capture_output=True, text=True)
            
            is_connected = result.stdout.strip() == "200"
            print(f"[DEBUG] {interface} -> {ip}: HTTP {result.stdout.strip()}, Connected: {is_connected}")
            
            return is_connected
            
        except Exception as e:
            print(f"[DEBUG] {interface} connection test error: {e}")
            return False
    
    def check_all_gopros_connected(self):
        """Check connection status of all GoPros"""
        connected_gopros = {}
        
        for gopro_id, config in self.gopros.items():
            if self.is_gopro_connected(gopro_id):
                print(f"[INFO] {config['name']} is connected")
                connected_gopros[gopro_id] = True
            else:
                print(f"[WARNING] {config['name']} is not connected")
                connected_gopros[gopro_id] = False
        
        all_connected = all(connected_gopros.values())
        return all_connected, connected_gopros

# ============================================================================
# CONNECTION WRAPPER FUNCTIONS (replace shell script calls)
# ============================================================================

# Initialize connection manager
connection_manager = GoProConnectionManager()

def is_gopro_connected(ip, interface, timeout=2):
    """Check if GoPro is reachable via specific network interface"""
    # Map interface to gopro_id for the connection manager
    gopro_id = "gopro3" if interface == "wlan0" else "gopro1"
    return connection_manager.is_gopro_connected(gopro_id, timeout)

def run_single_gopro_connect(gopro_id):
    """Connect to a single specific GoPro"""
    try:
        return connection_manager.connect_single_gopro(gopro_id)
    except Exception as e:
        print(f"[ERROR] Single GoPro connection error: {e}")
        return False

def run_connect_script():
    """Run the sequential dual GoPro connection script"""
    try:
        print("[INFO] Running integrated Python connection manager...")
        return connection_manager.connect_dual_gopros_sequential()
    except Exception as e:
        print(f"[ERROR] Dual connection error: {e}")
        return False

def check_all_gopros_connected():
    """Check if all GoPros are connected"""
    return connection_manager.check_all_gopros_connected()

# ============================================================================
# STORAGE AND UTILITY FUNCTIONS
# ============================================================================

def check_storage_availability():
    """Check if external SSD is mounted and writable, setup fallback if needed"""
    try:
        # Check if the USB mount point exists
        if os.path.exists("/home/pi"):
            # Test write access
            test_file = "/home/pi/.write_test"
            try:
                with open(test_file, 'w') as f:
                    f.write("test")
                os.remove(test_file)
                print("[INFO] External SSD accessible and writable at /home/pi")
                
                # Create the full directory structure
                full_download_dir = "/home/pi/GoPro_Clips" 
                full_combined_dir = "/home/pi/GoPro_Clips/Combined"
                os.makedirs(full_download_dir, exist_ok=True)
                os.makedirs(full_combined_dir, exist_ok=True)
                
                return full_download_dir, full_combined_dir
            except (IOError, OSError) as e:
                print(f"[WARNING] External SSD accessible but not writable: {e}")
        else:
            print("[WARNING] External SSD path /home/pi does not exist")
            
    except Exception as e:
        print(f"[WARNING] Error checking external SSD: {e}")
    
    # Fallback to local storage
    print(f"[INFO] Using fallback storage: {FALLBACK_DIR}")
    fallback_combined = os.path.join(FALLBACK_DIR, "Combined")
    os.makedirs(FALLBACK_DIR, exist_ok=True)
    os.makedirs(fallback_combined, exist_ok=True)
    return FALLBACK_DIR, fallback_combined

def get_available_space_gb(path):
    """Get available space in GB for given path"""
    try:
        statvfs = os.statvfs(path)
        available_bytes = statvfs.f_frsize * statvfs.f_bavail
        return available_bytes / (1024**3)  # Convert to GB
    except:
        return 0

def check_ffmpeg_installed():
    """Check if ffmpeg is installed"""
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        return result.returncode == 0
    except FileNotFoundError:
        return False

def get_video_duration(video_path):
    """Get video duration in seconds using ffprobe"""
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
            "-of", "csv=p=0", video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            duration = float(result.stdout.strip())
            return duration
        else:
            return None
    except Exception as e:
        print(f"[ERROR] Failed to get video duration: {e}")
        return None

# ============================================================================
# METADATA CREATION FUNCTIONS
# ============================================================================

def create_metadata_file(video_path, download_time_sec=None, video_duration_sec=None, file_size_mb=None, raspberry_timestamp=None):
    """Create a metadata text file for a video"""
    try:
        metadata_path = video_path.replace('.mp4', '_metadata.txt').replace('.MP4', '_metadata.txt')
        
        # Get video duration if not provided
        if video_duration_sec is None:
            video_duration_sec = get_video_duration(video_path)
        
        # Get file size if not provided
        if file_size_mb is None and os.path.exists(video_path):
            file_size_mb = os.path.getsize(video_path) / (1024*1024)
        
        # Use provided timestamp or current time
        if raspberry_timestamp is None:
            raspberry_timestamp = datetime.now()
        
        # Create metadata content
        with open(metadata_path, 'w') as f:
            f.write(f"Video Metadata\n")
            f.write(f"=" * 50 + "\n")
            f.write(f"File: {os.path.basename(video_path)}\n")
            f.write(f"Created (Raspberry Time): {raspberry_timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"File Size: {file_size_mb:.1f} MB\n" if file_size_mb else "File Size: Unknown\n")
            
            if video_duration_sec:
                minutes = int(video_duration_sec // 60)
                seconds = video_duration_sec % 60
                f.write(f"Video Duration: {minutes:02d}:{seconds:05.2f} ({video_duration_sec:.2f} seconds)\n")
            else:
                f.write(f"Video Duration: Unknown\n")
            
            if download_time_sec:
                download_minutes = int(download_time_sec // 60)
                download_secs = download_time_sec % 60
                f.write(f"Download Time: {download_minutes:02d}:{download_secs:05.2f} ({download_time_sec:.2f} seconds)\n")
                
                if file_size_mb and download_time_sec > 0:
                    speed_mbps = (file_size_mb * 8) / download_time_sec  # Convert to Mbps
                    f.write(f"Download Speed: {speed_mbps:.2f} Mbps ({file_size_mb/download_time_sec:.2f} MB/s)\n")
            
            f.write(f"Timestamp Source: Raspberry Pi (not GoPro)\n")
            f.write(f"\n")
        
        print(f"[INFO] Created metadata file: {metadata_path}")
        return metadata_path
        
    except Exception as e:
        print(f"[ERROR] Failed to create metadata file: {e}")
        return None

def create_combined_metadata_file(combined_path, video1_path, video2_path, combine_time_sec, combined_size_mb):
    """Create metadata file for combined video"""
    try:
        metadata_path = combined_path.replace('.mp4', '_metadata.txt').replace('.MP4', '_metadata.txt')
        
        # Get video durations and sizes
        video1_duration = get_video_duration(video1_path)
        video2_duration = get_video_duration(video2_path)
        combined_duration = get_video_duration(combined_path)
        
        video1_size_mb = os.path.getsize(video1_path) / (1024*1024) if os.path.exists(video1_path) else 0
        video2_size_mb = os.path.getsize(video2_path) / (1024*1024) if os.path.exists(video2_path) else 0
        
        with open(metadata_path, 'w') as f:
            f.write(f"Combined Video Metadata\n")
            f.write(f"=" * 50 + "\n")
            f.write(f"Combined File: {os.path.basename(combined_path)}\n")
            f.write(f"Created (Raspberry Time): {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Combined File Size: {combined_size_mb:.1f} MB\n")
            
            if combined_duration:
                minutes = int(combined_duration // 60)
                seconds = combined_duration % 60
                f.write(f"Combined Duration: {minutes:02d}:{seconds:05.2f} ({combined_duration:.2f} seconds)\n")
            
            combine_minutes = int(combine_time_sec // 60)
            combine_secs = combine_time_sec % 60
            f.write(f"Combination Time: {combine_minutes:02d}:{combine_secs:05.2f} ({combine_time_sec:.2f} seconds)\n")
            
            f.write(f"Timestamp Source: Raspberry Pi (not GoPro)\n")
            
            f.write(f"\nSource Videos:\n")
            f.write(f"-" * 30 + "\n")
            f.write(f"Video 1: {os.path.basename(video1_path)} ({video1_size_mb:.1f} MB")
            if video1_duration:
                f.write(f", {video1_duration:.2f}s")
            f.write(f")\n")
            
            f.write(f"Video 2: {os.path.basename(video2_path)} ({video2_size_mb:.1f} MB")
            if video2_duration:
                f.write(f", {video2_duration:.2f}s")
            f.write(f")\n")
            
            f.write(f"\nTotal Source Size: {video1_size_mb + video2_size_mb:.1f} MB\n")
            if video1_duration and video2_duration:
                f.write(f"Total Source Duration: {video1_duration + video2_duration:.2f} seconds\n")
            
            f.write(f"\n")
        
        print(f"[INFO] Created combined metadata file: {metadata_path}")
        return metadata_path
        
    except Exception as e:
        print(f"[ERROR] Failed to create combined metadata file: {e}")
        return None

# ============================================================================
# VIDEO COMBINATION FUNCTIONS
# ============================================================================

def video_combination_worker():
    """Background worker thread for combining videos"""
    global combination_queue
    
    print("[COMBINE_WORKER] Video combination worker started")
    
    while True:
        try:
            # Get the next combination task from queue (blocks if empty)
            task = combination_queue.get(timeout=1)
            
            if task is None:  # Poison pill to stop the worker
                print("[COMBINE_WORKER] Worker received stop signal")
                break
                
            video1_path, video2_path, output_path, timestamp = task
            
            print(f"[COMBINE_WORKER] Starting background combination: {os.path.basename(video1_path)} + {os.path.basename(video2_path)}")
            
            success = combine_videos(video1_path, video2_path, output_path, timestamp)
            
            if success:
                combined_size_mb = os.path.getsize(output_path) / (1024*1024)
                print(f"[COMBINE_WORKER] Background combination completed: {output_path} ({combined_size_mb:.1f}MB)")
            else:
                print(f"[COMBINE_WORKER] Background combination failed for {output_path}")
            
            # Mark task as done
            combination_queue.task_done()
            
        except queue.Empty:
            # No tasks in queue, continue waiting
            continue
        except Exception as e:
            print(f"[COMBINE_WORKER] Error in combination worker: {e}")
            combination_queue.task_done()

def start_combination_worker():
    """Start the background video combination worker thread"""
    global combination_thread
    
    if combination_thread is None or not combination_thread.is_alive():
        combination_thread = threading.Thread(target=video_combination_worker, daemon=True)
        combination_thread.start()
        print("[INFO] Background video combination worker started")

def stop_combination_worker():
    """Stop the background video combination worker thread"""
    global combination_queue, combination_thread
    
    if combination_thread and combination_thread.is_alive():
        # Send poison pill to stop worker
        combination_queue.put(None)
        combination_thread.join(timeout=5)
        print("[INFO] Background video combination worker stopped")

def queue_video_combination(video1_path, video2_path, output_path, timestamp):
    """Queue a video combination task for background processing"""
    global combination_queue
    
    task = (video1_path, video2_path, output_path, timestamp)
    combination_queue.put(task)
    queue_size = combination_queue.qsize()
    print(f"[INFO] Queued video combination for background processing (queue size: {queue_size})")

def get_combination_queue_status():
    """Get the current status of the combination queue"""
    global combination_queue
    return combination_queue.qsize()

def combine_videos(video1_path, video2_path, output_path, timestamp):
    """Combine two videos using ffmpeg - video1 followed by video2"""
    try:
        if not check_ffmpeg_installed():
            print("[ERROR] ffmpeg not installed. Install with: sudo apt install ffmpeg")
            return False
            
        if not os.path.exists(video1_path) or not os.path.exists(video2_path):
            print(f"[ERROR] One or both video files not found: {video1_path}, {video2_path}")
            return False
        
        # Track combination time
        combine_start_time = time.time()
        
        # Method 1: Try ultra-fast concat demuxer (fastest - no re-encoding)
        print(f"[COMBINE] Attempting ultra-fast stream copy concatenation...")
        success = try_concat_demuxer(video1_path, video2_path, output_path)
        
        if success:
            combine_end_time = time.time()
            combine_time_sec = combine_end_time - combine_start_time
            print(f"[COMBINE] Ultra-fast combination completed in {combine_time_sec:.1f} seconds: {output_path}")
            
            # Create metadata file for combined video
            combined_size_mb = os.path.getsize(output_path) / (1024*1024)
            create_combined_metadata_file(output_path, video1_path, video2_path, combine_time_sec, combined_size_mb)
            return True
        
        # Method 2: Fallback to concat filter with stream copy
        print(f"[COMBINE] Fallback: Using concat filter with stream copy...")
        success = try_concat_filter_copy(video1_path, video2_path, output_path)
        
        if success:
            combine_end_time = time.time()
            combine_time_sec = combine_end_time - combine_start_time
            print(f"[COMBINE] Fast combination completed in {combine_time_sec:.1f} seconds: {output_path}")
            
            # Create metadata file for combined video
            combined_size_mb = os.path.getsize(output_path) / (1024*1024)
            create_combined_metadata_file(output_path, video1_path, video2_path, combine_time_sec, combined_size_mb)
            return True
        
        # Method 3: Last resort - re-encoding (slow but compatible)
        print(f"[COMBINE] Last resort: Re-encoding concatenation...")
        success = try_concat_reencode(video1_path, video2_path, output_path)
        
        combine_end_time = time.time()
        combine_time_sec = combine_end_time - combine_start_time
        
        if success:
            print(f"[COMBINE] Re-encoding combination completed in {combine_time_sec:.1f} seconds: {output_path}")
            
            # Create metadata file for combined video
            combined_size_mb = os.path.getsize(output_path) / (1024*1024)
            create_combined_metadata_file(output_path, video1_path, video2_path, combine_time_sec, combined_size_mb)
            return True
        else:
            print(f"[ERROR] All combination methods failed")
            return False
            
    except Exception as e:
        print(f"[ERROR] Video combination failed: {e}")
        return False

def try_concat_demuxer(video1_path, video2_path, output_path):
    """Ultra-fast method: concat demuxer (no re-encoding, ~1-2 seconds)"""
    try:
        # Create temporary file list for concat demuxer
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(f"file '{os.path.abspath(video1_path)}'\n")
            f.write(f"file '{os.path.abspath(video2_path)}'\n")
            filelist_path = f.name
        
        try:
            # Ultra-fast concat demuxer - just copies streams
            cmd = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", filelist_path,
                "-c", "copy",  # Stream copy - no encoding
                "-avoid_negative_ts", "make_zero",  # Fix timing issues
                output_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.returncode == 0
            
        finally:
            os.unlink(filelist_path)
            
    except Exception as e:
        print(f"[DEBUG] Concat demuxer failed: {e}")
        return False

def try_concat_filter_copy(video1_path, video2_path, output_path):
    """Fast method: concat filter with stream copy (~5-10 seconds)"""
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", video1_path,
            "-i", video2_path,
            "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]",
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "ultrafast",  # Fastest encoding preset
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",  # Optimize for streaming
            output_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return result.returncode == 0
        
    except Exception as e:
        print(f"[DEBUG] Concat filter copy failed: {e}")
        return False

def try_concat_reencode(video1_path, video2_path, output_path):
    """Slowest method: Full re-encoding (30+ seconds but most compatible)"""
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", video1_path,
            "-i", video2_path,
            "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]",
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "fast",  # Balanced preset
            "-crf", "23",  # Good quality
            "-c:a", "aac", "-b:a", "192k",
            output_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return result.returncode == 0
        
    except Exception as e:
        print(f"[DEBUG] Re-encoding concat failed: {e}")
        return False

# ============================================================================
# GOPRO CONTROLLER CLASS
# ============================================================================

class GoProController:
    def __init__(self, gopro_id, config, base_download_dir):
        self.gopro_id = gopro_id
        self.config = config
        self.ip = config["ip"]
        self.interface = config["interface"]
        self.name = config["name"]
        self.download_dir = os.path.join(base_download_dir, config["download_subdir"])
        
    def get_gopro_camera(self):
        """Initialize GoPro camera object with specific IP"""
        return None
        
    def record_video(self, duration):
        """Start recording on this GoPro"""
        try:
            # Start recording
            cmd = f"curl -m5 --interface {self.interface} -s http://{self.ip}/gp/gpControl/command/shutter?p=1"
            result = subprocess.run(cmd, shell=True, capture_output=True)
            if result.returncode != 0:
                raise Exception(f"Failed to start recording on {self.name}")
            
            print(f"[{self.name}] Recording started for {duration} seconds...")
            time.sleep(duration)
            
            # Stop recording
            cmd = f"curl -m5 --interface {self.interface} -s http://{self.ip}/gp/gpControl/command/shutter?p=0"
            result = subprocess.run(cmd, shell=True, capture_output=True)
            if result.returncode != 0:
                raise Exception(f"Failed to stop recording on {self.name}")
                
            print(f"[{self.name}] Recording stopped")
            return True
            
        except Exception as e:
            print(f"[ERROR] {self.name} recording failed: {e}")
            return False
    
    def get_media_list(self):
        """Get media list from GoPro"""
        try:
            cmd = f"curl -m10 --interface {self.interface} -s http://{self.ip}/gp/gpMediaList"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                raise Exception("Failed to get media list")
            return json.loads(result.stdout)
        except Exception as e:
            print(f"[ERROR] {self.name} failed to get media list: {e}")
            return None
    
    def delete_file_from_gopro(self, filename):
        """Delete a specific file from GoPro storage"""
        try:
            # Use the GoPro HTTP API to delete specific file
            delete_url = f"http://{self.ip}/gp/gpControl/command/storage/delete?p=/100GOPRO/{filename}"
            cmd = f"curl -m10 --interface {self.interface} -s '{delete_url}'"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            
            if result.returncode == 0:
                print(f"[{self.name}] Successfully deleted {filename} from GoPro")
                return True
            else:
                print(f"[{self.name}] Failed to delete {filename} from GoPro")
                return False
                
        except Exception as e:
            print(f"[ERROR] {self.name} file deletion failed: {e}")
            return False
    
    def download_latest_clip(self):
        """Download the latest video clip and delete it from GoPro after successful download"""
        # Use current time for backwards compatibility
        return self.download_latest_clip_with_timestamp(datetime.now())
    
    def download_latest_clip_with_timestamp(self, recording_timestamp):
        """Download the latest video clip with a specific timestamp and delete it from GoPro after successful download"""
        try:
            # Ensure download directory exists before checking space
            os.makedirs(self.download_dir, exist_ok=True)
            
            # Check available space before download
            available_gb = get_available_space_gb(self.download_dir)
            if available_gb < 1.0:  # Less than 1GB available
                print(f"[WARNING] {self.name} - Low storage space: {available_gb:.1f}GB available")
                if available_gb < 0.5:  # Less than 500MB
                    print(f"[ERROR] {self.name} - Insufficient storage space for download")
                    return False
            
            media = self.get_media_list()
            if not media:
                return False
                
            files = media.get("media", [])[0].get("fs", [])
            videos = [f for f in files if f.get("n", "").lower().endswith(".mp4")]
            if not videos:
                print(f"[{self.name}] No videos found")
                return False
                
            latest = videos[-1]
            latest_name = latest["n"]
            
            # Use provided recording timestamp instead of current time
            ts = recording_timestamp.strftime("%Y-%m-%d_%H-%M-%S")
            
            # Download
            camera_url = f"http://{self.ip}:{CAM_PORT}/videos/DCIM/100GOPRO/{latest_name}"
            # Add GoPro name to filename: timestamp_GoProName_originalname.mp4
            final_dst = os.path.join(self.download_dir, f"{ts}_{self.name}_{latest_name}")
            
            print(f"[{self.name}] Downloading {latest_name} -> {final_dst}")
            print(f"[{self.name}] Available space: {available_gb:.1f}GB")
            print(f"[{self.name}] Using recording timestamp: {ts}")
            
            # Track download time
            download_start_time = time.time()
            
            # Use curl with progress and interface specification for download
            # 10 minute timeout for large files and added connection optimizations
            cmd = f"curl -# -m600 --connect-timeout 30 --speed-time 60 --speed-limit 1024 --interface {self.interface} -o '{final_dst}' '{camera_url}'"
            result = subprocess.run(cmd, shell=True)
            
            download_end_time = time.time()
            download_time_sec = download_end_time - download_start_time
            
            if result.returncode == 0 and os.path.exists(final_dst):
                file_size_mb = os.path.getsize(final_dst) / (1024*1024)
                print(f"[{self.name}] Successfully saved {file_size_mb:.1f}MB to {final_dst}")
                print(f"[{self.name}] Download completed in {download_time_sec:.1f} seconds")
                
                # Verify file integrity (basic check - file size > 0 and reasonable)
                if file_size_mb > 0.1:  # At least 100KB (very conservative)
                    # Create metadata file
                    create_metadata_file(final_dst, download_time_sec=download_time_sec, file_size_mb=file_size_mb, 
                                       raspberry_timestamp=recording_timestamp)
                    
                    # File downloaded successfully, now delete from GoPro
                    if self.delete_file_from_gopro(latest_name):
                        print(f"[{self.name}] File cleanup completed - removed from GoPro storage")
                    else:
                        print(f"[{self.name}] Warning: Download successful but failed to delete from GoPro")
                    return True
                else:
                    print(f"[{self.name}] Downloaded file appears corrupted (size: {file_size_mb:.1f}MB)")
                    # Remove corrupted local file
                    os.remove(final_dst)
                    return False
            else:
                print(f"[{self.name}] Download failed (took {download_time_sec:.1f} seconds)")
                # Clean up partial download
                if os.path.exists(final_dst):
                    os.remove(final_dst)
                return False
                
        except Exception as e:
            print(f"[ERROR] {self.name} download failed: {e}")
            return False

# ============================================================================
# MAIN RECORDING AND FETCH FUNCTIONS
# ============================================================================

def record_and_fetch_all():
    """Record and fetch from both GoPros simultaneously, then queue video combination"""
    # Check that both GoPros are connected before starting
    all_connected, connected_gopros = check_all_gopros_connected()
    connected_count = sum(connected_gopros.values())
    
    if connected_count < 2:
        print(f"[ERROR] Only {connected_count}/2 GoPros connected. Recording requires both GoPros.")
        print("[INFO] Please ensure both GoPros are connected before triggering recording.")
        return
    
    print("[INFO] Both GoPros confirmed connected - starting recording")
    
    # Check storage and get appropriate directories
    download_dir, combined_dir = check_storage_availability()
    
    # Set output for camera start
    pcf_output.port[CAM_BUSY_OUTPUT_PIN] = False
    
    # Store recording start time for consistent timestamping
    recording_start_time = datetime.now()
    recording_timestamp = recording_start_time.strftime("%Y-%m-%d_%H-%M-%S")
    
    controllers = {gopro_id: GoProController(gopro_id, config, download_dir) 
                  for gopro_id, config in GOPROS.items()}
    
    # Start recording on both GoPros in parallel
    with ThreadPoolExecutor(max_workers=2) as executor:
        # Submit recording tasks
        record_futures = {executor.submit(controller.record_video, RECORD_SECS): gopro_id 
                         for gopro_id, controller in controllers.items()}
        
        # Wait for recordings to complete
        record_results = {}
        for future in as_completed(record_futures):
            gopro_id = record_futures[future]
            try:
                record_results[gopro_id] = future.result()
            except Exception as e:
                print(f"[ERROR] Recording failed for {gopro_id}: {e}")
                record_results[gopro_id] = False
    
    # Wait for finalization
    print(f"[WAIT] Waiting {FINALIZE_SECS} seconds for finalization...")
    time.sleep(FINALIZE_SECS)
    
    # Download from both GoPros in parallel with extended timeout
    downloaded_files = {}
    print("[INFO] Starting parallel downloads with 10-minute timeout...")
    print(f"[INFO] Using recording timestamp: {recording_timestamp} for all files")
    
    with ThreadPoolExecutor(max_workers=2) as executor:
        download_futures = {executor.submit(controller.download_latest_clip_with_timestamp, 
                                          recording_start_time): gopro_id 
                           for gopro_id, controller in controllers.items()}
        
        download_results = {}
        for future in as_completed(download_futures):
            gopro_id = download_futures[future]
            try:
                result = future.result()
                download_results[gopro_id] = result
                # Store the path of successfully downloaded files
                if result:
                    # Get the latest file from the controller's download directory
                    controller = controllers[gopro_id]
                    files = [f for f in os.listdir(controller.download_dir) 
                            if f.lower().endswith('.mp4')]
                    if files:
                        # Get the most recent file (should be the one we just downloaded)
                        latest_file = max(files, key=lambda x: os.path.getctime(
                            os.path.join(controller.download_dir, x)))
                        downloaded_files[gopro_id] = os.path.join(
                            controller.download_dir, latest_file)
                        print(f"[INFO] {controllers[gopro_id].name} download completed successfully")
                    else:
                        print(f"[WARNING] {controllers[gopro_id].name} download reported success but no file found")
                else:
                    print(f"[ERROR] {controllers[gopro_id].name} download failed")
            except Exception as e:
                print(f"[ERROR] Download failed for {gopro_id}: {e}")
                download_results[gopro_id] = False
    
    # Set output back - downloads complete, ready for next trigger
    pcf_output.port[CAM_BUSY_OUTPUT_PIN] = True
    
    # Report results
    successful_records = sum(record_results.values())
    successful_downloads = sum(download_results.values())
    print(f"[SUMMARY] {successful_records}/2 recordings successful, {successful_downloads}/2 downloads successful")
    
    # Queue video combination for background processing if both downloads were successful
    if len(downloaded_files) == 2 and 'gopro1' in downloaded_files and 'gopro3' in downloaded_files:
        try:
            # Create combined directory
            os.makedirs(combined_dir, exist_ok=True)
            
            # Check space for combined video (estimate 2x largest file size needed)
            max_file_size = max([os.path.getsize(f) for f in downloaded_files.values()])
            available_space = get_available_space_gb(combined_dir) * 1024**3  # Convert to bytes
            
            if available_space < (max_file_size * 2):
                print(f"[WARNING] May not have enough space for video combination")
                print(f"[INFO] Available: {available_space/(1024**3):.1f}GB, Estimated needed: {(max_file_size*2)/(1024**3):.1f}GB")
                print("[INFO] Skipping video combination due to insufficient space")
            else:
                # Generate filename for combined file using recording timestamp
                combined_filename = f"{recording_timestamp}_Combined_GoPro1+GoPro3.mp4"
                combined_path = os.path.join(combined_dir, combined_filename)
                
                # Queue the combination task for background processing
                queue_video_combination(downloaded_files['gopro1'], downloaded_files['gopro3'], 
                                      combined_path, recording_timestamp)
                
                queue_size = get_combination_queue_status()
                print(f"[INFO] Video combination queued for background processing")
                if queue_size > 1:
                    print(f"[INFO] {queue_size-1} other combination tasks ahead in queue")
                
        except Exception as e:
            print(f"[ERROR] Failed to queue video combination: {e}")
    else:
        if len(downloaded_files) < 2:
            print("[INFO] Cannot combine videos - not all downloads successful")
        else:
            print("[INFO] Cannot combine videos - missing expected GoPro files")
    
    print("[INFO] Ready for next trigger (combination running in background)")
    
    # Show current queue status
    queue_size = get_combination_queue_status()
    if queue_size > 0:
        print(f"[INFO] Background combination queue: {queue_size} task(s) pending")

# ============================================================================
# MAIN PROGRAM
# ============================================================================

# Initialize PCF8574
pcf_input = PCF8574(I2C_BUS, I2C_ADDR_INPUT)
pcf_output = PCF8574(I2C_BUS, I2C_ADDR_OUTPUT)

def main():
    # Initialisierung
    # Der I2C Ausgang wird invertiert angesteuert!
    pcf_output.port[CAM_BUSY_OUTPUT_PIN] = True
    
    # Debug storage setup
    print("[DEBUG] Checking storage setup...")
    print(f"[DEBUG] /home/pi exists: {os.path.exists('/home/pi')}")
    if os.path.exists('/home/pi'):
        print(f"[DEBUG] /home/pi is writable: {os.access('/home/pi', os.W_OK)}")
        print(f"[DEBUG] Available space on USB: {get_available_space_gb('/home/pi'):.1f}GB")
    
    # Check current working directory and home space
    print(f"[DEBUG] Current working directory: {os.getcwd()}")
    print(f"[DEBUG] Home directory space: {get_available_space_gb('/home/pi'):.1f}GB")
    
    # Check ffmpeg installation
    if not check_ffmpeg_installed():
        print("[WARNING] ffmpeg not found. Video combination will not work.")
        print("[INFO] Install with: sudo apt install ffmpeg")
        print("[INFO] Continuing without video combination for now...")
    else:
        print("[INFO] ffmpeg found - video combination enabled")
        # Start the background video combination worker
        start_combination_worker()
    
    print("[INFO] Using integrated Python connection manager (no external shell scripts)")
    
    # Check connections using integrated manager
    all_connected, connected_gopros = check_all_gopros_connected()
    if not all_connected:
        try:
            connected_count = sum(connected_gopros.values())
            if connected_count == 1:
                # One GoPro connected - try to connect the missing one
                missing_gopro = [gid for gid, connected in connected_gopros.items() if not connected][0]
                missing_name = GOPROS[missing_gopro]['name']
                print(f"[INFO] {missing_name} not connected, running targeted reconnection...")
                run_single_gopro_connect(missing_gopro)
            else:
                # No GoPros connected - run full connection
                print("[INFO] No GoPros connected, running full connection...")
                run_connect_script()
            
            time.sleep(5)  # Give time for connections to establish
            all_connected, connected_gopros = check_all_gopros_connected()
            
            if not all_connected:
                connected_count = sum(connected_gopros.values())
                print(f"[WARNING] Only {connected_count}/2 GoPros connected after connection attempt")
                if connected_count == 0:
                    print("[ERROR] No GoPros connected, exiting...")
                    sys.exit(1)
                else:
                    missing_gopros = [GOPROS[gid]['name'] for gid, connected in connected_gopros.items() if not connected]
                    print(f"[INFO] Missing: {', '.join(missing_gopros)}")
                    print("[INFO] System will continue checking and attempting reconnection...")
        except Exception as e:
            print(f"[ERROR] Failed to connect to GoPros: {e}")
            # Check if any GoPros are still connected
            all_connected, connected_gopros = check_all_gopros_connected()
            connected_count = sum(connected_gopros.values())
            if connected_count == 0:
                print("[ERROR] No GoPros connected after error, exiting...")
                sys.exit(1)
            else:
                missing_gopros = [GOPROS[gid]['name'] for gid, connected in connected_gopros.items() if not connected]
                print(f"[INFO] Continuing with {connected_count}/2 GoPros connected")
                print(f"[INFO] Missing: {', '.join(missing_gopros)}")
    else:
        print("[INFO] All GoPros already reachable - skipping connection script.")
    
    print("[INFO] Using Raspberry Pi time for all file timestamps (not GoPro time)")
    print(f"Polling PCF8574@0x{I2C_ADDR_INPUT:02x} P{START_REC_INPUT_PIN}... (Ctrl-C to stop)")
    print("[INFO] System ready - downloads complete immediately, combinations run in background")
    print("[INFO] No external shell script dependencies - all connection management integrated")
    
    try:
        while True:
            # Wait for trigger
            if not pcf_input.port[START_REC_INPUT_PIN]:
                print("\n[TRIGGER] Input is HIGH - checking GoPro connections...")
                
                # Check connections before starting recording
                all_connected, connected_gopros = check_all_gopros_connected()
                connected_count = sum(connected_gopros.values())
                
                if connected_count < 2:
                    missing_gopros = [GOPROS[gid]['name'] for gid, connected in connected_gopros.items() if not connected]
                    missing_ids = [gid for gid, connected in connected_gopros.items() if not connected]
                    print(f"[WARNING] Only {connected_count}/2 GoPros connected")
                    print(f"[INFO] Missing: {', '.join(missing_gopros)}")
                    
                    # Try to reconnect missing GoPros individually first
                    if len(missing_ids) == 1:
                        # Only one missing - try single GoPro reconnection
                        missing_id = missing_ids[0]
                        missing_name = GOPROS[missing_id]['name']
                        print(f"[INFO] Attempting targeted reconnection of {missing_name}...")
                        
                        if run_single_gopro_connect(missing_id):
                            # Check if it worked
                            time.sleep(3)
                            all_connected, connected_gopros = check_all_gopros_connected()
                            connected_count = sum(connected_gopros.values())
                            
                            if connected_count == 2:
                                print("[SUCCESS] Both GoPros now connected - starting recording!")
                            else:
                                print(f"[ERROR] Targeted reconnection failed - trying full connection script...")
                                try:
                                    run_connect_script()
                                    time.sleep(5)
                                    all_connected, connected_gopros = check_all_gopros_connected()
                                    connected_count = sum(connected_gopros.values())
                                    
                                    if connected_count == 2:
                                        print("[SUCCESS] Both GoPros now connected - starting recording!")
                                    else:
                                        still_missing = [GOPROS[gid]['name'] for gid, connected in connected_gopros.items() if not connected]
                                        print(f"[ERROR] Full reconnection also failed - still missing: {', '.join(still_missing)}")
                                        print("[INFO] Recording cancelled - both GoPros required")
                                except Exception as e:
                                    print(f"[ERROR] Full reconnection attempt failed: {e}")
                                    print("[INFO] Recording cancelled - both GoPros required")
                        else:
                            print(f"[ERROR] Targeted reconnection of {missing_name} failed")
                            print("[INFO] Recording cancelled - both GoPros required")
                    else:
                        # Multiple missing - use full connection script
                        print(f"[INFO] Multiple GoPros missing - running full connection script...")
                        try:
                            run_connect_script()
                            time.sleep(5)
                            all_connected, connected_gopros = check_all_gopros_connected()
                            connected_count = sum(connected_gopros.values())
                            
                            if connected_count == 2:
                                print("[SUCCESS] Both GoPros now connected - starting recording!")
                            else:
                                still_missing = [GOPROS[gid]['name'] for gid, connected in connected_gopros.items() if not connected]
                                print(f"[ERROR] Reconnection failed - still missing: {', '.join(still_missing)}")
                                print("[INFO] Recording cancelled - both GoPros required")
                        except Exception as e:
                            print(f"[ERROR] Reconnection attempt failed: {e}")
                            print("[INFO] Recording cancelled - both GoPros required")
                        
                # Proceed only if both GoPros are connected
                if connected_count == 2:
                    print(f"[INFO] Both GoPros connected - starting recording")
                    
                    queue_size = get_combination_queue_status() 
                    if queue_size > 0:
                        print(f"[INFO] Note: {queue_size} video combination(s) still processing in background")
                    
                    try:
                        record_and_fetch_all()
                    except Exception as e:
                        print(f"[ERROR] Dual recording failed: {e}", flush=True)
                else:
                    print(f"[INFO] Recording skipped - need 2/2 GoPros, have {connected_count}/2")
                
                # Debounce
                start = time.time()
                while not pcf_input.port[START_REC_INPUT_PIN] and (time.time() - start) < (DEBOUNCE_MS/1000):
                    time.sleep(POLL_INTERVAL)
                
                print("[INFO] Debounce complete - ready for next trigger")
            
            time.sleep(POLL_INTERVAL)
            
    except KeyboardInterrupt:
        print("\nUser interrupt, shutting down...")
        
        # Show final queue status
        queue_size = get_combination_queue_status()
        if queue_size > 0:
            print(f"[INFO] {queue_size} video combination(s) still in progress...")
            print("[INFO] Waiting for background tasks to complete (press Ctrl+C again to force quit)")
            try:
                # Wait for queue to empty with timeout
                timeout = 60  # Wait up to 60 seconds
                start_time = time.time()
                while get_combination_queue_status() > 0 and (time.time() - start_time) < timeout:
                    time.sleep(1)
                
                if get_combination_queue_status() == 0:
                    print("[INFO] All background combinations completed")
                else:
                    print("[INFO] Timeout reached, some combinations may still be running")
                    
            except KeyboardInterrupt:
                print("\n[INFO] Force quit requested")
        
        # Stop the combination worker
        stop_combination_worker()
        sys.exit(0)

if __name__ == "__main__":
    main()
