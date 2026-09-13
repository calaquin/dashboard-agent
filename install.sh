#!/usr/bin/env bash
set -euo pipefail

# Kindle Dashboard Agent Installer
# Pinned to tagged releases or local repository files

ENROLLMENT_ID=""
BOOTSTRAP_TOKEN=""
EXPIRES_AT=""
TOKEN=""
PORT="8100"
BIND="0.0.0.0"
ENABLE_DOCKER=0
REENROLL=0
FORCE=0
TAG="${DASHBOARD_AGENT_TAG:-main}"
REPO_RAW_URL="https://raw.githubusercontent.com/calaquin/dashboard-agent/${TAG}"

usage() {
    cat <<EOF
Usage: install.sh [OPTIONS]

Options:
  --enrollment-id <UUID>         Enrollment session ID from Kindle Dashboard
  --bootstrap-token <TOKEN>      Short-lived bootstrap token from Kindle Dashboard
  --enrollment-expires-at <TIME> Unix timestamp when bootstrap credential expires
  --token <PERMANENT_TOKEN>      Direct permanent token (legacy / non-enrollment mode)
  --port <PORT>                  Agent HTTP port (default: 8100)
  --bind <ADDRESS>               Agent bind address (default: 0.0.0.0)
  --enable-docker                Grant dashboard-agent access to Docker daemon
  --reenroll                     Replace existing enrollment/credentials
  --force                        Force reinstallation
  --tag <TAG>                    Git tag or branch for asset download (default: main)
  -h, --help                     Show this help message
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --enrollment-id)
            ENROLLMENT_ID="$2"
            shift 2
            ;;
        --bootstrap-token)
            BOOTSTRAP_TOKEN="$2"
            shift 2
            ;;
        --enrollment-expires-at)
            EXPIRES_AT="$2"
            shift 2
            ;;
        --token)
            TOKEN="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --bind)
            BIND="$2"
            shift 2
            ;;
        --enable-docker)
            ENABLE_DOCKER=1
            shift
            ;;
        --reenroll)
            REENROLL=1
            shift
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --tag)
            TAG="$2"
            REPO_RAW_URL="https://raw.githubusercontent.com/calaquin/dashboard-agent/${TAG}"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            ;;
    esac
done

if [[ -z "$ENROLLMENT_ID" && -z "$BOOTSTRAP_TOKEN" && -z "$TOKEN" ]]; then
    echo "Error: Either (--enrollment-id and --bootstrap-token) or --token is required." >&2
    usage
fi

if [[ -n "$ENROLLMENT_ID" && -z "$BOOTSTRAP_TOKEN" ]] || [[ -z "$ENROLLMENT_ID" && -n "$BOOTSTRAP_TOKEN" ]]; then
    echo "Error: Both --enrollment-id and --bootstrap-token must be provided together." >&2
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "Error: install.sh must be run as root (e.g. using sudo)." >&2
    exit 1
fi

DATA_DIR="/var/lib/dashboard-agent"
LIB_DIR="/usr/local/lib/dashboard-agent"
SERVICE_FILE="/etc/systemd/system/dashboard-agent.service"

# Check for existing installation or legacy configuration
EXISTING_CONFIG=""
if [[ -e /etc/dashboard-agent || -L /etc/dashboard-agent ]]; then
    EXISTING_CONFIG="/etc/dashboard-agent"
elif [[ -f "$DATA_DIR/credentials.json" ]]; then
    EXISTING_CONFIG="$DATA_DIR/credentials.json"
fi

