#!/usr/bin/env bash
set -euo pipefail

# USB drive for the speech-to-text request capture and the wake word
# recordings on the Jetson (docs/jetson_setup.md, sections 9 and 10).
# Installed as /usr/local/sbin/aivi-capture.
#
#   format <device>  partition and format a USB drive as ext4 (ERASES IT)
#   setup [device]   mount the drive by UUID at boot and when plugged in
#   prepare          create the empty, immutable mount point only
#   mount            mount the drive again after an eject, and create
#                    missing data directories
#   eject            unmount, so the drive can be unplugged
#   status           show mount, marker, free space, samples and takes
#
# Layout: /mnt/aivi-capture is a plain directory on the SD card and is bind
# mounted into the stt-capture and wake-word-recorder containers. The drive is mounted below it at
# /mnt/aivi-capture/drive, so mounts and unmounts reach the running container.
# Without the drive, drive/ is an empty, immutable directory on the SD card
# without the marker file, and the containers write nothing.
#
# Usage: sudo aivi-capture format /dev/sdX | setup | mount | eject
#        aivi-capture status

LABEL=USB-128GB
PARENT=/mnt/aivi-capture
MOUNT_POINT=${PARENT}/drive
MARKER=.aivi-capture-volume
SAMPLES_DIR=aivi-stt
TAKES_DIR=aivi-wake-word
# The container runs as uid 1000 (aivi).
OWNER=1000:1000
MOUNT_OPTIONS=nofail,noatime,nodev,nosuid,noexec,x-systemd.device-timeout=10s
FSTAB_TAG="# AIVI capture drive, written by aivi-capture setup"
MOUNT_SERVICE=aivi-capture-mount.service
UDEV_RULE=/etc/udev/rules.d/90-aivi-capture.rules

die() {
  echo "$*" >&2
  exit 1
}

require_root() {
  [[ ${EUID} -eq 0 ]] || die "Run with sudo."
}

# A whole USB disk with nothing of it mounted or in use.
check_format_target() {
  local dev=$1
  [[ -b ${dev} ]] || die "${dev} is not a block device."
  [[ $(lsblk -dno TYPE "${dev}") == disk ]] || die "${dev} is not a whole disk."
  [[ $(lsblk -dno TRAN "${dev}") == usb ]] || die "${dev} is not a USB disk."
  if lsblk -nro MOUNTPOINT "${dev}" | grep -q .; then
    die "${dev} or one of its partitions is mounted or in use."
  fi
}

cmd_format() {
  require_root
  local dev=${1:-}
  [[ -n ${dev} ]] || die "Usage: sudo aivi-capture format /dev/sdX"
  dev=$(readlink -f "${dev}")
  check_format_target "${dev}"
  local serial
  serial=$(lsblk -dno SERIAL "${dev}")

  lsblk -o NAME,SIZE,TYPE,TRAN,MODEL,SERIAL,FSTYPE,LABEL "${dev}"
  echo
  echo "ALL DATA ON ${dev} WILL BE ERASED."
  local answer
  read -rp "Type the device name (${dev##*/}) to continue: " answer
  [[ ${answer} == "${dev##*/}" ]] || die "Aborted."
  # The same disk, still unmounted, after the prompt.
  check_format_target "${dev}"
  [[ $(lsblk -dno SERIAL "${dev}") == "${serial}" ]] || die "${dev} changed."

  wipefs --all "${dev}" >/dev/null
  printf 'label: dos\ntype=83\n' | sfdisk --quiet "${dev}"
  udevadm settle
  local part="" _
  for _ in $(seq 20); do
    part=$(lsblk -nrpo NAME,TYPE "${dev}" | awk '$2 == "part" {print $1; exit}')
    [[ -n ${part} && -b ${part} ]] && break
    sleep 0.5
  done
  [[ -n ${part} && -b ${part} ]] || die "The new partition did not appear."
  # The new partition can start where an old file system did; remove its
  # signatures so nothing but ext4 is detected.
  wipefs --all "${part}" >/dev/null
  mkfs.ext4 -F -q -L "${LABEL}" -m 0 "${part}"
  udevadm settle
  echo "Formatted ${part} as ext4 (${LABEL}). Next: sudo aivi-capture setup"
}

