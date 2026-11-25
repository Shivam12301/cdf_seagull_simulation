#!/usr/bin/env bash
# ==============================================================================
# Run Seagull CDF (Charging Data Function) Server
# 3GPP Diameter Rf Offline Charging Simulation
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

mkdir -p "${BASE_DIR}/logs"

echo "[CDF SERVER] Starting Seagull Diameter CDF Server on port 3868..."
seagull -conf "${BASE_DIR}/config/conf_cdf_server.xml" \
        -dico "${BASE_DIR}/config/base_rf.xml" \
        -scen "${BASE_DIR}/scenario/cdf_server.xml" \
        -log "${BASE_DIR}/logs/cdf_server.log" \
        -llevel E \
        -bg
