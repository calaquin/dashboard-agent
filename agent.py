#!/usr/bin/env python3

import argparse
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import py_compile
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid

def parse_port(value, default=8100):
    try:
        val = int(value)
        if 1 <= val <= 65535:
            return val
    except (TypeError, ValueError):
        pass
    return default


PORT = parse_port(os.environ.get("DASHBOARD_AGENT_PORT"), 8100)

AGENT_NAME = "dashboard-agent"
AGENT_VERSION = "0.3.3"
AGENT_VERSION = "0.3.5"
AGENT_VERSION = "0.3.6"
STATUS_SCHEMA_NAME = "dashboard-agent-status"
STATUS_SCHEMA_VERSION = 1

DATA_DIR = Path(os.environ.get("DASHBOARD_AGENT_DATA_DIR", "/var/lib/dashboard-agent"))
CONFIG_DIR = Path(os.environ.get("DASHBOARD_AGENT_CONFIG_DIR", "/etc/dashboard-agent"))
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def crockford_base32_encode(raw_bytes):
    if len(raw_bytes) != 5:
        raise ValueError("Expected 5 bytes for 40-bit Crockford Base32 encoding")
    val = int.from_bytes(raw_bytes, "big")
    chars = []
    for _ in range(8):
        chars.append(CROCKFORD_ALPHABET[val & 0x1F])
        val >>= 5
    chars.reverse()
    code = "".join(chars)
    return code[:4] + "-" + code[4:]


def compute_verification_code(bootstrap_token, enrollment_id, agent_id):
    prefix = b"kindle-dashboard-enrollment-v1"
    message = (
        prefix
        + b"\x00"
        + enrollment_id.encode("utf-8")
        + b"\x00"
        + agent_id.encode("utf-8")
    )
    digest = hmac.new(
        bootstrap_token.encode("utf-8"),
        message,
        hashlib.sha256
    ).digest()
    return crockford_base32_encode(digest[:5])


def get_or_create_agent_id(data_dir=None):
    if data_dir is None:
        data_dir = DATA_DIR
    agent_id_file = Path(data_dir) / "agent-id"
    try:
        if agent_id_file.exists():
            content = agent_id_file.read_text(encoding="utf-8").strip()
            if content and re.match(r"^[0-9a-fA-F-]{36}$", content):
                return content.lower()
    except Exception:
        pass
    new_id = str(uuid.uuid4())
    try:
        data_dir_path = Path(data_dir)
        data_dir_path.mkdir(parents=True, exist_ok=True)
        tmp_file = data_dir_path / (".agent-id.tmp.%d" % os.getpid())
        tmp_file.write_text(new_id + "\n", encoding="utf-8")
        try:
            os.chmod(tmp_file, 0o644)
        except OSError:
            pass
        os.replace(tmp_file, agent_id_file)
    except Exception:
        pass
    return new_id


def atomic_write_json(file_path, data, mode=0o600):
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / (".%s.tmp.%d" % (path.name, os.getpid()))
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    try:
        os.chmod(tmp_path, mode)
    except OSError:
        pass
    os.replace(tmp_path, path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


CAPABILITIES = [
    "status.v1",
    "metrics.host",
    "metrics.network",
    "metrics.storage",
    "metrics.docker",
    "metrics.snapraid",
    "profile.hardware.v1",
    "controls.docker.v1",
    "controls.power.v1",
    "controls.update.v1",
    "controls.uninstall.v1",
]

STATUS_CACHE_SECONDS = 5
SNAPRAID_CACHE_SECONDS = 60

MOUNTS = [
    ("/", "System"),
    ("/srv/media", "Media"),
    ("/srv/staging", "Staging"),
    ("/srv/docker", "Docker"),
]

SERVICES = [
    ("Plex", ["plex"]),
    ("Jellyfin", ["jellyfin"]),
    ("Sonarr", ["video-sonarr"]),
    ("Radarr", ["video-radarr"]),
    ("Lidarr", ["music-lidarr"]),
    ("SABnzbd", ["downloading-sabnzbd"]),
    ("Home Assistant", ["homeassistant"]),
    ("ESPHome", ["esphome"]),
    ("Joplin", ["joplin-server"]),
    ("UpSnap", ["upsnap"]),
    ("Twingate", ["twingate"]),
    ("Samba", ["samba"]),
    ("Portainer", ["portainer"]),
    ("CalWriter", ["calwriter"]),
    ("Tdarr", ["tdarr"]),
    ("CommonFrame DB", ["postgres-commonframe-home"]),
]

status_cache = {
    "time": 0,
    "data": None
}

snapraid_cache = {
    "time": 0,
    "data": None
}

cache_lock = threading.Lock()


def run_command(args, timeout=3):
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )

        return (
            result.returncode,
            result.stdout.strip(),
            result.stderr.strip()
        )

    except Exception as exc:
        return (1, "", str(exc))


