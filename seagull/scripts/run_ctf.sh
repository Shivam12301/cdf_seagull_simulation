#!/usr/bin/env bash
# ==============================================================================
# Run Seagull CTF (Charging Trigger Function) Client
# 3GPP Diameter Rf Offline Charging Simulation
# Usage: ./run_ctf.sh [session|event]
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

MODE="${1:-session}"
mkdir -p "${BASE_DIR}/logs"

if [ "$MODE" == "event" ]; then
    SCENARIO="${BASE_DIR}/scenario/ctf_event_client.xml"
    echo "[CTF CLIENT] Starting Event-Based Charging Simulation (ECUR)..."
else
    SCENARIO="${BASE_DIR}/scenario/ctf_session_client.xml"
    echo "[CTF CLIENT] Starting Session-Based Charging Simulation (SCUR)..."
fi

seagull -conf "${BASE_DIR}/config/conf_ctf_client.xml" \
        -dico "${BASE_DIR}/config/base_rf.xml" \
        -scen "${SCENARIO}" \
        -log "${BASE_DIR}/logs/ctf_client.log" \
        -llevel E
