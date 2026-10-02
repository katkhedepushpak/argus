#!/usr/bin/env bash
# Start the ARGUS watcher (detection) and the dashboard (UI) together. Ctrl+C stops both.
# Live-mode settings can be overridden from the environment or .env.
set -euo pipefail
cd "$(dirname "$0")/.."

export PYTHONPATH=.
export PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
export KUBECTL_LOGS_TARGET="${KUBECTL_LOGS_TARGET:-deployment/payment-service}"
export SPLUNK_URL="${SPLUNK_URL:-}"
PY=".venv/bin/python"

$PY -m src.agent.monitor &
WATCHER_PID=$!
trap 'kill $WATCHER_PID 2>/dev/null || true' EXIT

.venv/bin/streamlit run dashboard.py --server.headless true --server.port "${PORT:-8501}"
