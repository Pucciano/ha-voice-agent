#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MODEL_ID="${1:-Systran/faster-whisper-large-v3}"
TARGET_ROOT="${2:-${REPO_ROOT}/models/stt/large-v3-ct2}"

mkdir -p "${TARGET_ROOT}"

python3 - <<'PY' "${MODEL_ID}" "${TARGET_ROOT}"
import pathlib
import sys

model_id = sys.argv[1]
target_root = pathlib.Path(sys.argv[2])
target_root.mkdir(parents=True, exist_ok=True)

try:
    import huggingface_hub
except ImportError as exc:
    raise SystemExit(
        "Missing huggingface_hub. Install with: pip install huggingface_hub"
    ) from exc

huggingface_hub.snapshot_download(
    repo_id=model_id,
    local_dir=str(target_root),
    local_dir_use_symlinks=False,
    resume_download=True,
)
print(f"Downloaded {model_id} to {target_root}")
PY