# Propagation of the mount that holds PARENT; before PARENT exists, that of
# its parent directory, which is on the same mount.
propagation() {
  local target=${PARENT}
  [[ -d ${target} ]] || target=${PARENT%/*}
  findmnt -no PROPAGATION --target "${target}" 2>/dev/null || echo unknown
}

check_propagation() {
  local propagation
  propagation=$(propagation)
  [[ ${propagation} == shared* ]] || die "The mount holding ${PARENT} has \
propagation '${propagation}', not shared: a drive mounted later would never \
reach the container."
}

# Empty mount point on the SD card; immutable, so nothing can be written
# there while no drive is mounted.
cmd_prepare() {
  require_root
  install -d -o root -g root -m 0755 "${PARENT}"
  if mountpoint -q "${MOUNT_POINT}"; then
    return 0
  fi
  [[ -d ${MOUNT_POINT} ]] || mkdir -m 0755 "${MOUNT_POINT}"
  if [[ -n $(ls -A "${MOUNT_POINT}") ]]; then
    die "${MOUNT_POINT} is not empty although no drive is mounted."
  fi
  chattr +i "${MOUNT_POINT}"
}

write_fstab() {
  local uuid=$1 tmp
  tmp=$(mktemp)
  awk -v mp="${MOUNT_POINT}" -v tag="${FSTAB_TAG}" \
    '$0 != tag && $2 != mp' /etc/fstab >"${tmp}"
  printf '%s\nUUID=%s %s ext4 %s 0 2\n' \
    "${FSTAB_TAG}" "${uuid}" "${MOUNT_POINT}" "${MOUNT_OPTIONS}" >>"${tmp}"
  if ! cmp -s "${tmp}" /etc/fstab; then
    cp -p /etc/fstab /etc/fstab.aivi-capture.bak
    cat "${tmp}" >/etc/fstab
  fi
  rm -f "${tmp}"
}

# fstab mounts the drive at boot. When it is plugged in later, udev starts a
# service that mounts it; the service name needs no escaping in udev.
write_hotplug() {
  local uuid=$1
  cat >"/etc/systemd/system/${MOUNT_SERVICE}" <<EOF
[Unit]
Description=Mount the AIVI capture drive after it is plugged in

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'mountpoint -q ${MOUNT_POINT} || mount ${MOUNT_POINT} || mountpoint -q ${MOUNT_POINT}'
EOF
  cat >"${UDEV_RULE}" <<EOF
# AIVI capture drive, written by aivi-capture setup
ACTION=="add", SUBSYSTEM=="block", ENV{ID_FS_UUID}=="${uuid}", ENV{SYSTEMD_WANTS}+="${MOUNT_SERVICE}"
EOF
  systemctl daemon-reload
  udevadm control --reload-rules
}

# The data directories of both services, owned by the container user. Only
# on the mounted drive with its marker, never on the SD card.
make_data_dirs() {
  mountpoint -q "${MOUNT_POINT}" || die "The drive is not mounted."
  [[ -f ${MOUNT_POINT}/${MARKER} ]] ||
    die "The drive has no marker file. Run: sudo aivi-capture setup"
  local dir
  for dir in "${SAMPLES_DIR}" "${TAKES_DIR}"; do
    install -d -o "${OWNER%:*}" -g "${OWNER#*:}" -m 0755 \
      "${MOUNT_POINT}/${dir}"
  done
}

cmd_setup() {
  require_root
  check_propagation
  local dev=${1:-}
  if [[ -z ${dev} ]]; then
    local devices=()
    mapfile -t devices < <(blkid -c /dev/null -o device -t LABEL="${LABEL}" || true)
    ((${#devices[@]} == 1)) || die "Expected one drive labelled ${LABEL}, \
found ${#devices[@]}. Name it: sudo aivi-capture setup /dev/sdX1"
    dev=${devices[0]}
  fi
  [[ $(blkid -c /dev/null -o value -s TYPE "${dev}") == ext4 ]] ||
    die "${dev} is not ext4. Format it first: sudo aivi-capture format /dev/sdX"
  local uuid
  uuid=$(blkid -c /dev/null -o value -s UUID "${dev}")
  [[ -n ${uuid} ]] || die "${dev} has no UUID."

  if mountpoint -q "${MOUNT_POINT}"; then
    umount "${MOUNT_POINT}"
  fi
  cmd_prepare
  write_fstab "${uuid}"
  write_hotplug "${uuid}"
  mount "${MOUNT_POINT}"
  printf 'AIVI capture drive\nUUID=%s\n' "${uuid}" \
    >"${MOUNT_POINT}/${MARKER}"
  make_data_dirs
  cmd_status
}

cmd_mount() {
  require_root
  systemctl start "${MOUNT_SERVICE}"
  make_data_dirs
  cmd_status
}

cmd_eject() {
  require_root
  if ! mountpoint -q "${MOUNT_POINT}"; then
    echo "The drive is not mounted."
    return 0
  fi
  if [[ -n $(find "${MOUNT_POINT}/${TAKES_DIR}" -name '*.wav.part' \
    -print -quit 2>/dev/null) ]]; then
    die "A wake word take is running. Switch Wake-Word-Aufnahme off first."
  fi
  sync
  umount "${MOUNT_POINT}" || die "Could not unmount; try again in a moment."
  echo "The drive can be unplugged."
}

cmd_status() {
  echo "propagation: $(propagation)"
  if ! mountpoint -q "${MOUNT_POINT}" 2>/dev/null; then
    local flags=""
    [[ -d ${MOUNT_POINT} ]] && flags=$(lsattr -d "${MOUNT_POINT}" 2>/dev/null | awk '{print $1}')
    echo "drive:       not mounted"
    if [[ ${flags} == *i* ]]; then
      echo "mount point: empty and immutable"
    else
      echo "mount point: NOT immutable (run: sudo aivi-capture prepare)"
    fi
    return 0
  fi
  local samples="${MOUNT_POINT}/${SAMPLES_DIR}"
  echo "drive:       $(findmnt -no SOURCE,FSTYPE "${MOUNT_POINT}")"
  if [[ -f ${MOUNT_POINT}/${MARKER} ]]; then
    echo "marker:      present"
  else
    echo "marker:      MISSING, nothing is captured (run: sudo aivi-capture setup)"
  fi
  df -h --output=size,used,avail "${MOUNT_POINT}" | awk 'NR == 2 {print "space:       " $1 " total, " $2 " used, " $3 " free"}'
  if [[ -d ${samples} ]]; then
    local names
    names=$(find "${samples}" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%f\n' | sort)
    echo "samples:     $(grep -c . <<<"${names}" || true)"
    echo "newest:      $(tail -n 1 <<<"${names}")"
  else
    echo "samples:     no ${SAMPLES_DIR}/ directory (run: sudo aivi-capture mount)"
  fi
  local takes="${MOUNT_POINT}/${TAKES_DIR}/recordings"
  if [[ -d ${MOUNT_POINT}/${TAKES_DIR} ]]; then
    local positive negative running newest
    positive=$(find "${takes}/positive" -name '*.wav' 2>/dev/null | wc -l || true)
    negative=$(find "${takes}/negative" -name '*.wav' 2>/dev/null | wc -l || true)
    running=$(find "${takes}" -name '*.wav.part' 2>/dev/null | wc -l || true)
    newest=$(find "${takes}" -name '*.wav' -printf '%T@ %f\n' 2>/dev/null |
      sort -n | tail -n 1 | cut -d' ' -f2 || true)
    echo "takes:       ${positive} positive, ${negative} everyday, ${running} running"
    echo "newest take: ${newest:-none}"
  else
    echo "takes:       no ${TAKES_DIR}/ directory (run: sudo aivi-capture mount)"
  fi
}

case "${1:-status}" in
  format) cmd_format "${2:-}" ;;
  setup) cmd_setup "${2:-}" ;;
  prepare) cmd_prepare ;;
  mount) cmd_mount ;;
  eject) cmd_eject ;;
  status) cmd_status ;;
  *)
    echo "Usage: aivi-capture format <device>|setup [device]|prepare|mount|eject|status" >&2
    exit 2
    ;;
esac
