# Dashboard Agent

A small, dependency-free monitoring agent for Debian Docker hosts. It reports
local system, storage, network, Docker, and optional SnapRAID status to the
central [kindle-dashboard](https://github.com/calaquin/kindle-dashboard).

The agent exposes JSON only, requires a bearer token, and stores no data.

The status response follows the versioned contract documented in the central
dashboard's `STATUS_SCHEMA.md`. Agent `0.2.0` advertises status schema version
`1` and a capability list so newer dashboards can detect compatibility before
using optional fields.

## Requirements

- Debian with Python 3
- `iproute2` and `iputils-ping`
- Docker CLI access for container status
- TCP port 8100 reachable only from the central dashboard host

## Install

Run these commands on the monitored machine:

```bash
sudo apt update
sudo apt install -y python3 curl iproute2 iputils-ping

sudo useradd --system --user-group --home /nonexistent --shell /usr/sbin/nologin \
  dashboard-agent
sudo usermod -aG docker dashboard-agent

sudo install -d -m 0755 /usr/local/lib/dashboard-agent
curl -fsSL \
  https://raw.githubusercontent.com/calaquin/dashboard-agent/main/agent.py \
  -o /tmp/dashboard-agent.py
sudo install -m 0755 \
  /tmp/dashboard-agent.py \
  /usr/local/lib/dashboard-agent/agent.py

curl -fsSL \
  https://raw.githubusercontent.com/calaquin/dashboard-agent/main/dashboard-agent.service \
  -o /tmp/dashboard-agent.service
sudo install -m 0644 \
  /tmp/dashboard-agent.service \
  /etc/systemd/system/dashboard-agent.service

sudo install -m 0600 /dev/null /etc/dashboard-agent
sudoedit /etc/dashboard-agent
```

Set this machine's unique token in `/etc/dashboard-agent`:

```dotenv
DASHBOARD_AGENT_TOKEN=replace-with-this-machine-token
```

Start the agent:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dashboard-agent
```

If the `dashboard-agent` user already exists, skip the `useradd` command.

### Replacing the old bundled agent

If this host already runs `kindle-dashboard-agent`, stop and disable it before
starting this service because both use port 8100:

```bash
sudo systemctl disable --now kindle-dashboard-agent
```

You may reuse its token value, but put it in `/etc/dashboard-agent` under the
new `DASHBOARD_AGENT_TOKEN` variable name.

## Verify

Check service health locally:

```bash
curl http://127.0.0.1:8100/health
```

Read status using the configured token:

```bash
curl \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  http://127.0.0.1:8100/api/status
```

An unauthenticated status request should return `401 Unauthorized`.

Confirm that the service account can inspect Docker:

```bash
sudo -u dashboard-agent docker ps
```

Logs are available through systemd:

```bash
sudo journalctl -u dashboard-agent -n 50 --no-pager
```

## Configuration

- `DASHBOARD_AGENT_TOKEN` is required.
- `DASHBOARD_AGENT_BIND` defaults to `0.0.0.0`.
- `--port` defaults to `8100`.
- `--bind` overrides the bind address.

LAN agents should permit port 8100 only from Pi-PingTheLan. VPS agents should
be reachable only through the intended Twingate route and must not expose port
8100 to the public internet.

SnapRAID status requires passwordless permission for the exact
`sudo snapraid status` command. Without it, the rest of the agent continues to
work and SnapRAID is reported as unavailable or warning.

## Update

```bash
curl -fsSL \
  https://raw.githubusercontent.com/calaquin/dashboard-agent/main/agent.py \
  -o /tmp/dashboard-agent.py
sudo install -m 0755 \
  /tmp/dashboard-agent.py \
  /usr/local/lib/dashboard-agent/agent.py
sudo systemctl restart dashboard-agent
```
