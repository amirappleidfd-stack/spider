import os, shutil, subprocess, time, json, zipfile, requests, threading
from typing import Optional
from app.config import XRAY_VERSION, XRAY_DOWNLOAD_URL, XRAY_DIR, XRAY_BINARY, XRAY_CONFIG_PATH

_xray_process: Optional[subprocess.Popen] = None
_xray_lock = threading.Lock()

def is_xray_installed() -> bool:
    return os.path.isfile(XRAY_BINARY)

def download_xray() -> bool:
    zip_path = os.path.join(XRAY_DIR, "xray.zip")
    try:
        print(f"[xray] Downloading Xray-core {XRAY_VERSION}...")
        resp = requests.get(XRAY_DOWNLOAD_URL, stream=True, timeout=120)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                f.write(chunk)
                downloaded += len(chunk)
        print(f"[xray] Download complete ({downloaded // (1024*1024)}MB)")
        print("[xray] Extracting...")
        with zipfile.ZipFile(zip_path, "r") as z:
            for member in z.namelist():
                z.extract(member, XRAY_DIR)
        extracted_dir = os.path.join(XRAY_DIR, "Xray-linux-64")
        if os.path.isdir(extracted_dir):
            src = os.path.join(extracted_dir, "xray")
            if os.path.isfile(src):
                shutil.move(src, XRAY_BINARY)
                os.chmod(XRAY_BINARY, 0o755)
            for geo_file in ["geoip.dat", "geosite.dat"]:
                geo_src = os.path.join(extracted_dir, geo_file)
                if os.path.isfile(geo_src):
                    shutil.move(geo_src, os.path.join(XRAY_DIR, geo_file))
            shutil.rmtree(extracted_dir, ignore_errors=True)
        else:
            for name in ["xray", "geoip.dat", "geosite.dat"]:
                src = os.path.join(XRAY_DIR, name)
                if os.path.isfile(src):
                    if name == "xray":
                        os.chmod(src, 0o755)
        os.remove(zip_path)
        print("[xray] Installation complete!")
        return is_xray_installed()
    except Exception as e:
        print(f"[xray] Download failed: {e}")
        return False

def ensure_xray() -> bool:
    if is_xray_installed():
        print("[xray] Xray-core already installed")
        return True
    return download_xray()

def generate_xray_config(inbounds=None, outbounds=None) -> dict:
    if inbounds is None:
        inbounds = []
    if outbounds is None:
        outbounds = [{"protocol": "freedom", "tag": "direct"}, {"protocol": "blackhole", "tag": "block"}]
    return {"log": {"loglevel": "warning"}, "inbounds": inbounds, "outbounds": outbounds}

def start_xray() -> bool:
    global _xray_process
    with _xray_lock:
        if _xray_process and _xray_process.poll() is None:
            print("[xray] Already running")
            return True
        if not is_xray_installed():
            if not ensure_xray():
                return False
        if not os.path.isfile(XRAY_CONFIG_PATH):
            config = generate_xray_config()
            write_xray_config(config)
        try:
            print("[xray] Starting Xray-core...")
            _xray_process = subprocess.Popen(
                [XRAY_BINARY, "run", "-c", XRAY_CONFIG_PATH],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            time.sleep(1)
            if _xray_process.poll() is not None:
                stderr = _xray_process.stderr.read().decode() if _xray_process.stderr else ""
                print(f"[xray] Failed to start: {stderr}")
                return False
            print(f"[xray] Xray-core running (PID: {_xray_process.pid})")
            return True
        except Exception as e:
            print(f"[xray] Start error: {e}")
            return False

def stop_xray() -> None:
    global _xray_process
    with _xray_lock:
        if _xray_process:
            try:
                _xray_process.terminate()
                _xray_process.wait(timeout=5)
            except Exception:
                try:
                    _xray_process.kill()
                except Exception:
                    pass
            _xray_process = None
            print("[xray] Stopped")

def is_xray_running() -> bool:
    with _xray_lock:
        return _xray_process is not None and _xray_process.poll() is None

def write_xray_config(config: dict) -> bool:
    try:
        with open(XRAY_CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        print(f"[xray] Config write error: {e}")
        return False

def reload_xray() -> bool:
    stop_xray()
    return start_xray()

def get_xray_status() -> dict:
    return {"installed": is_xray_installed(), "running": is_xray_running(),
            "version": XRAY_VERSION, "binary": XRAY_BINARY, "config": XRAY_CONFIG_PATH}

def get_system_stats() -> dict:
    stats = {"cpu_percent": 0, "ram_percent": 0, "ram_used_mb": 0,
             "ram_total_mb": 0, "disk_percent": 0, "disk_used_gb": 0, "disk_total_gb": 0}
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        parts = line.split()
        total = sum(int(p) for p in parts[1:])
        idle = int(parts[4])
        stats["cpu_percent"] = round((1 - idle / max(total, 1)) * 100, 1)
    except Exception:
        try:
            with open("/proc/loadavg") as f:
                load = float(f.read().split()[0])
                stats["cpu_percent"] = round(min(load * 25, 100), 1)
        except Exception:
            pass
    try:
        import psutil
        ram = psutil.virtual_memory()
        stats["ram_percent"] = ram.percent
        stats["ram_used_mb"] = ram.used // (1024 * 1024)
        stats["ram_total_mb"] = ram.total // (1024 * 1024)
        disk = psutil.disk_usage("/")
        stats["disk_percent"] = disk.percent
        stats["disk_used_gb"] = round(disk.used / (1024 ** 3), 1)
        stats["disk_total_gb"] = round(disk.total / (1024 ** 3), 1)
    except ImportError:
        try:
            stat = os.statvfs("/")
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            used = total - free
            stats["disk_percent"] = round(used / max(total, 1) * 100, 1)
            stats["disk_used_gb"] = round(used / (1024 ** 3), 1)
            stats["disk_total_gb"] = round(total / (1024 ** 3), 1)
        except Exception:
            pass
        try:
            with open("/proc/meminfo") as f:
                meminfo = f.read()
            def _get_kb(key):
                for line in meminfo.split("\n"):
                    if line.startswith(key):
                        return int(line.split()[1])
                return 0
            total_kb = _get_kb("MemTotal")
            avail_kb = _get_kb("MemAvailable")
            used_kb = total_kb - avail_kb
            stats["ram_total_mb"] = total_kb // 1024
            stats["ram_used_mb"] = used_kb // 1024
            stats["ram_percent"] = round(used_kb / max(total_kb, 1) * 100, 1)
        except Exception:
            pass
    return stats