#!/bin/bash
set -euo pipefail

# Write the Jetson Nano SD card image to a microSD card on macOS and verify
# it by reading the card back.
#   sudo scripts/flash_jetson_sd.sh <image.zip> <disk>   (e.g. disk6)
# The zip is NVIDIA's jetson-nano-jp461-sd-card-image.zip. The script only
# accepts an external, removable disk and asks for confirmation first.

ZIP="${1:?usage: sudo $0 <image.zip> <disk>}"
DISK="${2:?usage: sudo $0 <image.zip> <disk>}"
IMG="sd-blob-b01.img"
PY="$(command -v python3)"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi
if [[ ! "${DISK}" =~ ^disk[0-9]+$ ]]; then
  echo "Give the whole disk, e.g. disk6, not a partition." >&2
  exit 1
fi

INFO="$(diskutil info "${DISK}")"
for required in "Removable Media: +Removable" "Device Location: +External"; do
  if ! grep -Eq "${required}" <<<"${INFO}"; then
    echo "/dev/${DISK} is not an external removable disk." >&2
    exit 1
  fi
done

echo "Checking the image (CRC and SHA-256) ..."
read -r IMG_BYTES IMG_SHA < <("${PY}" - "${ZIP}" "${IMG}" <<'EOF'
import hashlib, sys, zipfile
archive, member = sys.argv[1], sys.argv[2]
digest = hashlib.sha256()
with zipfile.ZipFile(archive) as zf, zf.open(member) as stream:  # CRC checked at EOF
    size = zf.getinfo(member).file_size
    while chunk := stream.read(8 * 1024 * 1024):
        digest.update(chunk)
print(size, digest.hexdigest())
EOF
)

diskutil list "${DISK}"
read -r -p "Erase /dev/${DISK} and write ${IMG}? Type the disk name to confirm: " answer
if [[ "${answer}" != "${DISK}" ]]; then
  echo "Aborted." >&2
  exit 1
fi

diskutil unmountDisk "/dev/${DISK}"
echo "Writing ${IMG} (${IMG_BYTES} bytes) ..."
/usr/bin/unzip -p "${ZIP}" "${IMG}" | /bin/dd of="/dev/r${DISK}" bs=1m status=progress
sync

echo "Reading the card back ..."
CARD_SHA="$("${PY}" - "/dev/r${DISK}" "${IMG_BYTES}" <<'EOF'
import hashlib, sys
path, left = sys.argv[1], int(sys.argv[2])
digest = hashlib.sha256()
with open(path, "rb", buffering=0) as device:
    while left > 0:
        # Raw devices read whole sectors only; cut the surplus of the last read.
        block = device.read(4 * 1024 * 1024 if left >= 4 * 1024 * 1024 else (left + 511) // 512 * 512)
        if not block:
            raise SystemExit("short read")
        digest.update(block[:left])
        left -= min(left, len(block))
print(digest.hexdigest())
EOF
)"
if [[ "${CARD_SHA}" != "${IMG_SHA}" ]]; then
  echo "Verification failed: card ${CARD_SHA}, image ${IMG_SHA}" >&2
  exit 1
fi
echo "Verified: SHA-256 ${CARD_SHA}"
diskutil eject "/dev/${DISK}"
