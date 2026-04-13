#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

VOICE="${1:-en_US-lessac-medium}"
TARGET_ROOT="${2:-${REPO_ROOT}/dev/models/tts}"

mkdir -p "${TARGET_ROOT}"

BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main"
ONNX_FILE="${VOICE}.onnx"
JSON_FILE="${VOICE}.onnx.json"

echo "Downloading Piper voice ${VOICE} into ${TARGET_ROOT}"

curl -fsSL "${BASE_URL}/${VOICE}/${ONNX_FILE}" -o "${TARGET_ROOT}/${ONNX_FILE}"
curl -fsSL "${BASE_URL}/${VOICE}/${JSON_FILE}" -o "${TARGET_ROOT}/${JSON_FILE}"

echo "Downloaded ${ONNX_FILE} and ${JSON_FILE}"