def read_cpu_sample():
    with open("/proc/stat", "r") as handle:
        line = handle.readline()

    parts = line.split()[1:]
    values = [int(value) for value in parts]

    idle = values[3]

    if len(values) > 4:
        idle += values[4]

    return sum(values), idle


def cpu_percent():
    try:
        total1, idle1 = read_cpu_sample()
        time.sleep(0.12)
        total2, idle2 = read_cpu_sample()

        total_delta = total2 - total1
        idle_delta = idle2 - idle1

        if total_delta <= 0:
            return 0

        value = (
            (total_delta - idle_delta)
            * 100.0
            / total_delta
        )

        return round(value, 1)

    except Exception:
        return None


def memory_status():
    values = {}

    try:
        with open("/proc/meminfo", "r") as handle:
            for line in handle:
                parts = line.replace(":", "").split()

                if len(parts) >= 2:
                    values[parts[0]] = int(parts[1])

        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable")

        if available is None:
            available = (
                values.get("MemFree", 0)
                + values.get("Buffers", 0)
                + values.get("Cached", 0)
            )

        used = total - available

        percent = (
            round(used * 100.0 / total, 1)
            if total
            else None
        )

        return {
            "total_mb": round(total / 1024.0),
            "used_mb": round(used / 1024.0),
            "percent": percent
        }

    except Exception:
        return {
            "total_mb": None,
            "used_mb": None,
            "percent": None
        }


def temperature():
    candidates = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/hwmon/hwmon0/temp1_input",
    ]

    for path in candidates:
        try:
            with open(path, "r") as handle:
                value = float(handle.read().strip())

            if value > 1000:
                value /= 1000.0

            return round(value, 1)

        except Exception:
            pass

    return None


def uptime_seconds():
    try:
        with open("/proc/uptime", "r") as handle:
            return int(float(handle.read().split()[0]))

    except Exception:
        return None


def load_average():
    try:
        with open("/proc/loadavg", "r") as handle:
            return float(handle.read().split()[0])

    except Exception:
        return None


def hostname():
    try:
        return socket.gethostname()

    except Exception:
        return "unknown"


def lan_ip():
    code, output, _ = run_command(["hostname", "-I"])

    if code != 0:
        return None

    addresses = output.split()

    # Prefer the normal LAN address over Docker bridges.
    for address in addresses:
        if address.startswith("10."):
            return address

    for address in addresses:
        if not address.startswith("172."):
            return address

    return addresses[0] if addresses else None


def storage_status():
    result = []

    for path, label in MOUNTS:
        if not os.path.exists(path):
            continue

        # Avoid displaying a normal directory as a separate filesystem.
        if path != "/" and not os.path.ismount(path):
            continue

        try:
            usage = shutil.disk_usage(path)

            percent = (
                usage.used * 100.0 / usage.total
                if usage.total
                else 0
            )

            result.append({
                "label": label,
                "path": path,
                "total_gb": round(
                    usage.total / 1024.0 / 1024.0 / 1024.0,
                    1
                ),
                "used_gb": round(
                    usage.used / 1024.0 / 1024.0 / 1024.0,
                    1
                ),
                "free_gb": round(
                    usage.free / 1024.0 / 1024.0 / 1024.0,
                    1
                ),
                "percent": round(percent, 1)
            })

        except Exception:
            pass

    return result


def docker_status():
    code, output, error = run_command([
        "docker",
        "ps",
        "-a",
        "--format",
        "{{.Names}}|{{.Image}}|{{.State}}|{{.Status}}"
    ])

    if code != 0:
        return {
            "available": False,
            "error": error or output,
            "total": 0,
            "running": 0,
            "containers": [],
            "services": []
        }

    containers = []

    for line in output.splitlines():
        parts = line.split("|", 3)

        if len(parts) != 4:
            continue

        containers.append({
            "name": parts[0],
            "image": parts[1],
            "docker_state": parts[2].lower(),
            "status": parts[3]
        })

    display_containers = []

    for container in containers:
        docker_state = container["docker_state"]
        status = container["status"]

        if docker_state == "running":
            state = (
                "warning"
                if "unhealthy" in status.lower()
                else "up"
            )
        elif docker_state in ("paused", "restarting"):
            state = "warning"
        else:
            state = "down"

        display_containers.append({
            "name": container["name"],
            "image": container["image"],
            "state": state,
            "detail": status
        })

    state_order = {
        "warning": 0,
        "down": 1,
        "up": 2
    }

    display_containers.sort(
        key=lambda container: (
            state_order.get(container["state"], 3),
            container["name"].lower()
        )
    )

    services = []

    for label, patterns in SERVICES:
        matches = []

        for container in containers:
            haystack = (
                container["name"]
                + " "
                + container["image"]
            ).lower()

            for pattern in patterns:
                if pattern.lower() in haystack:
                    matches.append(container)
                    break

        if not matches:
            services.append({
                "name": label,
                "state": "missing",
                "detail": "Not found"
            })
            continue

        healthy_running = False
        unhealthy_running = False

        for container in matches:
            if container["docker_state"] == "running":
                if "unhealthy" in container["status"].lower():
                    unhealthy_running = True
                else:
                    healthy_running = True

        if healthy_running:
            state = "up"
            detail = "Running"

        elif unhealthy_running:
            state = "warning"
            detail = "Unhealthy"

        else:
            state = "down"
            detail = matches[0]["status"]

        services.append({
            "name": label,
            "state": state,
            "detail": detail
        })

    running = sum(
        1
        for container in containers
        if container["docker_state"] == "running"
    )

    return {
        "available": True,
        "total": len(containers),
        "running": running,
        "containers": display_containers,
        "services": services
    }


