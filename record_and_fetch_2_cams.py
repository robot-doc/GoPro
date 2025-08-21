#!/usr/bin/env python3
import time, os, sys, json, requests, subprocess, threading
from datetime import datetime, timezone
from pcf8574 import PCF8574

# ==== CONFIG ====
RECORD_SECS    = 5
FINALIZE_SECS  = 2
DOWNLOAD_DIR   = "/media/pi/Clips/GoPro_Clips"
TRIGGER_PIN    = 17

I2C_BUS         = 1
I2C_ADDR_OUTPUT = 0x20
I2C_ADDR_INPUT  = 0x38
INPUT_PIN       = 0
OUTPUT_PIN      = 0
POLL_INTERVAL   = 0.05
DEBOUNCE_MS     = 200

# ==== INIT ====
pcf_input  = PCF8574(I2C_BUS, I2C_ADDR_INPUT)
pcf_output = PCF8574(I2C_BUS, I2C_ADDR_OUTPUT)

def is_gopro_connected(ip="10.5.5.9", timeout=2):
    try:
        response = requests.get(f"http://{ip}/gp/gpControl/status", timeout=timeout)
        return response.status_code == 200
    except requests.RequestException:
        return False

def run_connect_script():
    script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "connect_gopro.sh"))
    print(f"[INFO] Running connection script at: {script_path}")
    if not os.path.isfile(script_path):
        raise FileNotFoundError(f"[ERROR] Script not found at {script_path}")
    result = subprocess.run(["bash", script_path], capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        raise RuntimeError("Connection script failed")

# ==== Dual Camera Trigger ====
def start_dual_recording():
    def trigger_cam(interface):
        try:
            print(f"[INFO] Triggering shutter on {interface}...")
            subprocess.run([
                "curl", "--interface", interface,
                "http://10.5.5.9/gp/gpControl/command/shutter?p=1"
            ], timeout=3)
        except Exception as e:
            print(f"[ERROR] Trigger failed on {interface}: {e}")

    t1 = threading.Thread(target=trigger_cam, args=("wlan0",))
    t2 = threading.Thread(target=trigger_cam, args=("wlan1",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

# ==== Download from each GoPro ====
def fetch_latest_clip(interface, name_tag):
    ip = "10.5.5.9"
    port = 8080

    try:
        # Get media list
        media_resp = subprocess.check_output([
            "curl", "--interface", interface,
            f"http://{ip}/gp/gpMediaList"
        ], timeout=5)
        media = json.loads(media_resp)
        files = media.get("media", [])[0].get("fs", [])
        videos = [f for f in files if f.get("n", "").lower().endswith(".mp4")]
        latest = videos[-1]
        latest_name = latest["n"]

        # Build download path
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        camera_url = f"http://{ip}:{port}/videos/DCIM/100GOPRO/{latest_name}"
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        tmp_name = f"{name_tag}_{latest_name}"
        final_dst = os.path.join(DOWNLOAD_DIR, f"{ts}_{name_tag}.mp4")

        print(f"[DL-{name_tag}] Downloading {latest_name} -> {final_dst}", flush=True)
        with requests.get(camera_url, stream=True, timeout=10) as r:
            r.raise_for_status()
            with open(tmp_name, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
        os.replace(tmp_name, final_dst)
        print(f"[OK-{name_tag}] Saved to {final_dst}")

    except Exception as e:
        print(f"[ERROR-{name_tag}] Download failed: {e}")

# ==== Main Logic ====
def record_and_fetch():
    pcf_output.port[OUTPUT_PIN] = False

    print("[REC] Starting dual-camera recording for", RECORD_SECS, "seconds...", flush=True)
    start_dual_recording()
    time.sleep(RECORD_SECS)
    start_dual_recording()  # stops recording

    print("[WAIT] Waiting", FINALIZE_SECS, "seconds...", flush=True)
    time.sleep(FINALIZE_SECS)

    # Download from both cameras
    fetch_latest_clip("wlan0", "cam1")
    fetch_latest_clip("wlan1", "cam2")

    pcf_output.port[OUTPUT_PIN] = True

def main():
    pcf_output.port[OUTPUT_PIN] = True

    if not is_gopro_connected():
        try:
            run_connect_script()
        except Exception as e:
            print(f"[ERROR] Failed to connect to GoPro: {e}")
            sys.exit(1)
    else:
        print("[INFO] GoPro already reachable — skipping connection script.")

    print(f"Polling PCF8574@0x{I2C_ADDR_INPUT:02x} P{INPUT_PIN}... (Ctrl-C to stop)")

    try:
        while True:
            if not pcf_input.port[INPUT_PIN]:
                print("\n[TRIGGER] Input HIGH - starting recording")
                try:
                    record_and_fetch()
                except Exception as e:
                    print(f"[ERROR] record_and_fetch failed: {e}", flush=True)

                start = time.time()
                while pcf_input.port[INPUT_PIN] and (time.time() - start) < (DEBOUNCE_MS/1000):
                    time.sleep(POLL_INTERVAL)

                print("[INFO] Debounce done - ready for next trigger")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting.")
        sys.exit(0)

if __name__ == "__main__":
    main()
