#!/usr/bin/env bash
# ==============================================================================
# scripts/start_container.sh
# Dual-Process Container Entrypoint: Web Dashboard + Trading Bot Supervisor
# ==============================================================================
set -e

export PYTHONUNBUFFERED=1
PORT="${PORT:-10000}"

echo "================================================================================"
echo "[CONTAINER_BOOT] Initializing 24/7 Continuous Trading Bot Environment"
echo "[CONTAINER_BOOT] Timestamp: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
echo "[CONTAINER_BOOT] Web Dashboard HTTP Port: ${PORT}"
echo "================================================================================"

# Initialize directories
mkdir -p state logs historical_data sync_staging

# Launch Dashboard in background
echo "[CONTAINER_BOOT] Launching Web Dashboard on port ${PORT}..."
python dashboard.py --port "${PORT}" &
DASHBOARD_PID=$!
echo "[CONTAINER_BOOT] Web Dashboard running (PID: ${DASHBOARD_PID})"

# Trap signals for graceful shutdown
cleanup() {
    echo "[CONTAINER_SHUTDOWN] Signal caught. Initiating graceful shutdown..."
    kill -TERM "${DASHBOARD_PID}" 2>/dev/null || true
    if [ -n "${SUPERVISOR_PID}" ]; then
        kill -TERM "${SUPERVISOR_PID}" 2>/dev/null || true
    fi
    wait
    echo "[CONTAINER_SHUTDOWN] All child processes stopped."
    exit 0
}
trap cleanup SIGINT SIGTERM

# Launch Trading Bot Supervisor
echo "[CONTAINER_BOOT] Launching Trading Bot Process Supervisor..."
python scripts/rotator.py --role active --action run &
SUPERVISOR_PID=$!
echo "[CONTAINER_BOOT] Supervisor running (PID: ${SUPERVISOR_PID})"

# Wait for supervisor process
wait "${SUPERVISOR_PID}"