if [[ -n "$EXISTING_CONFIG" && $FORCE -eq 0 && $REENROLL -eq 0 ]]; then
    echo "Notice: An existing dashboard-agent configuration was detected ($EXISTING_CONFIG)."
    if [ -t 0 ]; then
        read -r -p "Do you want to replace the existing configuration and deploy the new key? [Y/n] " response
        case "$response" in
            [nN][oO]|[nN])
                echo "Installation cancelled by user."
                exit 0
                ;;
            *)
                echo "Proceeding with replacement..."
                ;;
        esac
    elif [ -c /dev/tty ] && [ -r /dev/tty ]; then
        read -r -p "Do you want to replace the existing configuration and deploy the new key? [Y/n] " response < /dev/tty || response="y"
        case "$response" in
            [nN][oO]|[nN])
                echo "Installation cancelled by user."
                exit 0
                ;;
            *)
                echo "Proceeding with replacement..."
                ;;
        esac
    else
        echo "Non-interactive mode: proceeding with replacement."
    fi
fi

# Clean up / backup existing legacy /etc/dashboard-agent
if [[ -e /etc/dashboard-agent || -L /etc/dashboard-agent ]]; then
    BACKUP_PATH="/etc/dashboard-agent.bak.$(date +%s)"
    echo "Backing up existing /etc/dashboard-agent to $BACKUP_PATH..."
    mv -f /etc/dashboard-agent "$BACKUP_PATH" 2>/dev/null || rm -rf /etc/dashboard-agent
fi

echo "Installing Kindle Dashboard Agent..."

# Install required packages if apt is present
if command -v apt-get >/dev/null 2>&1; then
    echo "Checking dependencies..."
    apt-get update -qq || true
    apt-get install -y -qq python3 curl iproute2 iputils-ping >/dev/null 2>&1 || true
fi

# Create service user if it doesn't exist
if ! id -u dashboard-agent >/dev/null 2>&1; then
    echo "Creating system user dashboard-agent..."
    useradd --system --user-group --home /nonexistent --shell /usr/sbin/nologin dashboard-agent
fi

# Handle Docker group opt-in
if [[ $ENABLE_DOCKER -eq 1 ]]; then
    if getent group docker >/dev/null 2>&1; then
        echo "Adding dashboard-agent to docker group (Docker monitoring enabled)..."
        usermod -aG docker dashboard-agent
    else
        echo "Warning: docker group not found on this host. Docker monitoring may be unavailable."
    fi
fi

# Setup directory permissions
install -d -m 0755 -o dashboard-agent -g dashboard-agent "$LIB_DIR"
install -d -m 0700 -o dashboard-agent -g dashboard-agent "$DATA_DIR"
chown -R dashboard-agent:dashboard-agent "$LIB_DIR" 2>/dev/null || true

# Locate or download agent.py and dashboard-agent.service
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_BUSTER=$(date +%s)

if [[ -f "$SCRIPT_DIR/agent.py" ]]; then
    echo "Using local agent.py..."
    install -m 0755 "$SCRIPT_DIR/agent.py" "$LIB_DIR/agent.py"
else
    echo "Downloading agent.py from ${REPO_RAW_URL}/agent.py..."
    curl -fsSL "${REPO_RAW_URL}/agent.py?t=${CACHE_BUSTER}" -o /tmp/dashboard-agent.py
    install -m 0755 /tmp/dashboard-agent.py "$LIB_DIR/agent.py"
    rm -f /tmp/dashboard-agent.py
fi

if [[ -f "$SCRIPT_DIR/dashboard-agent.service" ]]; then
    echo "Using local dashboard-agent.service..."
    install -m 0644 "$SCRIPT_DIR/dashboard-agent.service" "$SERVICE_FILE"
else
    echo "Downloading dashboard-agent.service from ${REPO_RAW_URL}/dashboard-agent.service..."
    curl -fsSL "${REPO_RAW_URL}/dashboard-agent.service?t=${CACHE_BUSTER}" -o /tmp/dashboard-agent.service
    install -m 0644 /tmp/dashboard-agent.service "$SERVICE_FILE"
    rm -f /tmp/dashboard-agent.service
fi

# Persistent agent_id
AGENT_ID=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
import agent
print(agent.get_or_create_agent_id('$DATA_DIR'))
")
chown dashboard-agent:dashboard-agent "$DATA_DIR/agent-id" || true
chmod 0644 "$DATA_DIR/agent-id" || true