def default_gateway():
    code, output, _ = run_command([
        "ip",
        "route",
        "show",
        "default"
    ])

    if code == 0 and output:
        parts = output.split()
        try:
            index = parts.index("via")
            return parts[index + 1]
        except Exception:
            pass

    # Fallback: parse /proc/net/route
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.strip().split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    gw_hex = parts[2]
                    if gw_hex != "00000000":
                        gw_int = int(gw_hex, 16)
                        return socket.inet_ntoa(struct.pack("<L", gw_int))
    except Exception:
        pass

    return None


def ping(host):
    if not host:
        return False

    code, _, _ = run_command(
        ["ping", "-c", "1", "-W", "1", host],
        timeout=2
    )

    if code == 0:
        return True

    # Fallback 1: TCP probe to common router ports (DNS 53, HTTP 80, HTTPS 443)
    # 0 = open, 111 (ECONNREFUSED) = router is online and active
    for port in (53, 80, 443):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            res = sock.connect_ex((host, port))
            sock.close()
            if res in (0, 111):
                return True
        except Exception:
            pass

    # Fallback 2: Check ARP cache (/proc/net/arp)
    try:
        with open("/proc/net/arp", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == host:
                    flags = int(parts[2], 16) if parts[2].startswith("0x") else int(parts[2])
                    if flags > 0 and parts[3] != "00:00:00:00:00:00":
                        return True
    except Exception:
        pass

    return False


def wan_available():
    sock = None

    try:
        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM
        )

        sock.settimeout(1.5)
        sock.connect(("1.1.1.1", 53))
        return True

    except Exception:
        return False

    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def snapraid_status():
    now = time.time()

    if (
        snapraid_cache["data"] is not None
        and now - snapraid_cache["time"]
            < SNAPRAID_CACHE_SECONDS
    ):
        return snapraid_cache["data"]

    executable = shutil.which("snapraid")

    if not executable:
        data = {
            "available": False,
            "state": "missing",
            "summary": "SnapRAID not found"
        }

    else:
        code, output, error = run_command(
            [executable, "status"],
            timeout=8
        )
        if code != 0:
            code, output, error = run_command(
                ["sudo", "-n", executable, "status"],
                timeout=8
            )

        combined = (output + "\n" + error).strip()
        lower = combined.lower()

        has_status_output = any(kw in lower for kw in ("no error detected", "scrub status", "array status", "self test", "files with"))
        if code != 0 and not has_status_output:
            summary = "Sudo required for snapraid status" if ("password" in lower or "sudo" in lower) else "SnapRAID status failed"
            data = {
                "available": True,
                "state": "warning",
                "summary": summary,
                "detail": combined[:200]
            }
        else:
            errors = 0
            scrub_percent = None
            unsynced_files = 0
            oldest_scrub_days = None

            for line in combined.splitlines():
                clean = line.strip()
                l_clean = clean.lower()

                if "no error detected" in l_clean:
                    errors = 0
                elif "error detected" in l_clean or "errors detected" in l_clean:
                    m = re.search(r'(\d+)\s+error', l_clean)
                    errors = int(m.group(1)) if m else 1

                m_scrub = re.search(r'(\d+)%\s+of the array is scrubbed', l_clean)
                if m_scrub:
                    scrub_percent = int(m_scrub.group(1))

                m_oldest = re.search(r'oldest block was scrubbed (\d+) days ago', l_clean)
                if m_oldest:
                    oldest_scrub_days = int(m_oldest.group(1))

                m_mod = re.search(r'you have (\d+) files with', l_clean)
                if m_mod:
                    unsynced_files += int(m_mod.group(1))

            if errors > 0:
                state = "error"
            elif unsynced_files > 500 or (oldest_scrub_days is not None and oldest_scrub_days > 14):
                state = "warning"
            else:
                state = "ok"

            summary_parts = []
            if scrub_percent is not None:
                summary_parts.append("%s%% scrubbed" % scrub_percent)
            if unsynced_files > 0:
                summary_parts.append("%s unsynced" % unsynced_files)
            if oldest_scrub_days is not None:
                summary_parts.append("scrubbed %sd ago" % oldest_scrub_days)

            summary = " · ".join(summary_parts) if summary_parts else ("No errors detected" if state == "ok" else "Check status")

            data = {
                "available": True,
                "state": state,
                "summary": summary,
                "errors": errors,
                "scrub_percent": scrub_percent,
                "unsynced_files": unsynced_files,
                "oldest_scrub_days": oldest_scrub_days
            }

    snapraid_cache["time"] = now
    snapraid_cache["data"] = data

    return data


