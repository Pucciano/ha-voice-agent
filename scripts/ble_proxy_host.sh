#!/usr/bin/env bash
set -euo pipefail

# Host preparation for the Bluetooth proxy (docs/ble_proxy.md). The proxy
# container brings its own BlueZ, so the host only has to provide a working
# Bluetooth adapter and must not run bluetoothd itself: two bluetoothd on one
# adapter fight over it.
#
#   setup        make the adapter work and switch the host's bluetoothd off
#   status       show the adapters, their drivers and addresses
#   load-driver  Jetson only: load the patched rtk_btusb once at boot; run by
#                aivi-rtk-btusb.service
#
# Platforms:
#   Jetson Nano, L4T R32 (kernel 4.9): its btusb driver mistakes a Realtek
#     RTL8761BU dongle for an RTL8761A and fails to load the firmware.
#     Realtek's own rtk_btusb driver, part of L4T, handles the chip but finds
#     the firmware by USB product id, and its table lacks 0xa729, the id of
#     the common "Bluetooth 5.3 Radio" dongles. setup builds the same driver
#     from NVIDIA's sources with that id added, installs it next to the
#     original, installs the chip's firmware and keeps btusb away. A boot
#     service loads the driver and refuses to load it again after a load that
#     never finished: rtk_btusb crashes the kernel when the dongle goes away
#     while it starts, and kernel.panic reboots, so a crash at load must not
#     turn into a boot loop.
#   Raspberry Pi OS: the kernel handles the onboard adapter and the dongle;
#     setup only makes sure the Realtek firmware package is installed.
#
# Usage: sudo bash ble_proxy_host.sh setup
#        bash ble_proxy_host.sh status
# setup is safe to run again and installs this script as
# /usr/local/sbin/aivi-ble-host. On the Jetson, reboot once afterwards to
# check that the adapter also comes up at boot.

# RTL8761BU firmware from linux-firmware, pinned by tag and checksum. rtk_btusb
# looks for it in /lib/firmware without the rtl_bt/ directory and without
# .bin, like the rtl8822cu_* files L4T ships.
FIRMWARE_TAG=20261019
FIRMWARE_URL="https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git/plain/rtl_bt"
FIRMWARE_FILES=(
  "rtl8761bu_fw.bin 1d7a9597349ad89344fa16c1913d3e39e9a12e966e417ca16871bc79bbe59edb rtl8761bu_fw"
  "rtl8761bu_config.bin 6c28a3f07c6a30ed208c4b64862a23f02b7d93543ea980edd24df16bab45095f rtl8761bu_config"
)

# rtk_btusb 3.1 as shipped in L4T R32.7.x, from the OE4T mirror of NVIDIA's
# kernel sources, pinned by commit and checksum. Built unchanged, the sources
# must give the srcversion of the installed driver; with the product id added
# they give RTK_PATCHED_SRCVERSION.
RTK_SRC_URL="https://raw.githubusercontent.com/OE4T/linux-tegra-4.9/6944a5ce1dae5947ff24ada6b6592d9e3062d38c/nvidia/drivers/bluetooth/realtek"
RTK_SRC_FILES=(
  "Makefile b26736f7906212cab5548e76ae6070ea8f435cf9658734e3fea55aab40a36fe7"
  "rtk_bt.c 90c2195b7b4b683b576a88acdc1ae06dd1ad5be4a0a618dee5872b47e769b610"
  "rtk_bt.h b12dd57e0bb7c63bb8245855ffe6a4f99188438a54bdc7830c0ec6cc32fa40f0"
  "rtk_coex.c 53c67b14a5073f406af85373b2797246b0e69a9b8a7f48988039f713368aec45"
  "rtk_coex.h 1a903638d9519ff9fc5bd330ce2bb3bca5c51096b1366ac6775b9a1f82d1ecd2"
  "rtk_misc.c 2037d972fc1442c9f222c02b56234fcb294d9ecaa99c50c26b326f3dfdd0e6c0"
  "rtk_misc.h 1eeb3c3dadc68348d7757416824e5ac6fd78d3018beebd4b1996dad7591ef895"
)
RTK_ORIGINAL_SRCVERSION=C2EB8CCA3C9FFB551D54D4B
RTK_PATCHED_SRCVERSION=36BE323B8CEF638078FD961
RTK_BUILD_DIR=/usr/local/src/aivi-rtk-btusb
# Exists from the start of a driver load until the adapter is up; found at the
# next start, it means the last load crashed the kernel.
RTK_LOAD_MARKER=/var/lib/aivi/rtk_btusb.loading
RTK_UNIT=aivi-rtk-btusb.service
INSTALLED_SCRIPT=/usr/local/sbin/aivi-ble-host

MODPROBE_CONF=/etc/modprobe.d/aivi-ble-proxy.conf
NO_ADDRESS=00:00:00:00:00:00

log() { printf '\n==> %s\n' "$*"; }

die() {
  echo "$*" >&2
  exit 1
}

require_root() {
  [[ ${EUID} -eq 0 ]] || die "Run with sudo."
}