# Configure enrollment or permanent credentials
if [[ -n "$ENROLLMENT_ID" && -n "$BOOTSTRAP_TOKEN" ]]; then
    ENROLL_TMP=$(mktemp)
    cat <<EOF > "$ENROLL_TMP"
{
  "enrollment_id": "$ENROLLMENT_ID",
  "bootstrap_token": "$BOOTSTRAP_TOKEN",
  "expires_at": ${EXPIRES_AT:-$(( $(date +%s) + 1800 ))}
}
EOF
    install -m 0600 -o dashboard-agent -g dashboard-agent "$ENROLL_TMP" "$DATA_DIR/enrollment.json"
    rm -f "$ENROLL_TMP"
    rm -f "$DATA_DIR/credentials.json"
elif [[ -n "$TOKEN" ]]; then
    CREDS_TMP=$(mktemp)
    cat <<EOF > "$CREDS_TMP"
{
  "token": "$TOKEN",
  "agent_id": "$AGENT_ID",
  "created_at": $(date +%s)
}
EOF
    install -m 0600 -o dashboard-agent -g dashboard-agent "$CREDS_TMP" "$DATA_DIR/credentials.json"
    rm -f "$CREDS_TMP"
    rm -f "$DATA_DIR/enrollment.json"
fi

# Start or restart systemd service
if command -v systemctl >/dev/null 2>&1; then
    echo "Starting dashboard-agent service..."
    systemctl daemon-reload
    systemctl enable dashboard-agent >/dev/null 2>&1 || true
    systemctl restart dashboard-agent
    sleep 1
fi

# Self-health check
if command -v curl >/dev/null 2>&1; then
    if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        echo "Agent health check passed on port ${PORT}."
    else
        echo "Warning: Agent did not respond on http://127.0.0.1:${PORT}/health. Check journalctl -u dashboard-agent."
    fi
fi

if [[ -n "$ENROLLMENT_ID" && -n "$BOOTSTRAP_TOKEN" ]]; then
    VERIFY_CODE=$(python3 -c "
import sys
sys.path.insert(0, '$LIB_DIR')
import agent
print(agent.compute_verification_code('$BOOTSTRAP_TOKEN', '$ENROLLMENT_ID', '$AGENT_ID'))
")

    cat <<EOF

============================================================
  ✓ Dashboard Agent installed & awaiting verification
  
  Host:              $(hostname)
  Agent ID:          $AGENT_ID
  Verification Code: $VERIFY_CODE
  
  Enter this Verification Code in Kindle Dashboard Settings
  to complete device setup.
============================================================

EOF

    echo "Waiting for verification handshake from Kindle Dashboard (Ctrl+C to exit)..."
    EXPIRY_TIME=${EXPIRES_AT:-$(( $(date +%s) + 1800 ))}

    trap 'echo -e "\nScript exited. Dashboard Agent service remains running in background waiting for verification."; exit 0' INT

    while true; do
        if [[ -f "$DATA_DIR/credentials.json" && ! -f "$DATA_DIR/enrollment.json" ]]; then
            echo ""
            echo "✓ Verification handshake complete! Device successfully activated."
            exit 0
        fi

        CURRENT_TIME=$(date +%s)
        if [[ $CURRENT_TIME -ge $EXPIRY_TIME ]]; then
            echo ""
            echo "✗ Enrollment session expired before verification was completed." >&2
            exit 1
        fi

        if command -v systemctl >/dev/null 2>&1; then
            if ! systemctl is-active --quiet dashboard-agent; then
                echo ""
                echo "✗ Error: dashboard-agent service stopped unexpectedly. Check 'journalctl -u dashboard-agent'." >&2
                exit 1
            fi
        fi

        sleep 1
    done
else
    echo "✓ Dashboard Agent successfully installed and active."
fi

