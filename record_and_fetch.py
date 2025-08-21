#!/usr/bin/env python3
import time, os, sys, json, requests, subprocess
from datetime import datetime, timezone
from goprocam import GoProCamera, constants
import RPi.GPIO as GPIO
import signal
import sys
from pcf8574 import PCF8574
import asyncio
from bleak import BleakClient

RECORD_SECS    = 5
FINALIZE_SECS  = 2
DOWNLOAD_DIR   = "/media/pi/Clips/GoPro_Clips"
CAM_IP         = "10.5.5.9"
CAM_PORT       = 8080
LAST_CLIP_FILE = os.path.join(DOWNLOAD_DIR, ".last_clip")  # marker file
TRIGGER_PIN    = 17

I2C_BUS        = 1
I2C_ADDR_OUTPUT = 0x20
I2C_ADDR_INPUT = 0x38
INPUT_PIN      = 0
OUTPUT_PIN     = 0 
POLL_INTERVAL  = 0.05
DEBOUNCE_MS    = 200


pcf_input = PCF8574(I2C_BUS, I2C_ADDR_INPUT)
pcf_output = PCF8574(I2C_BUS, I2C_ADDR_OUTPUT)

def is_gopro_connected(ip=CAM_IP, timeout=2):
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


    
def record_and_fetch():
    # Ausgang setzen für Kamera Start
    pcf_output.port[OUTPUT_PIN] = False
    gp = GoProCamera.GoPro()

    # … your existing setup: mode, FOV, res/fps, zoom, etc. …

    # 6) record
    print("[REC] Recording for", RECORD_SECS, "seconds...", flush=True)
    gp.shoot_video(RECORD_SECS)
    print("[WAIT] Waiting", FINALIZE_SECS, "seconds...", flush=True)
    time.sleep(FINALIZE_SECS)

    # 7) fetch latest clip
    raw_media = gp.listMedia()
    media     = json.loads(raw_media)
    files     = media.get("media", [])[0].get("fs", [])
    videos    = [f for f in files if f.get("n","").lower().endswith(".mp4")]
    latest    = videos[-1]
    latest_name = latest["n"]

    # parse timestamp (your existing block)
    ts_raw = latest.get("d") or latest.get("mod") or ""
    dt = None
    if ts_raw.endswith("Z"):
        try: dt = datetime.fromisoformat(ts_raw.replace("Z","+00:00"))
        except: dt = None
    elif len(ts_raw)>=19 and ts_raw[8]=="T":
        try: dt = datetime.strptime(ts_raw, "%Y%m%dT%H%M%S%z")
        except: dt = None
    elif ts_raw.isdigit():
        try: dt = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
        except: dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)
    ts = dt.strftime("%Y%m%d_%H%M%S")

    # download
    camera_url = f"http://{CAM_IP}:{CAM_PORT}/videos/DCIM/100GOPRO/{latest_name}"
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    tmp_name   = latest_name
    final_dst  = os.path.join(DOWNLOAD_DIR, ts + os.path.splitext(latest_name)[1])

    print("[DL] Downloading", latest_name, "->", final_dst, flush=True)
    try:
        with requests.get(camera_url, stream=True, timeout=10) as r:
            r.raise_for_status()
            with open(tmp_name, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
    except Exception as e:
        print("[ERROR] Download failed:", e, flush=True)
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        return

    os.replace(tmp_name, final_dst)
    print("[OK] Saved clip to", final_dst, flush=True)
    pcf_output.port[OUTPUT_PIN] = True


def main():
    # Initialisierung
    # Ausgang zurücksetzen für Kamera Start
    pcf_output.port[OUTPUT_PIN] = True
    if not is_gopro_connected():
        try:
            run_connect_script()
        except Exception as e:
            print(f"[ERROR] Failed to connect to GoPro: {e}")
            sys.exit(1)
    else:
        print("[INFO] GoPro already reachable — skipping connection script.")

    
    print(f"Polling PCF8574@0x{I2C_ADDR_INPUT:02x} P{INPUT_PIN}... (Ctrl-C zum Stop)")

    try:
        while True:
            # 1) wait for any HIGH
            if not pcf_input.port[INPUT_PIN]:
                print("\n[TRIGGER] Eingang ist HIGH - starte Aufnahme")
                try:
                    record_and_fetch()
                except Exception as e:
                    print(f"[ERROR] record_and_fetch fehlgeschlagen: {e}", flush=True)

                # 2) Debounce: block until LOW or timeout
                start = time.time()
                while pcf_input.port[INPUT_PIN] and (time.time() - start) < (DEBOUNCE_MS/1000):
                    time.sleep(POLL_INTERVAL)

                print("[INFO] Debounce vorbei - bereit für nächsten Trigger")

            # 3) small sleep so we don't spin too hard
            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\nAbbruch durch Nutzer, beende...")
        sys.exit(0)



if __name__ == "__main__":
    main()
