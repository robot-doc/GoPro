#!/usr/bin/env python3
"""
Simplified record_and_fetch_dual_v2.py
Streamlined version with integrated connection management
"""

import time, os, sys, json, subprocess
from datetime import datetime
from pcf8574 import PCF8574
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import tempfile
import queue

# Configuration
RECORD_SECS = 38
FINALIZE_SECS = 2
DOWNLOAD_DIR = "/home/pi/GoPro_Clips"
COMBINED_DIR = "/home/pi/GoPro_Clips/Combined"
FALLBACK_DIR = "/home/pi/GoPro_Clips_Backup"
CAM_PORT = 8080

# GoPro configurations
GOPROS = {
    "gopro3": {
        "mac": "D0:21:F8:9C:FF:80",
        "ssid": "HERO8 Achim 3", 
        "psk": "5d3-QNv-MTm",
        "ip": "10.5.5.9",
        "interface": "wlan0",
        "name": "GoPro3",
        "download_subdir": "GoPro3"
    },
    "gopro1": {
        "mac": "C8:52:0D:A5:9A:39",
        "ssid": "HERO8 Achim 1",
        "psk": "vDh-p7g-TDj", 
        "ip": "10.5.5.9",
        "interface": "wlan1",
        "name": "GoPro1",
        "download_subdir": "GoPro1"
    }
}

# I2C Configuration
I2C_BUS = 1
I2C_ADDR_OUTPUT = 0x20
I2C_ADDR_INPUT = 0x38
START_REC_INPUT_PIN = 0
CAM_BUSY_OUTPUT_PIN = 0
POLL_INTERVAL = 0.05
DEBOUNCE_MS = 200

# Paths
PYTHON_BIN = "/home/pi/gopro-ble-py/gopro-ble-py/venv/bin/python"
BLE_TOOL = "/home/pi/gopro-ble-py/gopro-ble-py/main.py"