def build_status():
    gateway = default_gateway()
    memory = memory_status()

    return {
        "schema": {
            "name": STATUS_SCHEMA_NAME,
            "version": STATUS_SCHEMA_VERSION
        },

        "agent": {
            "name": AGENT_NAME,
            "version": AGENT_VERSION,
            "agent_id": get_or_create_agent_id(AgentHandler.data_dir),
            "capabilities": list(CAPABILITIES)
        },

        "generated": int(time.time()),

        "host": {
            "hostname": hostname(),
            "ip": lan_ip(),
            "cpu_percent": cpu_percent(),
            "memory_percent": memory["percent"],
            "memory_used_mb": memory["used_mb"],
            "memory_total_mb": memory["total_mb"],
            "temperature_c": temperature(),
            "uptime_seconds": uptime_seconds(),
            "load1": load_average()
        },

        "network": {
            "gateway": gateway,
            "lan": ping(gateway),
            "wan": wan_available()
        },

        "storage": storage_status(),

        "docker": docker_status(),

        "snapraid": snapraid_status()
    }


def cached_status():
    now = time.time()

    with cache_lock:
        if (
            status_cache["data"] is not None
            and now - status_cache["time"]
                < STATUS_CACHE_SECONDS
        ):
            return status_cache["data"]

        data = build_status()

        status_cache["time"] = now
        status_cache["data"] = data

        return data


class JsonHandlerMixin:

    def send_json(self, status, data):
        payload = json.dumps(
            data,
            separators=(",", ":")
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Cache-Control",
            "no-store, no-cache, must-revalidate"
        )
        self.end_headers()

        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        print(
            "%s - %s"
            % (
                self.address_string(),
                fmt % args
            ),
            flush=True
        )


def collect_cpu_profile():
    cpu_info = {
        "model": "Unknown CPU",
        "arch": os.uname().machine if hasattr(os, "uname") else "unknown",
        "cores_physical": 1,
        "threads_logical": os.cpu_count() or 1,
        "mhz_max": None,
        "mhz_min": None,
        "mhz_cur": None,
        "l1_cache": None,
        "l2_cache": None,
        "l3_cache": None,
        "flags": []
    }
    try:
        if Path("/proc/cpuinfo").exists():
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as handle:
                core_ids = set()
                flags = set()
                for line in handle:
                    line = line.strip()
                    if ":" in line:
                        k, v = line.split(":", 1)
                        k = k.strip().lower()
                        v = v.strip()
                        if k in ("model name", "hardware", "cpu architecture", "processor"):
                            if cpu_info["model"] == "Unknown CPU" and v:
                                cpu_info["model"] = v
                        elif k == "cpu mhz" and not cpu_info["mhz_cur"]:
                            try:
                                cpu_info["mhz_cur"] = round(float(v), 1)
                            except ValueError:
                                pass
                        elif k == "core id":
                            core_ids.add(v)
                        elif k in ("flags", "features"):
                            flags.update(v.split())
                if core_ids:
                    cpu_info["cores_physical"] = len(core_ids)
                else:
                    cpu_info["cores_physical"] = cpu_info["threads_logical"]
                cpu_info["flags"] = sorted(list(flags))[:30]
    except Exception:
        pass

    try:
        max_freq_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
        if max_freq_path.exists():
            cpu_info["mhz_max"] = round(int(max_freq_path.read_text().strip()) / 1000.0, 1)
        min_freq_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq")
        if min_freq_path.exists():
            cpu_info["mhz_min"] = round(int(min_freq_path.read_text().strip()) / 1000.0, 1)
    except Exception:
        pass

    return cpu_info