platform() {
  if grep -qs "^# R32 " /etc/nv_tegra_release; then
    echo l4t-r32
  elif grep -qas "Raspberry Pi" /proc/device-tree/model; then
    echo raspberry-pi
  else
    echo other
  fi
}

interface_driver() {
  local link
  link=$(readlink -f "$1/driver" 2>/dev/null) || true
  [[ -n ${link} ]] && basename "${link}" || echo none
}

# Downloads a file and checks its sha256.
fetch() {
  local url=$1 sum=$2 dest=$3
  curl -fsSL -o "${dest}.tmp" "${url}"
  echo "${sum}  ${dest}.tmp" | sha256sum -c --quiet - || {
    rm -f "${dest}.tmp"
    die "Checksum mismatch for ${url}."
  }
  mv "${dest}.tmp" "${dest}"
}

install_realtek_firmware() {
  local entry name sum target
  for entry in "${FIRMWARE_FILES[@]}"; do
    read -r name sum target <<<"${entry}"
    if echo "${sum}  /lib/firmware/${target}" | sha256sum -c --status - 2>/dev/null; then
      echo "/lib/firmware/${target} is current."
      continue
    fi
    fetch "${FIRMWARE_URL}/${name}?h=${FIRMWARE_TAG}" "${sum}" "/lib/firmware/${target}"
    chmod 0644 "/lib/firmware/${target}"
    echo "Installed /lib/firmware/${target} (linux-firmware ${FIRMWARE_TAG})."
  done
}

srcversion_of() {
  modinfo -F srcversion "$1" 2>/dev/null || true
}

build_rtk_btusb() {
  local kernel original target entry name sum
  kernel=$(uname -r)
  original=/lib/modules/${kernel}/kernel/drivers/bluetooth/realtek/rtk_btusb.ko
  target=/lib/modules/${kernel}/updates/aivi/rtk_btusb.ko
  if [[ $(srcversion_of "${target}") == "${RTK_PATCHED_SRCVERSION}" ]]; then
    echo "${target} is current."
    return
  fi
  [[ $(srcversion_of "${original}") == "${RTK_ORIGINAL_SRCVERSION}" ]] ||
    die "The installed rtk_btusb is not the one these sources were checked against."
  [[ -d /lib/modules/${kernel}/build ]] ||
    die "Kernel headers missing (package nvidia-l4t-kernel-headers)."

  rm -rf "${RTK_BUILD_DIR}"
  install -d "${RTK_BUILD_DIR}"
  for entry in "${RTK_SRC_FILES[@]}"; do
    read -r name sum <<<"${entry}"
    fetch "${RTK_SRC_URL}/${name}" "${sum}" "${RTK_BUILD_DIR}/${name}"
  done

  echo "Building the unchanged driver to compare it with the installed one."
  make -s -C "/lib/modules/${kernel}/build" M="${RTK_BUILD_DIR}" modules >/dev/null
  [[ $(srcversion_of "${RTK_BUILD_DIR}/rtk_btusb.ko") == "${RTK_ORIGINAL_SRCVERSION}" ]] ||
    die "The downloaded sources differ from the installed driver."

  echo "Adding product id 0xa729 next to the RTL8761BU entry 0x8771."
  sed -i \
    -e '/^\t{0x8771, 0x8761, /a\	{0xa729, 0x8761, "mp_rtl8761bu_fw", "rtl8761bu_fw", "rtl8761bu_config", NULL, 0}, /* RTL8761BUV, added by ble_proxy_host.sh */' \
    -e 's/^\(\t*\)case 0x8771:$/\1case 0xa729:\n\1case 0x8771:/' \
    "${RTK_BUILD_DIR}/rtk_misc.c"
  make -s -C "/lib/modules/${kernel}/build" M="${RTK_BUILD_DIR}" modules >/dev/null
  [[ $(srcversion_of "${RTK_BUILD_DIR}/rtk_btusb.ko") == "${RTK_PATCHED_SRCVERSION}" ]] ||
    die "The patched driver is not the expected build."

  install -D -m 0644 "${RTK_BUILD_DIR}/rtk_btusb.ko" "${target}"
  depmod -a "${kernel}"
  echo "Installed ${target}; it takes precedence over the original."
}

# Loads the patched rtk_btusb; loading it probes the unbound dongle once.
# Never make the kernel probe a bound dongle again (unbind, re-authorize,
# replug while it starts): rtk_btusb 3.1 crashes the kernel when the device
# goes away while it loads the firmware.
load_driver() {
  require_root
  local module
  if [[ $(cat /sys/module/rtk_btusb/srcversion 2>/dev/null) == "${RTK_PATCHED_SRCVERSION}" ]]; then
    echo "The patched rtk_btusb is loaded."
    return
  fi
  if [[ -e ${RTK_LOAD_MARKER} ]]; then
    die "The last rtk_btusb load did not finish, probably a crash; not loading it again. See docs/ble_proxy.md, Troubleshooting."
  fi
  [[ $(srcversion_of rtk_btusb) == "${RTK_PATCHED_SRCVERSION}" ]] ||
    die "modprobe would not load the patched rtk_btusb."
  install -d "${RTK_LOAD_MARKER%/*}"
  : >"${RTK_LOAD_MARKER}"
  sync
  for module in btusb rtk_btusb; do
    if lsmod | grep -q "^${module} "; then
      modprobe -r "${module}"
    fi
  done
  modprobe rtk_btusb
  if wait_for_adapter; then
    echo "Loaded the patched rtk_btusb; adapter $(adapter_address)."
  else
    echo "Loaded the patched rtk_btusb, but no adapter came up." >&2
  fi
  # The kernel survived the load, so the next boot may load it again.
  rm -f "${RTK_LOAD_MARKER}"
  sync
}

