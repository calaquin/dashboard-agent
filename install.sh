#!/usr/bin/env bash
set -euo pipefail

# Kindle Dashboard Agent Installer
# Pinned to tagged releases or local repository files

ENROLLMENT_ID=""
BOOTSTRAP_TOKEN=""
EXPIRES_AT=""
TOKEN=""
PORT="8100"
INSTANCE=""
BIND="0.0.0.0"
ENABLE_DOCKER=0
ENABLE_POWER=0
ENABLE_SNAPRAID=0
UNINSTALL=0
REENROLL=0
REMOVE_OLD=0
FORCE=0
TAG="${DASHBOARD_AGENT_TAG:-v0.7.1}"
TAG="${DASHBOARD_AGENT_TAG:-v0.7.2}"
REPO_RAW_URL="https://raw.githubusercontent.com/calaquin/dashboard-agent/${TAG}"

prompt_yn() {
    local prompt_text="$1"
    local default_ans="${2:-n}"
    local response=""

    if [ -t 0 ]; then
        read -r -p "$prompt_text " response
    elif [ -c /dev/tty ] && [ -r /dev/tty ]; then
        read -r -p "$prompt_text " response < /dev/tty || response="$default_ans"
    else
        response="$default_ans"
    fi

    case "$response" in
        [yY][eE][sS]|[yY])
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

usage() {
    cat <<EOF
Usage: install.sh [OPTIONS]

Options:
  --enrollment-id <UUID>         Enrollment session ID from Kindle Dashboard
  --bootstrap-token <TOKEN>      Short-lived bootstrap token from Kindle Dashboard
  --enrollment-expires-at <TIME> Unix timestamp when bootstrap credential expires
  --token <PERMANENT_TOKEN>      Direct permanent token (legacy / non-enrollment mode)
  --port <PORT>                  Agent HTTP port (default: 8100)
  --instance <NAME>              Agent instance name (default: port or 'default')
  --bind <ADDRESS>               Agent bind address (default: 0.0.0.0)
  --enable-docker                Grant dashboard-agent access to Docker daemon
  --enable-power                 Grant dashboard-agent permission to reboot/shutdown host
  --enable-snapraid              Grant dashboard-agent permission to run 'snapraid status'
  --uninstall                    Stop, disable, and remove agent instance
  --remove-old                   Automatically remove old/superseded agent instances
  --reenroll                     Re-enroll existing instance with new credentials
  --force                        Non-interactive execution, accept defaults
  --tag <TAG>                    Specify release tag to install (e.g. v0.3.8)
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
        --instance)
            INSTANCE="$2"
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
        --enable-power)
            ENABLE_POWER=1
            shift
            ;;
        --enable-snapraid)
            ENABLE_SNAPRAID=1
            shift
            ;;
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        --remove-old)
            REMOVE_OLD=1
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

