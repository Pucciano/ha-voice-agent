#!/usr/bin/env bash
set -euo pipefail

LLM_HEALTH_URL="${1:-http://localhost:10100/health}"
PROXY_HEALTH_URL="${2:-http://localhost:18001/health}"
STT_HOST="${3:-localhost}"
STT_PORT="${4:-10300}"
TTS_HOST="${5:-localhost}"
TTS_PORT="${6:-10200}"

check_tcp() {
  local host="$1"
  local port="$2"
  timeout 3 bash -c "</dev/tcp/${host}/${port}" >/dev/null 2>&1
}

echo "Checking vLLM health: ${LLM_HEALTH_URL}"
curl -fsSL "${LLM_HEALTH_URL}" >/dev/null

echo "Checking llm_proxy health: ${PROXY_HEALTH_URL}"
curl -fsSL "${PROXY_HEALTH_URL}" >/dev/null

echo "Checking STT port ${STT_HOST}:${STT_PORT}"
check_tcp "${STT_HOST}" "${STT_PORT}"

echo "Checking TTS port ${TTS_HOST}:${TTS_PORT}"
check_tcp "${TTS_HOST}" "${TTS_PORT}"

echo "All health checks passed"