# Global queue for video combination
combination_queue = queue.Queue()
combination_thread = None

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def run_cmd(cmd, timeout=30, shell=True):
    """Run command with timeout"""
    try:
        result = subprocess.run(cmd, shell=shell, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "Timeout"
    except Exception as e:
        return False, "", str(e)

def get_available_space_gb(path):
    """Get available space in GB"""
    try:
        statvfs = os.statvfs(path)
        return (statvfs.f_frsize * statvfs.f_bavail) / (1024**3)
    except:
        return 0

def check_storage_availability():
    """Setup storage directories"""
    try:
        if os.path.exists("/home/pi") and os.access("/home/pi", os.W_OK):
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            os.makedirs(COMBINED_DIR, exist_ok=True)
            return DOWNLOAD_DIR, COMBINED_DIR
    except:
        pass
    
    os.makedirs(FALLBACK_DIR, exist_ok=True)
    os.makedirs(os.path.join(FALLBACK_DIR, "Combined"), exist_ok=True)
    return FALLBACK_DIR, os.path.join(FALLBACK_DIR, "Combined")

# ============================================================================
# CONNECTION MANAGEMENT
# ============================================================================

def is_gopro_wifi_already_on(gopro_id):
    """Quick check if GoPro Wi-Fi is already broadcasting"""
    config = GOPROS[gopro_id]
    # Quick scan for the SSID
    success, output, _ = run_cmd(f"sudo iwlist scan | grep -i '{config['ssid']}'", timeout=10)
    return success and config['ssid'] in output

def activate_gopro_wifi(gopro_id, max_attempts=5):
    """Smart GoPro Wi-Fi activation with adaptive retry strategy"""
    config = GOPROS[gopro_id]
    print(f"Activating {config['name']} Wi-Fi via BLE...")
    
    # First check if Wi-Fi is already on
    if is_gopro_wifi_already_on(gopro_id):
        print(f"✓ {config['name']} Wi-Fi already broadcasting")
        return True
    
    bluetooth_reset_count = 0
    base_delay = 3
    
    for attempt in range(max_attempts):
        print(f"BLE attempt {attempt+1}/{max_attempts} for {config['name']}")
        
        # Exponential backoff with jitter (but cap at reasonable max)
        if attempt > 0:
            delay = min(base_delay * (1.5 ** attempt) + (attempt * 0.5), 15)
            print(f"Waiting {delay:.1f}s before attempt...")
            time.sleep(delay)
        else:
            time.sleep(8)  # Initial longer wait for BLE advertising
        
        # Try BLE command with adaptive timeout
        timeout = 20 + (attempt * 5)  # Increase timeout on later attempts
        cmd = f"'{PYTHON_BIN}' '{BLE_TOOL}' --interactive true --address '{config['mac']}' --command 'wifi on'"
        success, stdout, stderr = run_cmd(cmd, timeout=timeout)
        
        if success:
            print(f"✓ BLE command succeeded for {config['name']}")
            
            # Verify Wi-Fi actually turned on (give it time to start)
            print("Verifying Wi-Fi activation...")
            for verify_attempt in range(3):
                time.sleep(3)
                if is_gopro_wifi_already_on(gopro_id):
                    print(f"✓ {config['name']} Wi-Fi confirmed broadcasting")
                    return True
                print(f"Wi-Fi not yet broadcasting, checking again... ({verify_attempt+1}/3)")
            
            print(f"⚠ BLE succeeded but Wi-Fi not broadcasting for {config['name']}")
            # Continue trying rather than giving up
        
        # Analyze failure and decide on recovery strategy
        needs_bt_reset = False
        
        if "timeout" in stderr.lower() or "timed out" in stderr.lower():
            print(f"BLE timeout - GoPro may be sleeping or out of range")
            needs_bt_reset = attempt >= 2  # Reset BT after 2 timeouts
        elif "connection refused" in stderr.lower() or "no route" in stderr.lower():
            print(f"BLE connection refused - GoPro may not be in pairing mode")
            needs_bt_reset = True
        elif "device not found" in stderr.lower() or "not available" in stderr.lower():
            print(f"BLE device not found - checking Bluetooth adapter")
            needs_bt_reset = True
        else:
            print(f"BLE failed: {stderr[:100]}...")
            needs_bt_reset = attempt >= 1  # Reset BT for unknown errors
        
        # Smart Bluetooth reset - only when likely to help
        if needs_bt_reset and bluetooth_reset_count < 2 and attempt < max_attempts - 1:
            print(f"Resetting Bluetooth adapter (reset #{bluetooth_reset_count + 1})")
            if reset_bluetooth_smart():
                bluetooth_reset_count += 1
                time.sleep(2)  # Give BT time to stabilize
            else:
                print("Bluetooth reset failed - continuing without reset")
        
        # On last two attempts, try alternative approaches
        if attempt >= max_attempts - 2:
            print(f"Trying alternative BLE approach for {config['name']}...")
            # Try without interactive mode
            alt_cmd = f"'{PYTHON_BIN}' '{BLE_TOOL}' --address '{config['mac']}' --command 'wifi on'"
            alt_success, _, _ = run_cmd(alt_cmd, timeout=timeout)
            if alt_success:
                time.sleep(5)
                if is_gopro_wifi_already_on(gopro_id):
                    print(f"✓ Alternative BLE method worked for {config['name']}")
                    return True
    
    print(f"✗ All BLE attempts failed for {config['name']} after {max_attempts} tries")
    return False

def reset_bluetooth_smart():
    """Smart Bluetooth reset with verification"""
    print("Performing smart Bluetooth reset...")
    
    # Stop bluetooth service cleanly first
    run_cmd("sudo systemctl stop bluetooth", timeout=10)
    time.sleep(1)
    
    # Reset HCI device
    success1, _, _ = run_cmd("sudo hciconfig hci0 down", timeout=5)
    time.sleep(1)
    success2, _, _ = run_cmd("sudo hciconfig hci0 up", timeout=5)
    time.sleep(1)
    
    # Restart bluetooth service
    success3, _, _ = run_cmd("sudo systemctl start bluetooth", timeout=10)
    time.sleep(2)
    
    # Verify Bluetooth is working
    success4, output, _ = run_cmd("hciconfig hci0", timeout=5)
    bt_working = success4 and "UP RUNNING" in output
    
    if bt_working:
        print("✓ Bluetooth reset successful")
    else:
        print("✗ Bluetooth reset may have failed")
    
    return bt_working

def setup_wifi_interface(gopro_id):
    """Setup Wi-Fi interface for GoPro"""
    config = GOPROS[gopro_id] 
    interface = config['interface']
    
    # Clean up interface
    run_cmd(f"sudo pkill -9 -f 'wpa_supplicant.*{interface}'")
    run_cmd(f"sudo pkill -9 -f 'dhclient.*{interface}'")
    run_cmd(f"sudo rm -f '/var/run/wpa_supplicant/{interface}'")
    
    # Reset interface
    run_cmd(f"sudo ip link set {interface} down")
    run_cmd(f"sudo ip addr flush dev {interface}")
    time.sleep(2)
    run_cmd(f"sudo ip link set {interface} up")
    time.sleep(3)
    
    # Create wpa_supplicant config
    wpa_conf = f"/etc/wpa_supplicant/wpa_supplicant_{interface}.conf"
    
    # Check if config exists
    exists, _, _ = run_cmd(f"sudo test -f '{wpa_conf}'")
    if not exists:
        base_config = """ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=DE
"""
        run_cmd(f"sudo tee '{wpa_conf}' > /dev/null", shell=False, 
                timeout=10, input=base_config)
    
    # Add network if not exists
    success, content, _ = run_cmd(f"sudo cat '{wpa_conf}'")
    if success and f'ssid="{config["ssid"]}"' not in content:
        network_block = f"""
network={{
    ssid="{config['ssid']}"
    psk="{config['psk']}"
    key_mgmt=WPA-PSK
}}
"""
        run_cmd(f"sudo tee -a '{wpa_conf}' > /dev/null", shell=False,
                input=network_block)
    
    return wpa_conf

def connect_wifi(gopro_id, wpa_conf):
    """Connect to GoPro Wi-Fi"""
    config = GOPROS[gopro_id]
    interface = config['interface']
    
    # Start wpa_supplicant
    success, _, _ = run_cmd(f"sudo wpa_supplicant -B -i {interface} -c '{wpa_conf}'")
    if not success:
        return False
    
    time.sleep(5)
    
    # Get DHCP lease
    run_cmd(f"timeout 15 sudo dhclient {interface}")
    
    # Check if we got IP, assign static if not
    success, output, _ = run_cmd(f"ip addr show {interface}")
    if success and "inet 10.5.5." not in output:
        static_ip = "10.5.5.100/24" if interface == "wlan0" else "10.5.5.101/24"
        run_cmd(f"sudo ip addr add {static_ip} dev {interface}")
    
    # Add route
    run_cmd(f"sudo ip route replace {config['ip']}/32 dev {interface}")
    
    return True

def test_gopro_connection(gopro_id):
    """Test GoPro HTTP connection"""
    config = GOPROS[gopro_id]
    
    for attempt in range(5):
        cmd = f"curl -m5 --interface {config['interface']} -s -o /dev/null -w '%{{http_code}}' http://{config['ip']}/gp/gpControl/status"
        success, output, _ = run_cmd(cmd)
        
        if success and output.strip() == "200":
            print(f"✓ {config['name']} connected successfully")
            return True
        
        if attempt < 4:
            time.sleep(3)
    
    print(f"✗ {config['name']} connection failed")
    return False

def connect_single_gopro(gopro_id):
    """Connect to a single GoPro with smart connection strategy"""
    config = GOPROS[gopro_id]
    print(f"\n=== Connecting {config['name']} ===")
    
    # Check interface exists
    success, _, _ = run_cmd(f"ip link show {config['interface']}")
    if not success:
        print(f"✗ Interface {config['interface']} not found")
        return False
    
    # Quick check if already connected
    if test_gopro_connection(gopro_id):
        print(f"✓ {config['name']} already connected and working")
        return True
    
    # Step 1: Activate GoPro Wi-Fi via BLE (smart retry logic)
    if not activate_gopro_wifi(gopro_id):
        return False
    
    # Step 2: Setup Wi-Fi interface 
    print(f"Setting up Wi-Fi interface for {config['name']}...")
    wpa_conf = setup_wifi_interface(gopro_id)
    if not wpa_conf:
        print(f"✗ Failed to setup Wi-Fi interface for {config['name']}")
        return False
    
    # Step 3: Connect to Wi-Fi with verification
    print(f"Connecting to {config['name']} Wi-Fi network...")
    if not connect_wifi(gopro_id, wpa_conf):
        print(f"✗ Failed to connect to {config['name']} Wi-Fi")
        return False
    
    # Step 4: Test HTTP connection with retry
    print(f"Testing HTTP connection to {config['name']}...")
    return test_gopro_connection(gopro_id)

def connect_all_gopros():
    """Connect both GoPros sequentially"""
    print("Connecting GoPros sequentially...")
    
    # Cleanup
    run_cmd("sudo pkill -9 -f 'wpa_supplicant.*wlan'")
    run_cmd("sudo pkill -9 -f 'dhclient.*wlan'")
    run_cmd("sudo systemctl restart bluetooth")
    time.sleep(3)
    
    # Reset interfaces
    for interface in ["wlan0", "wlan1"]:
        run_cmd(f"sudo ip link set {interface} down")
    time.sleep(2)
    for interface in ["wlan0", "wlan1"]:
        run_cmd(f"sudo ip link set {interface} up")
    time.sleep(3)
    
    # Connect sequentially
    result1 = connect_single_gopro("gopro3")
    time.sleep(3)
    result2 = connect_single_gopro("gopro1")
    
    if result1 and result2:
        print("✓ Both GoPros connected successfully!")
        return True
    else:
        print(f"✗ Connection failed (GoPro3: {result1}, GoPro1: {result2})")
        return False

def check_gopro_connected(gopro_id):
    """Check if single GoPro is connected"""
    config = GOPROS[gopro_id]
    cmd = f"curl -m2 --interface {config['interface']} -s -o /dev/null -w '%{{http_code}}' http://{config['ip']}/gp/gpControl/status"
    success, output, _ = run_cmd(cmd)
    return success and output.strip() == "200"

def check_all_connected():
    """Check both GoPros"""
    gopro3_ok = check_gopro_connected("gopro3")
    gopro1_ok = check_gopro_connected("gopro1")
    
    print(f"GoPro3: {'✓' if gopro3_ok else '✗'}, GoPro1: {'✓' if gopro1_ok else '✗'}")
    return gopro3_ok and gopro1_ok, {"gopro3": gopro3_ok, "gopro1": gopro1_ok}

# ============================================================================
# GOPRO CONTROL
# ============================================================================

class GoProController:
    def __init__(self, gopro_id, base_download_dir):
        self.gopro_id = gopro_id
        self.config = GOPROS[gopro_id]
        self.download_dir = os.path.join(base_download_dir, self.config["download_subdir"])
        os.makedirs(self.download_dir, exist_ok=True)
    
    def _api_call(self, endpoint):
        """Make GoPro API call"""
        cmd = f"curl -m5 --interface {self.config['interface']} -s http://{self.config['ip']}{endpoint}"
        return run_cmd(cmd)
    
    def record_video(self, duration):
        """Record video"""
        # Start recording
        success, _, _ = self._api_call("/gp/gpControl/command/shutter?p=1")
        if not success:
            print(f"Failed to start recording on {self.config['name']}")
            return False
        
        print(f"[{self.config['name']}] Recording for {duration}s...")
        time.sleep(duration)
        
        # Stop recording
        success, _, _ = self._api_call("/gp/gpControl/command/shutter?p=0")
        if not success:
            print(f"Failed to stop recording on {self.config['name']}")
            return False
        
        print(f"[{self.config['name']}] Recording stopped")
        return True
    
    def get_latest_video(self):
        """Get latest video filename"""
        success, output, _ = self._api_call("/gp/gpMediaList")
        if not success:
            return None
        
        try:
            media = json.loads(output)
            files = media.get("media", [])[0].get("fs", [])
            videos = [f for f in files if f.get("n", "").lower().endswith(".mp4")]
            return videos[-1]["n"] if videos else None
        except:
            return None
    
    def download_video(self, filename, timestamp):
        """Download video file"""
        if not filename:
            return False
        
        # Check space
        if get_available_space_gb(self.download_dir) < 0.5:
            print(f"Insufficient space for {self.config['name']}")
            return False
        
        # Download
        ts = timestamp.strftime("%Y-%m-%d_%H-%M-%S")
        local_path = os.path.join(self.download_dir, f"{ts}_{self.config['name']}_{filename}")
        url = f"http://{self.config['ip']}:{CAM_PORT}/videos/DCIM/100GOPRO/{filename}"
        
        cmd = f"curl -# -m600 --interface {self.config['interface']} -o '{local_path}' '{url}'"
        success, _, _ = run_cmd(cmd, timeout=660)
        
        if success and os.path.exists(local_path):
            size_mb = os.path.getsize(local_path) / (1024*1024)
            print(f"[{self.config['name']}] Downloaded {size_mb:.1f}MB")
            
            # Delete from GoPro
            self._api_call(f"/gp/gpControl/command/storage/delete?p=/100GOPRO/{filename}")
            return local_path
        
        return False

# ============================================================================
# VIDEO COMBINATION
# ============================================================================

def combine_videos_simple(video1, video2, output):
    """Simple video combination using ffmpeg"""
    try:
        # Try fastest method first - concat demuxer
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(f"file '{os.path.abspath(video1)}'\n")
            f.write(f"file '{os.path.abspath(video2)}'\n")
            filelist = f.name
        
        try:
            cmd = f"ffmpeg -y -f concat -safe 0 -i '{filelist}' -c copy '{output}'"
            success, _, _ = run_cmd(cmd, timeout=60)
            
            if success and os.path.exists(output):
                print(f"✓ Combined videos: {os.path.basename(output)}")
                return True
            
            # Fallback to re-encoding
            cmd = f"ffmpeg -y -i '{video1}' -i '{video2}' -filter_complex '[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]' -map '[outv]' -map '[outa]' -c:v libx264 -preset fast '{output}'"
            success, _, _ = run_cmd(cmd, timeout=300)
            
            if success and os.path.exists(output):
                print(f"✓ Combined videos (re-encoded): {os.path.basename(output)}")
                return True
                
        finally:
            os.unlink(filelist)
            
    except Exception as e:
        print(f"Video combination failed: {e}")
    
    return False

def combination_worker():
    """Background worker for video combination"""
    while True:
        try:
            task = combination_queue.get(timeout=1)
            if task is None:  # Stop signal
                break
            
            video1, video2, output = task
            print(f"Combining {os.path.basename(video1)} + {os.path.basename(video2)}...")
            combine_videos_simple(video1, video2, output)
            combination_queue.task_done()
            
        except queue.Empty:
            continue
        except Exception as e:
            print(f"Combination worker error: {e}")
            combination_queue.task_done()

def start_combination_worker():
    """Start background combination worker"""
    global combination_thread
    if combination_thread is None or not combination_thread.is_alive():
        combination_thread = threading.Thread(target=combination_worker, daemon=True)
        combination_thread.start()

def queue_combination(video1, video2, output):
    """Queue video combination"""
    combination_queue.put((video1, video2, output))
    print(f"Queued combination (queue: {combination_queue.qsize()})")

# ============================================================================
# MAIN RECORDING FUNCTION
# ============================================================================

def record_and_fetch_all():
    """Main recording and fetching logic"""
    # Check connections
    all_ok, status = check_all_connected()
    if not all_ok:
        print("Not all GoPros connected - aborting")
        return
    
    print("✓ Both GoPros connected - starting recording")
    
    # Setup storage
    download_dir, combined_dir = check_storage_availability()
    
    # Set busy indicator
    pcf_output.port[CAM_BUSY_OUTPUT_PIN] = False
    
    # Record timestamp
    recording_time = datetime.now()
    timestamp = recording_time.strftime("%Y-%m-%d_%H-%M-%S")
    
    # Create controllers
    controllers = {gid: GoProController(gid, download_dir) for gid in GOPROS.keys()}
    
    # Record in parallel
    with ThreadPoolExecutor(max_workers=2) as executor:
        record_futures = {
            executor.submit(controller.record_video, RECORD_SECS): gid 
            for gid, controller in controllers.items()
        }
        
        record_results = {}
        for future in as_completed(record_futures):
            gid = record_futures[future]
            try:
                record_results[gid] = future.result()
            except Exception as e:
                print(f"Recording failed for {gid}: {e}")
                record_results[gid] = False
    
    print(f"Waiting {FINALIZE_SECS}s for finalization...")
    time.sleep(FINALIZE_SECS)
    
    # Download in parallel
    with ThreadPoolExecutor(max_workers=2) as executor:
        download_futures = {}
        for gid, controller in controllers.items():
            filename = controller.get_latest_video()
            if filename:
                future = executor.submit(controller.download_video, filename, recording_time)
                download_futures[future] = gid
        
        downloaded_files = {}
        for future in as_completed(download_futures):
            gid = download_futures[future]
            try:
                result = future.result()
                if result:
                    downloaded_files[gid] = result
                    print(f"✓ {GOPROS[gid]['name']} download completed")
                else:
                    print(f"✗ {GOPROS[gid]['name']} download failed")
            except Exception as e:
                print(f"Download error for {gid}: {e}")
    
    # Clear busy indicator
    pcf_output.port[CAM_BUSY_OUTPUT_PIN] = True
    
    # Queue combination if both downloaded
    if len(downloaded_files) == 2:
        output_file = os.path.join(combined_dir, f"{timestamp}_Combined.mp4")
        queue_combination(downloaded_files['gopro1'], downloaded_files['gopro3'], output_file)
    
    successful_records = sum(record_results.values())
    successful_downloads = len(downloaded_files)
    print(f"Summary: {successful_records}/2 recorded, {successful_downloads}/2 downloaded")

# ============================================================================
# MAIN PROGRAM
# ============================================================================

# Initialize PCF8574
pcf_input = PCF8574(I2C_BUS, I2C_ADDR_INPUT)
pcf_output = PCF8574(I2C_BUS, I2C_ADDR_OUTPUT)

def main():
    """Main program"""
    pcf_output.port[CAM_BUSY_OUTPUT_PIN] = True
    
    print("GoPro Dual Recording System - Simplified Version")
    print("=" * 50)
    
    # Check ffmpeg
    ffmpeg_ok, _, _ = run_cmd("ffmpeg -version")
    if ffmpeg_ok:
        print("✓ ffmpeg found - starting combination worker")
        start_combination_worker()
    else:
        print("✗ ffmpeg not found - no video combination")
    
    # Check connections
    all_ok, status = check_all_connected()
    if not all_ok:
        print("Attempting to connect GoPros...")
        connect_all_gopros()
        time.sleep(3)
        all_ok, status = check_all_connected()
        
        if not all_ok:
            connected_count = sum(status.values())
            if connected_count == 0:
                print("No GoPros connected - exiting")
                sys.exit(1)
            print(f"Only {connected_count}/2 GoPros connected - continuing")
    else:
        print("✓ Both GoPros already connected")
    
    print(f"\nPolling for trigger on pin {START_REC_INPUT_PIN}...")
    print("System ready - press Ctrl+C to exit")
    
    try:
        while True:
            if not pcf_input.port[START_REC_INPUT_PIN]:
                print("\n🔴 TRIGGER DETECTED")
                
                # Quick connection check
                all_ok, status = check_all_connected()
                if not all_ok:
                    missing = [GOPROS[gid]['name'] for gid, ok in status.items() if not ok]
                    print(f"Missing GoPros: {missing}")
                    
                    # Try reconnection
                    if len(missing) == 1:
                        missing_id = [gid for gid, ok in status.items() if not ok][0]
                        if connect_single_gopro(missing_id):
                            all_ok = True
                    else:
                        if connect_all_gopros():
                            all_ok = True
                
                if all_ok:
                    try:
                        record_and_fetch_all()
                    except Exception as e:
                        print(f"Recording failed: {e}")
                else:
                    print("Recording cancelled - GoPros not ready")
                
                # Debounce
                start_time = time.time()
                while not pcf_input.port[START_REC_INPUT_PIN] and (time.time() - start_time) < (DEBOUNCE_MS/1000):
                    time.sleep(POLL_INTERVAL)
                
                print("Ready for next trigger\n")
            
            time.sleep(POLL_INTERVAL)
            
    except KeyboardInterrupt:
        print("\nShutting down...")
        
        # Wait for combinations to finish
        if combination_queue.qsize() > 0:
            print(f"Waiting for {combination_queue.qsize()} combinations to finish...")
            timeout = 60
            start_time = time.time()
            while combination_queue.qsize() > 0 and (time.time() - start_time) < timeout:
                time.sleep(1)
        
        # Stop worker
        combination_queue.put(None)
        if combination_thread:
            combination_thread.join(timeout=5)
        
        print("Goodbye!")

if __name__ == "__main__":
    main()