install_boot_service() {
  if [[ $(readlink -f "${BASH_SOURCE[0]}") != "${INSTALLED_SCRIPT}" ]]; then
    install -m 0755 "${BASH_SOURCE[0]}" "${INSTALLED_SCRIPT}"
  fi
  cat >"/etc/systemd/system/${RTK_UNIT}" <<EOF
# Written by ble_proxy_host.sh (docs/ble_proxy.md).
[Unit]
Description=Patched rtk_btusb driver for the Bluetooth proxy dongle

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${INSTALLED_SCRIPT} load-driver

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "${RTK_UNIT}"
}

unblock_rfkill() {
  local r
  for r in /sys/class/rfkill/rfkill*; do
    [[ -r ${r}/type && $(<"${r}/type") == bluetooth ]] || continue
    echo 0 >"${r}/soft"
  done
}

# Prints the address of the first adapter once the kernel has set it up.
adapter_address() {
  hciconfig 2>/dev/null | awk '/BD Address:/ {print $3; exit}'
}

wait_for_adapter() {
  local _ address
  for _ in $(seq 1 30); do
    address=$(adapter_address)
    if [[ -n ${address} && ${address} != "${NO_ADDRESS}" ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

setup() {
  require_root
  local plat
  plat=$(platform)
  log "Platform: ${plat}"

  # First, so that the host's bluetoothd never takes the new adapter.
  log "Host bluetoothd off; the proxy container runs its own"
  systemctl disable --now bluetooth.service >/dev/null 2>&1 || true
  systemctl mask bluetooth.service

  case ${plat} in
    l4t-r32)
      log "Realtek RTL8761BU firmware for rtk_btusb"
      install_realtek_firmware
      log "rtk_btusb with the product id of the dongle"
      build_rtk_btusb
      log "No automatic driver loading (${MODPROBE_CONF})"
      cat >"${MODPROBE_CONF}" <<EOF
# Written by ble_proxy_host.sh (docs/ble_proxy.md). The kernel 4.9 btusb
# driver mistakes the RTL8761BU dongle for an RTL8761A and fails to load its
# firmware; nothing else on this host uses it. The patched rtk_btusb is loaded
# by ${RTK_UNIT}, not by udev.
blacklist btusb
blacklist rtk_btusb
EOF
      log "Boot service ${RTK_UNIT}"
      install_boot_service
      log "Load the driver now"
      load_driver
      ;;
    raspberry-pi)
      log "Realtek firmware package for USB dongles"
      if ! dpkg-query -W -f='${Status}' firmware-realtek 2>/dev/null | grep -q "ok installed"; then
        apt-get update
        apt-get -y install firmware-realtek
      else
        echo "firmware-realtek is installed."
      fi
      ;;
    *)
      echo "Unknown platform: only the steps for every host run."
      ;;
  esac

  log "Bluetooth not blocked by rfkill"
  unblock_rfkill

  log "Adapters"
  wait_for_adapter || die "No Bluetooth adapter came up; see docs/ble_proxy.md, Troubleshooting."
  status
}

status() {
  local hci intf
  echo "Platform: $(platform)"
  echo "Host bluetoothd: $(systemctl is-enabled bluetooth.service 2>/dev/null || true), $(systemctl is-active bluetooth.service 2>/dev/null || true)"
  if ! compgen -G '/sys/class/bluetooth/hci*' >/dev/null; then
    echo "No Bluetooth adapter."
    return 1
  fi
  for hci in /sys/class/bluetooth/hci*; do
    intf=$(readlink -f "${hci}/device")
    echo "${hci##*/}: driver $(interface_driver "${intf}"), bus ${intf##*/}"
  done
  if command -v hciconfig >/dev/null; then
    # The kernel reads the address while it sets the adapter up, so it shows
    # even when the adapter is down; 00:00:00:00:00:00 means the setup failed.
    hciconfig | grep -E '^hci|BD Address|UP|DOWN'
  fi
  if [[ -e ${RTK_LOAD_MARKER} ]]; then
    echo "${RTK_LOAD_MARKER} exists: the last driver load did not finish."
  fi
}

case ${1:-} in
  setup) setup ;;
  status) status ;;
  load-driver) load_driver ;;
  *) die "Usage: $0 setup | status | load-driver" ;;
esac