def collect_platform_profile():
    plat = {
        "board_vendor": "",
        "board_name": "",
        "product_name": "",
        "bios_version": "",
        "virtualization": "none",
        "uefi": False
    }
    try:
        dmi_path = Path("/sys/class/dmi/id")
        if dmi_path.exists():
            for key, field in [
                ("sys_vendor", "board_vendor"),
                ("board_name", "board_name"),
                ("product_name", "product_name"),
                ("bios_version", "bios_version")
            ]:
                f = dmi_path / key
                if f.exists():
                    try:
                        plat[field] = f.read_text().strip()
                    except Exception:
                        pass
    except Exception:
        pass

    try:
        dt_model = Path("/sys/firmware/devicetree/base/model")
        if dt_model.exists():
            plat["board_name"] = dt_model.read_bytes().decode("utf-8", errors="replace").replace("\x00", "").strip()
    except Exception:
        pass

    if Path("/sys/firmware/efi").exists():
        plat["uefi"] = True

    code, out, _ = run_command(["systemd-detect-virt"], timeout=2)
    if code == 0 and out.strip():
        plat["virtualization"] = out.strip()

    return plat


def collect_os_profile():
    os_info = {
        "distro": "Linux",
        "version": "",
        "kernel": os.uname().release if hasattr(os, "uname") else "",
        "arch": os.uname().machine if hasattr(os, "uname") else ""
    }
    try:
        if Path("/etc/os-release").exists():
            with open("/etc/os-release", "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        v = v.strip('"\'')
                        if k == "PRETTY_NAME":
                            os_info["distro"] = v
                        elif k == "VERSION_ID" and not os_info["version"]:
                            os_info["version"] = v
    except Exception:
        pass
    return os_info


def collect_memory_profile():
    mem = memory_status()
    total_mb = mem.get("total_mb", 0)
    used_mb = mem.get("used_mb", 0)
    free_mb = max(0, total_mb - used_mb)
    return {
        "total_mb": total_mb,
        "used_mb": used_mb,
        "free_mb": free_mb,
        "percent": mem.get("percent")
    }


def collect_network_interfaces():
    ifaces = []
    net_path = Path("/sys/class/net")
    if net_path.exists():
        try:
            for iface_dir in sorted(net_path.iterdir()):
                name = iface_dir.name
                if name == "lo":
                    continue
                mac = ""
                speed = None
                operstate = "unknown"
                try:
                    addr_file = iface_dir / "address"
                    if addr_file.exists():
                        mac = addr_file.read_text().strip().upper()
                except Exception:
                    pass
                try:
                    speed_file = iface_dir / "speed"
                    if speed_file.exists():
                        val = int(speed_file.read_text().strip())
                        if val > 0:
                            speed = "%d Mbps" % val
                except Exception:
                    pass
                try:
                    state_file = iface_dir / "operstate"
                    if state_file.exists():
                        operstate = state_file.read_text().strip()
                except Exception:
                    pass
                ifaces.append({
                    "name": name,
                    "mac": mac,
                    "speed": speed,
                    "state": operstate
                })
        except Exception:
            pass
    return ifaces


def collect_gpu_profile():
    gpus = []
    code, out, _ = run_command(["lspci"], timeout=3)
    if code == 0:
        for line in out.splitlines():
            line_lower = line.lower()
            if "vga compatible controller" in line_lower or "3d controller" in line_lower or "display controller" in line_lower:
                parts = line.split(":", 2)
                name = parts[-1].strip() if len(parts) >= 3 else line.strip()
                gpus.append({"model": name, "type": "PCIe"})
    if not gpus:
        code_nvi, out_nvi, _ = run_command(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], timeout=3)
        if code_nvi == 0:
            for line in out_nvi.splitlines():
                if line.strip():
                    gpus.append({"model": line.strip(), "type": "NVIDIA"})
    return gpus


def collect_storage_devices():
    devices = []
    code, out, _ = run_command(["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MODEL,TRAN,ROTA"], timeout=3)
    if code == 0:
        try:
            parsed = json.loads(out)
            for block in parsed.get("blockdevices", []):
                if block.get("type") in ("disk", "rom"):
                    devices.append({
                        "name": block.get("name", ""),
                        "size": block.get("size", ""),
                        "model": (block.get("model") or "").strip(),
                        "transport": block.get("tran", ""),
                        "rotational": bool(block.get("rota", 1))
                    })
        except Exception:
            pass
    return devices


def collect_hardware_profile():
    return {
        "cpu": collect_cpu_profile(),
        "platform": collect_platform_profile(),
        "os": collect_os_profile(),
        "memory": collect_memory_profile(),
        "network_interfaces": collect_network_interfaces(),
        "gpu": collect_gpu_profile(),
        "storage_devices": collect_storage_devices(),
        "timestamp": int(time.time())
    }


ALLOWED_ACTIONS = {"start", "stop", "restart", "pause", "unpause"}


def is_container_allowlisted(name):
    if not re.match(r"^[a-zA-Z0-9_.-]+$", name):
        return False
    allowlist_env = os.environ.get("DASHBOARD_AGENT_DOCKER_ALLOWLIST", "").strip()
    if allowlist_env == "*":
        return True
    if allowlist_env:
        allowed = {item.strip() for item in allowlist_env.split(",") if item.strip()}
        return name in allowed
    return True