if [[ $UNINSTALL -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
        echo "Error: install.sh --uninstall must be run as root (e.g. using sudo)." >&2
        exit 1
    fi
    if [[ -z "$INSTANCE" ]]; then
        if [[ "$PORT" != "8100" ]]; then
            INSTANCE="$PORT"
        else
            INSTANCE="default"
        fi
    fi
    if [[ "$INSTANCE" == "default" ]]; then
        DATA_DIR="/var/lib/dashboard-agent"
        SERVICE_NAME="dashboard-agent"
    else
        DATA_DIR="/var/lib/dashboard-agent-${INSTANCE}"
        SERVICE_NAME="dashboard-agent@${INSTANCE}"
    fi

    echo "Stopping and disabling ${SERVICE_NAME}..."
    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
        systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    fi
    if [[ -d "$DATA_DIR" ]]; then
        echo "Removing data directory $DATA_DIR..."
        rm -rf "$DATA_DIR"
    fi
    echo "✓ Successfully uninstalled ${SERVICE_NAME}."
    exit 0
fi

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

if [[ -z "$INSTANCE" ]]; then
    if [[ "$PORT" != "8100" ]]; then
        INSTANCE="$PORT"
    else
        INSTANCE="default"
    fi
fi

if [[ "$INSTANCE" == "default" ]]; then
    DATA_DIR="/var/lib/dashboard-agent"
    SERVICE_NAME="dashboard-agent"
else
    DATA_DIR="/var/lib/dashboard-agent-${INSTANCE}"
    SERVICE_NAME="dashboard-agent@${INSTANCE}"
fi

LIB_DIR="/usr/local/lib/dashboard-agent"
SERVICE_FILE="/etc/systemd/system/dashboard-agent.service"
SERVICE_FILE_TEMPLATE="/etc/systemd/system/dashboard-agent@.service"

# Check for existing installation or legacy configuration for THIS instance
EXISTING_CONFIG=""
if [[ "$INSTANCE" == "default" && (-e /etc/dashboard-agent || -L /etc/dashboard-agent) ]]; then
    EXISTING_CONFIG="/etc/dashboard-agent"
elif [[ -f "$DATA_DIR/credentials.json" ]]; then
    EXISTING_CONFIG="$DATA_DIR/credentials.json"
fi

if [[ -n "$EXISTING_CONFIG" && $FORCE -eq 0 && $REENROLL -eq 0 ]]; then
    echo "Notice: An existing dashboard-agent configuration was detected for instance '$INSTANCE' ($EXISTING_CONFIG)."
    if prompt_yn "Do you want to replace the existing configuration and deploy the new key? [Y/n]" "y"; then
        echo "Proceeding with replacement..."
    else
        echo "Installation cancelled by user."
        exit 0
    fi
fi

# Clean up / backup existing legacy /etc/dashboard-agent if configuring default instance
if [[ "$INSTANCE" == "default" && (-e /etc/dashboard-agent || -L /etc/dashboard-agent) ]]; then
    BACKUP_PATH="/etc/dashboard-agent.bak.$(date +%s)"
    echo "Backing up existing /etc/dashboard-agent to $BACKUP_PATH..."
    mv -f /etc/dashboard-agent "$BACKUP_PATH" 2>/dev/null || rm -rf /etc/dashboard-agent
fi

# Check for other old / existing agent instances on this host
OTHER_INSTANCES=()

# 1. Check default single-instance service if we are installing a multi-instance service
if [[ "$SERVICE_NAME" != "dashboard-agent" ]]; then
    if (command -v systemctl >/dev/null 2>&1 && (systemctl is-active --quiet dashboard-agent 2>/dev/null || systemctl is-enabled --quiet dashboard-agent 2>/dev/null)) || [[ -d "/var/lib/dashboard-agent" || -e "/etc/dashboard-agent" ]]; then
        OTHER_INSTANCES+=("dashboard-agent")
    fi
fi

# 2. Check other template services currently loaded or active
if command -v systemctl >/dev/null 2>&1; then
    while IFS= read -r unit; do
        [[ -z "$unit" ]] && continue
        local_inst="${unit#dashboard-agent@}"
        local_inst="${local_inst%.service}"
        if [[ -n "$local_inst" && "$local_inst" != "$INSTANCE" ]]; then
            OTHER_INSTANCES+=("dashboard-agent@${local_inst}")
        fi
    done < <(systemctl list-units --type=service --state=active,loaded "dashboard-agent@*.service" --no-legend 2>/dev/null | awk '{print $1}' || true)
fi

# 3. Check other data directories in /var/lib
for dir in /var/lib/dashboard-agent-*; do
    if [[ -d "$dir" && "$dir" != *.bak* ]]; then
        dir_inst="${dir#/var/lib/dashboard-agent-}"
        if [[ -n "$dir_inst" && "$dir_inst" != "$INSTANCE" && "$dir_inst" != "*" ]]; then
            OTHER_INSTANCES+=("dashboard-agent@${dir_inst}")
        fi
    fi
done

# Deduplicate old instances list
OLD_INSTANCES=()
if [[ ${#OTHER_INSTANCES[@]} -gt 0 ]]; then
    while IFS= read -r item; do
        [[ -n "$item" ]] && OLD_INSTANCES+=("$item")
    done < <(printf "%s\n" "${OTHER_INSTANCES[@]}" | sort -u)
fi

# Offer to stop, disable, and remove old instances
for old_svc in "${OLD_INSTANCES[@]}"; do
    old_status="inactive"
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$old_svc" 2>/dev/null; then
        old_status="active"
    elif command -v systemctl >/dev/null 2>&1 && systemctl is-enabled --quiet "$old_svc" 2>/dev/null; then
        old_status="enabled"
    fi

    if [[ "$old_svc" == "dashboard-agent" ]]; then
        old_data_dir="/var/lib/dashboard-agent"
    else
        old_inst="${old_svc#dashboard-agent@}"
        old_inst="${old_inst%.service}"
        old_data_dir="/var/lib/dashboard-agent-${old_inst}"
    fi

    echo ""
    echo "Notice: Detected an existing old agent instance '$old_svc' (Status: $old_status, Data: $old_data_dir)."
    if [[ $REMOVE_OLD -eq 1 ]] || prompt_yn "Would you like to stop, disable, and remove this old instance? [y/N]" "n"; then
        echo "Stopping and disabling $old_svc..."
        if command -v systemctl >/dev/null 2>&1; then
            systemctl stop "$old_svc" 2>/dev/null || true
            systemctl disable "$old_svc" 2>/dev/null || true
        fi
        if [[ -d "$old_data_dir" ]]; then
            backup_dir="${old_data_dir}.bak.$(date +%s)"
            echo "Backing up and removing $old_data_dir -> $backup_dir..."
            mv -f "$old_data_dir" "$backup_dir" 2>/dev/null || rm -rf "$old_data_dir"
        fi
        if [[ "$old_svc" == "dashboard-agent" && (-e /etc/dashboard-agent || -L /etc/dashboard-agent) ]]; then
            rm -rf /etc/dashboard-agent 2>/dev/null || true
        fi
        echo "✓ Successfully removed old instance '$old_svc'."
    else
        echo "Keeping old instance '$old_svc'."
    fi
done

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
SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"
fi
CACHE_BUSTER=$(date +%s)

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/agent.py" ]]; then
    echo "Using local agent.py..."
    install -m 0755 "$SCRIPT_DIR/agent.py" "$LIB_DIR/agent.py"
else
    echo "Downloading agent.py from ${REPO_RAW_URL}/agent.py..."
    curl -fsSL "${REPO_RAW_URL}/agent.py?t=${CACHE_BUSTER}" -o /tmp/dashboard-agent.py
    install -m 0755 /tmp/dashboard-agent.py "$LIB_DIR/agent.py"
    rm -f /tmp/dashboard-agent.py
    AGENT_TMP=$(mktemp)
    curl -fsSL "${REPO_RAW_URL}/agent.py?t=${CACHE_BUSTER}" -o "$AGENT_TMP"
    install -m 0755 "$AGENT_TMP" "$LIB_DIR/agent.py"
    rm -f "$AGENT_TMP"
fi

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/dashboard-agent.service" ]]; then
    echo "Using local dashboard-agent.service..."
    install -m 0644 "$SCRIPT_DIR/dashboard-agent.service" "$SERVICE_FILE"
else
    echo "Downloading dashboard-agent.service from ${REPO_RAW_URL}/dashboard-agent.service..."
    curl -fsSL "${REPO_RAW_URL}/dashboard-agent.service?t=${CACHE_BUSTER}" -o /tmp/dashboard-agent.service
    install -m 0644 /tmp/dashboard-agent.service "$SERVICE_FILE"
    rm -f /tmp/dashboard-agent.service
    SVC_TMP=$(mktemp)
    curl -fsSL "${REPO_RAW_URL}/dashboard-agent.service?t=${CACHE_BUSTER}" -o "$SVC_TMP"
    install -m 0644 "$SVC_TMP" "$SERVICE_FILE"
    rm -f "$SVC_TMP"
fi

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/dashboard-agent@.service" ]]; then
    echo "Using local dashboard-agent@.service..."
    install -m 0644 "$SCRIPT_DIR/dashboard-agent@.service" "$SERVICE_FILE_TEMPLATE"
else
    echo "Downloading dashboard-agent@.service from ${REPO_RAW_URL}/dashboard-agent@.service..."
    curl -fsSL "${REPO_RAW_URL}/dashboard-agent@.service?t=${CACHE_BUSTER}" -o /tmp/dashboard-agent@.service
    install -m 0644 /tmp/dashboard-agent@.service "$SERVICE_FILE_TEMPLATE"
    rm -f /tmp/dashboard-agent@.service
    SVCT_TMP=$(mktemp)
    curl -fsSL "${REPO_RAW_URL}/dashboard-agent@.service?t=${CACHE_BUSTER}" -o "$SVCT_TMP"
    install -m 0644 "$SVCT_TMP" "$SERVICE_FILE_TEMPLATE"
    rm -f "$SVCT_TMP"
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

# Handle service management and host power permissions
echo "Configuring service permissions..."
if [[ -d /etc/sudoers.d ]]; then
    if [[ $ENABLE_POWER -eq 1 ]]; then
        cat <<EOF > /etc/sudoers.d/dashboard-agent-power
dashboard-agent ALL=(ALL) NOPASSWD: /bin/systemctl reboot, /bin/systemctl poweroff, /bin/systemctl stop dashboard-agent*, /bin/systemctl disable dashboard-agent*, /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff, /usr/bin/systemctl stop dashboard-agent*, /usr/bin/systemctl disable dashboard-agent*, /sbin/reboot, /sbin/shutdown
EOF
    else
        cat <<EOF > /etc/sudoers.d/dashboard-agent-power
dashboard-agent ALL=(ALL) NOPASSWD: /bin/systemctl stop dashboard-agent*, /bin/systemctl disable dashboard-agent*, /usr/bin/systemctl stop dashboard-agent*, /usr/bin/systemctl disable dashboard-agent*
EOF
    fi
    chmod 0440 /etc/sudoers.d/dashboard-agent-power 2>/dev/null || true

    if [[ $ENABLE_SNAPRAID -eq 1 ]]; then
        echo "Configuring SnapRAID sudoers permissions..."
        cat <<EOF > /etc/sudoers.d/dashboard-agent-snapraid
dashboard-agent ALL=(ALL) NOPASSWD: /usr/bin/snapraid, /usr/bin/snapraid *, /usr/local/bin/snapraid, /usr/local/bin/snapraid *, /bin/snapraid, /bin/snapraid *, /usr/sbin/snapraid, /usr/sbin/snapraid *, /snap/bin/snapraid, /snap/bin/snapraid *
EOF
        chmod 0440 /etc/sudoers.d/dashboard-agent-snapraid 2>/dev/null || true
    fi
fi

# Write instance configuration environment file
ENV_TMP=$(mktemp)
cat <<EOF > "$ENV_TMP"
DASHBOARD_AGENT_PORT=${PORT}
DASHBOARD_AGENT_BIND=${BIND}
DASHBOARD_AGENT_DATA_DIR=${DATA_DIR}
DASHBOARD_AGENT_ALLOW_POWER=${ENABLE_POWER}
DASHBOARD_AGENT_ENABLE_SNAPRAID=${ENABLE_SNAPRAID}
EOF
install -m 0600 -o dashboard-agent -g dashboard-agent "$ENV_TMP" "$DATA_DIR/agent.env"
rm -f "$ENV_TMP"

# Start or restart systemd service
if command -v systemctl >/dev/null 2>&1; then
    echo "Starting ${SERVICE_NAME} service..."
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true
    systemctl restart "${SERVICE_NAME}"
    sleep 1
fi

# Self-health check
if command -v curl >/dev/null 2>&1; then
    if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        echo "Agent health check passed on port ${PORT}."
    else
        echo "Warning: Agent did not respond on http://127.0.0.1:${PORT}/health. Check journalctl -u ${SERVICE_NAME}."
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
  Instance:          $INSTANCE
  Port:              $PORT
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
            if ! systemctl is-active --quiet "${SERVICE_NAME}"; then
                echo ""
                echo "✗ Error: ${SERVICE_NAME} service stopped unexpectedly. Check 'journalctl -u ${SERVICE_NAME}'." >&2
                exit 1
            fi
        fi

        sleep 1
    done
else
    echo "✓ Dashboard Agent successfully installed and active."
fi

