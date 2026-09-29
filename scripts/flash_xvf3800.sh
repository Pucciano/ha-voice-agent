#!/usr/bin/env bash
set -euo pipefail

# One-time switch of a reSpeaker XVF3800 from the factory USB firmware to the
# formatBCE I2S firmware, over USB DFU. Connect only the XMOS USB-C port (next
# to the 3.5 mm jack), not the XIAO.
#
# The image is written to the DFU upgrade slot (alt 1). The factory slot
# (alt 0) stays untouched, so the board can still fall back to it.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

USB_ID="2886:001a"
IMAGE_NAME="application_xvf3800_inthost-lr48-sqr-i2c-v1.0.7-release.bin"
IMAGE_MD5="043a848f544ff2c7265ac19685daf5de"

# Same formatBCE commit as the ESPHome components, so firmware and driver match.
REF="$(sed -nE 's/^[[:space:]]*xvf3800_ref:[[:space:]]*([0-9a-f]{40}).*/\1/p' \
  "${REPO_ROOT}/esphome/packages/aivi-base.yaml")"
if [[ -z "${REF}" ]]; then
  echo "xvf3800_ref not found in esphome/packages/aivi-base.yaml" >&2
  exit 1
fi
URL="https://github.com/formatBCE/Respeaker-XVF3800-ESPHome-integration/raw/${REF}/${IMAGE_NAME}"

if ! command -v dfu-util >/dev/null; then
  echo "dfu-util not found (macOS: brew install dfu-util)" >&2
  exit 1
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
IMAGE="${WORK_DIR}/${IMAGE_NAME}"

echo "Downloading ${IMAGE_NAME} (formatBCE ${REF:0:7})"
curl -fsSL "${URL}" -o "${IMAGE}"
ACTUAL_MD5="$(md5 -q "${IMAGE}" 2>/dev/null || md5sum "${IMAGE}" | cut -d' ' -f1)"
if [[ "${ACTUAL_MD5}" != "${IMAGE_MD5}" ]]; then
  echo "MD5 mismatch: expected ${IMAGE_MD5}, got ${ACTUAL_MD5}" >&2
  exit 1
fi

FOUND="$(dfu-util -l 2>/dev/null | grep -c "\[${USB_ID}\].*alt=1" || true)"
if [[ "${FOUND}" -ne 1 ]]; then
  echo "Expected one XVF3800 on USB (${USB_ID}), found ${FOUND}." >&2
  echo "Connect only the XMOS USB-C port. A board that already runs the I2S" >&2
  echo "firmware does not show up on USB and needs no update." >&2
  exit 1
fi

echo "Writing the upgrade slot (alt 1)"
dfu-util -d "${USB_ID}" -R -e -a 1 -D "${IMAGE}"

# The I2S firmware has no USB interface. If the board comes back on USB, it
# rejected the image and booted the factory firmware.
sleep 6
if dfu-util -l 2>/dev/null | grep -q "\[${USB_ID}\]"; then
  echo "The XVF3800 is back on USB: it booted the factory firmware." >&2
  exit 1
fi
echo "Done: the XVF3800 left USB mode and runs the I2S firmware."