def execute_container_action(name, action):
    if action not in ALLOWED_ACTIONS:
        return (400, {"error": "Invalid action"})
    if not is_container_allowlisted(name):
        return (403, {"error": "Container is not allowlisted"})
    code, out, err = run_command(["docker", action, name], timeout=15)
    if code != 0:
        return (500, {"error": err or "Failed to %s container %s" % (action, name)})
    return (200, {"ok": True, "container": name, "action": action, "output": out})


def get_container_logs(name, lines=100):
    if not is_container_allowlisted(name):
        return (403, {"error": "Container is not allowlisted"})
    lines = min(max(int(lines), 1), 500)
    code, out, err = run_command(["docker", "logs", "--tail", str(lines), name], timeout=10)
    if code != 0:
        return (500, {"error": err or "Failed to fetch logs for %s" % name})
    return (200, {"ok": True, "container": name, "logs": out})


def execute_host_power_action(action):
    allow_power = os.environ.get("DASHBOARD_AGENT_ALLOW_POWER", "").lower() in ("1", "true", "yes")
    if not allow_power:
        return (403, {"error": "Host power management is disabled on this agent"})
    if action == "reboot":
        base_cmd = ["/bin/systemctl", "reboot"] if shutil.which("systemctl") else ["/sbin/reboot"]
    elif action == "shutdown":
        base_cmd = ["/bin/systemctl", "poweroff"] if shutil.which("systemctl") else ["/sbin/shutdown", "-h", "now"]
    else:
        return (400, {"error": "Invalid power action"})

    code, out, err = run_command(base_cmd, timeout=10)
    if code != 0:
        sudo_cmd = ["sudo", "-n"] + base_cmd
        code, out, err = run_command(sudo_cmd, timeout=10)
    if code != 0:
        return (500, {"error": err or "Failed to %s host" % action})
    return (200, {"ok": True, "action": action, "output": out})


def determine_service_name(data_dir=None):
    target_data_dir = Path(data_dir).resolve() if data_dir else DATA_DIR.resolve()
    dir_name = target_data_dir.name
    if dir_name == "dashboard-agent":
        return "dashboard-agent"
    if dir_name.startswith("dashboard-agent-"):
        instance = dir_name[len("dashboard-agent-"):]
        return "dashboard-agent@%s" % instance
    return "dashboard-agent"


def execute_uninstallation(data_dir=None, purge_data=True, remove_service=True):
    target_data_dir = Path(data_dir).resolve() if data_dir else DATA_DIR.resolve()
    service_name = determine_service_name(target_data_dir)

    def _cleanup_worker():
        time.sleep(0.5)
        # 1. Stop and disable systemd service
        if remove_service:
            for cmd in (
                ["systemctl", "stop", service_name],
                ["sudo", "-n", "systemctl", "stop", service_name],
                ["systemctl", "disable", service_name],
                ["sudo", "-n", "systemctl", "disable", service_name],
            ):
                try:
                    subprocess.run(cmd, capture_output=True, timeout=5)
                except Exception:
                    pass

        # 2. Purge data directory (credentials, agent-id, logs, agent.env)
        if purge_data and target_data_dir.exists():
            try:
                for item in list(target_data_dir.iterdir()):
                    try:
                        if item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink()
                    except Exception:
                        pass
                try:
                    target_data_dir.rmdir()
                except Exception:
                    pass
            except Exception:
                pass

        # 3. Terminate agent process
        os._exit(0)

    t = threading.Thread(target=_cleanup_worker)
    t.daemon = True
    t.start()

    return {
        "ok": True,
        "status": "uninstalling",
        "service": service_name,
        "data_dir": str(target_data_dir),
        "message": "Agent service stopping and instance data removed."
    }


