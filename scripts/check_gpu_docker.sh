#!/usr/bin/env bash
set -euo pipefail

IMAGE="nvidia/cuda:12.4.1-base-ubuntu22.04"

echo "[check_gpu_docker] Verifying NVIDIA Container Toolkit with: ${IMAGE}"
docker run --rm --gpus all "${IMAGE}" nvidia-smi

echo "[check_gpu_docker] PASS"
