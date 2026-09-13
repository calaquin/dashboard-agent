#!/bin/sh
set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo "Restarting dashboard-agent..."
sudo systemctl restart dashboard-agent
INSTANCE="${1:-default}"
PORT="${2:-8100}"

if [ -n "$INSTANCE" ] && [ "$INSTANCE" != "default" ]; then
    SERVICE="dashboard-agent@${INSTANCE}"
else
    SERVICE="dashboard-agent"
fi

echo "Restarting ${SERVICE}..."
sudo systemctl restart "${SERVICE}"

i=0
while [ $i -lt 5 ]; do
    if curl -sf http://127.0.0.1:8100/health >/dev/null 2>&1; then
        HEALTH=$(curl -sf http://127.0.0.1:8100/health)
        echo "${GREEN}✓ dashboard-agent successfully restarted & verified healthy!${NC}"
    if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        HEALTH=$(curl -sf "http://127.0.0.1:${PORT}/health")
        echo "${GREEN}✓ ${SERVICE} successfully restarted & verified healthy!${NC}"
        echo "Status: $HEALTH"
        exit 0
    fi
    sleep 1
    i=$((i + 1))
done

echo "${RED}✗ Health check failed after service restart!${NC}"
sudo journalctl -u dashboard-agent -n 25 --no-pager
sudo journalctl -u "${SERVICE}" -n 25 --no-pager
exit 1