class AgentHandler(JsonHandlerMixin, http.server.BaseHTTPRequestHandler):

    agent_token = ""
    data_dir = DATA_DIR

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length or length > 65536:
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw)
        except Exception:
            return None

    @classmethod
    def get_active_token(cls):
        if cls.agent_token:
            return cls.agent_token
        creds_file = Path(cls.data_dir) / "credentials.json"
        if creds_file.exists():
            try:
                data = json.loads(creds_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("token"):
                    cls.agent_token = str(data["token"])
                    return cls.agent_token
            except Exception:
                pass
        legacy_file = Path(os.environ.get("DASHBOARD_AGENT_CONFIG", "/etc/dashboard-agent"))
        if legacy_file.exists() and legacy_file.is_file():
            try:
                for line in legacy_file.read_text(encoding="utf-8").splitlines():
                    if line.startswith("DASHBOARD_AGENT_TOKEN="):
                        cls.agent_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        return cls.agent_token
            except Exception:
                pass
        return ""

    @classmethod
    def get_enrollment(cls):
        enrollment_file = Path(cls.data_dir) / "enrollment.json"
        if not enrollment_file.exists():
            return None
        try:
            data = json.loads(enrollment_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            expires_at = data.get("expires_at")
            if expires_at is not None and time.time() > float(expires_at):
                try:
                    enrollment_file.unlink()
                except OSError:
                    pass
                return {"expired": True, "enrollment_id": data.get("enrollment_id")}
            return data
        except Exception:
            return None

    def is_authorized(self):
        supplied = self.headers.get("Authorization", "")
        token = getattr(self, "agent_token", "") or self.get_active_token()
        if not token:
            return False
        expected = "Bearer " + token
        return hmac.compare_digest(supplied, expected)

    def check_enrollment_auth(self):
        enrollment = self.get_enrollment()
        if not enrollment:
            return False, 404, {"error": "Enrollment not active"}
        if enrollment.get("expired"):
            return False, 410, {"error": "Enrollment expired"}

        bootstrap_token = enrollment.get("bootstrap_token", "")
        if not bootstrap_token:
            return False, 401, {"error": "Invalid enrollment state"}

        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + bootstrap_token
        if not hmac.compare_digest(supplied, expected):
            return False, 401, {"error": "Unauthorized bootstrap credential"}

        return True, 200, enrollment

    def do_GET(self):
        url_parts = urllib.parse.urlsplit(self.path)
        path = url_parts.path
        query = urllib.parse.parse_qs(url_parts.query)

        if path == "/health":
            self.send_json(200, {
                "ok": True,
                "agent": {
                    "name": AGENT_NAME,
                    "version": AGENT_VERSION,
                    "agent_id": get_or_create_agent_id(self.data_dir)
                },
                "schema": {
                    "name": STATUS_SCHEMA_NAME,
                    "version": STATUS_SCHEMA_VERSION
                }
            })
            return

        if path == "/api/enroll/probe":
            ok, code, payload = self.check_enrollment_auth()
            if not ok:
                self.send_json(code, payload)
                return
            agent_id = get_or_create_agent_id(self.data_dir)
            self.send_json(200, {
                "agent_id": agent_id,
                "hostname": hostname(),
                "agent_name": AGENT_NAME,
                "agent_version": AGENT_VERSION,
                "schema_version": STATUS_SCHEMA_VERSION,
                "capabilities": list(CAPABILITIES),
                "enrollment_id": payload.get("enrollment_id")
            })
            return

        if not self.is_authorized():
            self.send_json(401, {"error": "Unauthorized"})
            return

        if path == "/api/status":
            try:
                self.send_json(200, cached_status())
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})
            return

        if path == "/api/profile":
            try:
                self.send_json(200, collect_hardware_profile())
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})
            return

        logs_match = re.match(r"^/api/containers/([a-zA-Z0-9_.-]+)/logs$", path)
        if logs_match:
            container_name = logs_match.group(1)
            try:
                lines = int(query.get("lines", [100])[0])
            except (ValueError, TypeError):
                lines = 100
            status_code, response = get_container_logs(container_name, lines)
            self.send_json(status_code, response)
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path

        if path == "/api/enroll/commit":
            body_json = self.read_json()
            if not isinstance(body_json, dict) or not body_json.get("permanent_token"):
                self.send_json(400, {"error": "Missing permanent_token"})
                return

            permanent_token = str(body_json["permanent_token"]).strip()
            if not permanent_token:
                self.send_json(400, {"error": "Invalid permanent_token"})
                return

            active = self.get_active_token()
            if active and hmac.compare_digest(active, permanent_token):
                agent_id = get_or_create_agent_id(self.data_dir)
                self.send_json(200, {
                    "ok": True,
                    "agent_id": agent_id,
                    "status": "committed",
                    "idempotent": True
                })
                return

            ok, code, payload = self.check_enrollment_auth()
            if not ok:
                self.send_json(code, payload)
                return

            agent_id = get_or_create_agent_id(self.data_dir)
            creds_data = {
                "token": permanent_token,
                "agent_id": agent_id,
                "created_at": int(time.time())
            }
            creds_file = Path(self.data_dir) / "credentials.json"
            atomic_write_json(creds_file, creds_data, mode=0o600)

            AgentHandler.agent_token = permanent_token

            enrollment_file = Path(self.data_dir) / "enrollment.json"
            if enrollment_file.exists():
                try:
                    enrollment_file.unlink()
                except OSError:
                    pass

            self.send_json(200, {
                "ok": True,
                "agent_id": agent_id,
                "status": "committed"
            })
            return

        if not self.is_authorized():
            self.send_json(401, {"error": "Unauthorized"})
            return

        action_match = re.match(
            r"^/api/containers/([a-zA-Z0-9_.-]+)/(start|stop|restart|pause|unpause)$",
            path
        )
        if action_match:
            container_name = action_match.group(1)
            action = action_match.group(2)
            status_code, response = execute_container_action(container_name, action)
            self.send_json(status_code, response)
            return

        power_match = re.match(r"^/api/power/(reboot|shutdown)$", path)
        if power_match:
            action = power_match.group(1)
            status_code, response = execute_host_power_action(action)
            self.send_json(status_code, response)
            return

        if path == "/api/uninstall":
            body = self.read_json()
            purge_data = True
            remove_service = True
            if isinstance(body, dict):
                if "purge_data" in body:
                    purge_data = bool(body["purge_data"])
                if "remove_service" in body:
                    remove_service = bool(body["remove_service"])

            res = execute_uninstallation(
                data_dir=self.data_dir,
                purge_data=purge_data,
                remove_service=remove_service
            )
            self.send_json(200, res)
            return

        if path == "/api/update":
            tag = os.environ.get("DASHBOARD_AGENT_TAG", "main")
            try:
                body_json = self.read_json()
                if isinstance(body_json, dict) and body_json.get("tag"):
                    tag = str(body_json["tag"]).strip()
            except Exception:
                pass
            repo_url = os.environ.get(
                "DASHBOARD_AGENT_REPO_URL",
                "https://raw.githubusercontent.com/calaquin/dashboard-agent/%s" % tag
            )
            cache_buster = int(time.time())
            update_url = "%s/agent.py?t=%d" % (repo_url, cache_buster)

            try:
                req = urllib.request.Request(
                    update_url,
                    headers={"User-Agent": "dashboard-agent/%s" % AGENT_VERSION}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    status_code = getattr(resp, "status", getattr(resp, "code", 200))
                    if status_code != 200:
                        self.send_json(502, {"error": "Failed to download update (HTTP %d)" % status_code})
                        return
                    code_bytes = resp.read()

                if not code_bytes or len(code_bytes) < 50:
                    self.send_json(502, {"error": "Downloaded update is empty or invalid"})
                    return

                new_version = AGENT_VERSION
                m_ver = re.search(r'AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', code_bytes.decode("utf-8", errors="replace"))
                if m_ver:
                    new_version = m_ver.group(1)

                target_file = Path(__file__).resolve()
                lib_file = Path("/usr/local/lib/dashboard-agent/agent.py")
                if lib_file.exists() or lib_file.parent.exists():
                    target_file = lib_file

                target_file.parent.mkdir(parents=True, exist_ok=True)
                tmp_new = target_file.parent / (".agent.py.new.%d" % os.getpid())
                tmp_new.write_bytes(code_bytes)
                try:
                    os.chmod(tmp_new, 0o755)
                    py_compile.compile(str(tmp_new), doraise=True)
                except Exception as comp_err:
                    try:
                        tmp_new.unlink()
                    except OSError:
                        pass
                    self.send_json(400, {"error": "Update verification failed: %s" % comp_err})
                    return

                os.replace(tmp_new, target_file)

                self.send_json(200, {
                    "ok": True,
                    "status": "updated",
                    "from_version": AGENT_VERSION,
                    "to_version": new_version
                })

                def restart_agent():
                    time.sleep(0.5)
                    os._exit(0)

                threading.Thread(target=restart_agent, daemon=True).start()
                return

            except Exception as exc:
                self.send_json(500, {"error": "Update failed: %s" % exc})
                return

        self.send_json(404, {"error": "Not found"})


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bind",
        default=os.environ.get(
            "DASHBOARD_AGENT_BIND",
            "0.0.0.0"
        )
    )
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--token", default=None)
    args = parser.parse_args(argv)

    data_dir = args.data_dir or os.environ.get("DASHBOARD_AGENT_DATA_DIR")
    if data_dir:
        AgentHandler.data_dir = Path(data_dir)

    token = args.token or os.environ.get(
        "DASHBOARD_AGENT_TOKEN",
        ""
    )

    if token:
        AgentHandler.agent_token = token
    else:
        active_token = AgentHandler.get_active_token()
        enrollment = AgentHandler.get_enrollment()
        if not active_token and not enrollment:
            parser.error("DASHBOARD_AGENT_TOKEN or %s/credentials.json/enrollment.json is required" % AgentHandler.data_dir)

    port = args.port if args.port is not None else parse_port(os.environ.get("DASHBOARD_AGENT_PORT"), PORT)

    server = http.server.ThreadingHTTPServer(
        (args.bind, port),
        AgentHandler
    )

    print(
        "Dashboard agent listening on %s:%d"
        % (args.bind, port),
        flush=True
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
