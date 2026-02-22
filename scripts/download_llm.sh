#!/usr/bin/env bash
set -euo pipefail

MODEL_ID="${1:-Qwen/Qwen3-8B}"
TARGET_ROOT="${2:-/home/forensicshark/Documents/ha-voice-agent/dev/models/llm}"

mkdir -p "${TARGET_ROOT}"

python3 - <<'PY' "${MODEL_ID}" "${TARGET_ROOT}"
import pathlib
import sys

model_id = sys.argv[1]
target_root = pathlib.Path(sys.argv[2])
target = target_root / model_id.split("/")[-1]
target.mkdir(parents=True, exist_ok=True)

try:
    import huggingface_hub
except ImportError as exc:
    raise SystemExit(
        "Missing huggingface_hub. Install with: pip install huggingface_hub"
    ) from exc

huggingface_hub.snapshot_download(
    repo_id=model_id,
    local_dir=str(target),
    local_dir_use_symlinks=False,
    resume_download=True,
)
print(f"Downloaded {model_id} to {target}")
PY
