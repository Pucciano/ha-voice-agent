#!/usr/bin/env bash
set -euo pipefail

VOICE="${1:-en_US-lessac-medium}"
TARGET_ROOT="${2:-/home/forensicshark/Documents/ha-voice-agent/dev/models/tts}"

mkdir -p "${TARGET_ROOT}"

BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main"
ONNX_FILE="${VOICE}.onnx"
JSON_FILE="${VOICE}.onnx.json"

echo "Downloading Piper voice ${VOICE} into ${TARGET_ROOT}"

curl -fsSL "${BASE_URL}/${VOICE}/${ONNX_FILE}" -o "${TARGET_ROOT}/${ONNX_FILE}"
curl -fsSL "${BASE_URL}/${VOICE}/${JSON_FILE}" -o "${TARGET_ROOT}/${JSON_FILE}"

echo "Downloaded ${ONNX_FILE} and ${JSON_FILE}"
