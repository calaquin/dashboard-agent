#!/usr/bin/env python3

import argparse
import http.server
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse

PORT = 8100

AGENT_NAME = "dashboard-agent"
AGENT_VERSION = "0.2.0"
STATUS_SCHEMA_NAME = "dashboard-agent-status"
STATUS_SCHEMA_VERSION = 1

CAPABILITIES = [
    "status.v1",
    "metrics.host",
    "metrics.network",
    "metrics.storage",
    "metrics.docker",
    "metrics.snapraid",
    "controls.docker.v1",
    "controls.power.v1",
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

    if code != 0:
        return None

    parts = output.split()

    try:
        index = parts.index("via")
        return parts[index + 1]

    except Exception:
        return None


def ping(host):
    if not host:
        return False

    code, _, _ = run_command(
        ["ping", "-c", "1", "-W", "1", host],
        timeout=2
    )

    return code == 0


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
            ["sudo", "-n", executable, "status"],
            timeout=8
        )

        combined = (output + "\n" + error).strip()
        lower = combined.lower()

        if code != 0:
            state = "warning"

        elif "no error detected" in lower:
            state = "ok"

        elif "warning" in lower or "error" in lower:
            state = "warning"

        else:
            state = "ok"

        summary = "Status available"

        for line in combined.splitlines():
            clean = line.strip()

            if not clean:
                continue

            lower_line = clean.lower()

            if (
                "no error detected" in lower_line
                or "warning" in lower_line
                or "error" in lower_line
            ):
                summary = clean
                break

        data = {
            "available": True,
            "state": state,
            "summary": summary
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
        cmd = ["/bin/systemctl", "reboot"] if shutil.which("systemctl") else ["/sbin/reboot"]
        base_cmd = ["/bin/systemctl", "reboot"] if shutil.which("systemctl") else ["/sbin/reboot"]
    elif action == "shutdown":
        cmd = ["/bin/systemctl", "poweroff"] if shutil.which("systemctl") else ["/sbin/shutdown", "-h", "now"]
        base_cmd = ["/bin/systemctl", "poweroff"] if shutil.which("systemctl") else ["/sbin/shutdown", "-h", "now"]
    else:
        return (400, {"error": "Invalid power action"})
    code, out, err = run_command(cmd, timeout=10)

    code, out, err = run_command(base_cmd, timeout=10)
    if code != 0:
        sudo_cmd = ["sudo", "-n"] + base_cmd
        code, out, err = run_command(sudo_cmd, timeout=10)
    if code != 0:
        return (500, {"error": err or "Failed to %s host" % action})
    return (200, {"ok": True, "action": action, "output": out})


class AgentHandler(JsonHandlerMixin, http.server.BaseHTTPRequestHandler):

    agent_token = ""

    def is_authorized(self):
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + self.agent_token

        return hmac.compare_digest(supplied, expected)

    def do_GET(self):
        url_parts = urllib.parse.urlsplit(self.path)
        path = url_parts.path
        query = urllib.parse.parse_qs(url_parts.query)

        if path == "/health":
            self.send_json(200, {
                "ok": True,
                "agent": {
                    "name": AGENT_NAME,
                    "version": AGENT_VERSION
                },
                "schema": {
                    "name": STATUS_SCHEMA_NAME,
                    "version": STATUS_SCHEMA_VERSION
                }
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
    args = parser.parse_args(argv)

    token = os.environ.get(
        "DASHBOARD_AGENT_TOKEN",
        ""
    )

    if not token:
        parser.error("DASHBOARD_AGENT_TOKEN is required")

    AgentHandler.agent_token = token
    port = args.port or PORT

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
