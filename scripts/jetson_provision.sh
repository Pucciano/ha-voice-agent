#!/usr/bin/env bash
set -euo pipefail

# One-time setup of a Jetson Nano, installed from the JetPack 4.6.x SD card
# image, as a headless speech server for Home Assistant. Run it on the Jetson
# as root, from a directory that also holds jetson_logmode.sh and
# docker-compose.yml (compose/jetson):
#   sudo bash jetson_provision.sh
# It is safe to run again. Reboot afterwards to finish the update.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADMIN_USER="${SUDO_USER:-aivi}"
COMPOSE_VERSION="v2.29.7"
COMPOSE_SHA256="6e9fbd5daa20dca5d7d89145081ae8155d68ef2928b497d9f85b54fe0f9dbb2c"
COMPOSE_BIN=/usr/local/lib/docker/cli-plugins/docker-compose

# Large desktop applications. On L4T R32.7 removing them pulls in only a few
# desktop meta packages. NVIDIA's full desktop package list would also remove
# network-manager, the CUDA compiler and TensorRT, so it is not used here.
DESKTOP_APPS='^(libreoffice|thunderbird|chromium|rhythmbox|shotwell|remmina|transmission|aisleriot|gnome-mahjongg|gnome-mines|gnome-sudoku|cheese|simple-scan|totem|deja-dup|gnome-todo|example-content)'
PROTECTED='^(nvidia-|cuda|libcudnn|libnvinfer|tensorrt|docker|containerd|runc|openssh|network-manager|systemd|udev|netplan|sudo|apt$|dpkg|python3$|python3-minimal|libc6|linux-|ubuntu-minimal|ca-certificates|curl|openssl)'

log() { printf '\n==> %s\n' "$*"; }

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi
if ! grep -q "^# R32 " /etc/nv_tegra_release; then
  echo "This is not an L4T R32 (JetPack 4) system." >&2
  exit 1
fi
for file in jetson_logmode.sh docker-compose.yml; do
  if [[ ! -f "${HERE}/${file}" ]]; then
    echo "Missing ${HERE}/${file}" >&2
    exit 1
  fi
done

export DEBIAN_FRONTEND=noninteractive
APT=(apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

log "Stop automatic updates; this device is updated on purpose, then runs offline"
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
systemctl disable --now unattended-upgrades >/dev/null 2>&1 || true
while fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; do
  echo "Waiting for another apt process to finish ..."
  sleep 5
done

log "Update to the last JetPack 4 release (L4T R32.7.x)"
apt-get update
"${APT[@]}" dist-upgrade
# The SD card image ships without curl.
"${APT[@]}" install curl

log "Remove large desktop applications"
mapfile -t apps < <(dpkg-query -W -f='${Package}\n' | grep -E "${DESKTOP_APPS}" || true)
if ((${#apps[@]})); then
  mapfile -t removals < <(apt-get -s purge "${apps[@]}" | awk '/^Purg /{print $2}')
  if printf '%s\n' "${removals[@]}" | grep -Eq "${PROTECTED}"; then
    echo "Refusing to purge, protected packages would go too:" >&2
    printf '%s\n' "${removals[@]}" | grep -E "${PROTECTED}" >&2
    exit 1
  fi
  "${APT[@]}" purge "${apps[@]}"
else
  echo "Already removed."
fi
apt-get clean

log "Boot to the console instead of the desktop"
systemctl set-default multi-user.target

log "Docker Compose ${COMPOSE_VERSION}"
if ! docker compose version 2>/dev/null | grep -q "${COMPOSE_VERSION}"; then
  install -d /usr/local/lib/docker/cli-plugins
  curl -fsSL -o "${COMPOSE_BIN}.tmp" \
    "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-aarch64"
  echo "${COMPOSE_SHA256}  ${COMPOSE_BIN}.tmp" | sha256sum -c -
  chmod 0755 "${COMPOSE_BIN}.tmp"
  mv "${COMPOSE_BIN}.tmp" "${COMPOSE_BIN}"
fi
docker compose version

log "Docker access for ${ADMIN_USER}"
usermod -aG docker "${ADMIN_USER}"

log "Speech services: data directories and compose project"
install -d -m 0755 \
  /srv/wyoming/speech-to-phrase/models /srv/wyoming/speech-to-phrase/train \
  /srv/wyoming/piper/data /srv/wyoming/whisper/data
install -d -m 0750 -g docker /opt/aivi/compose
install -m 0644 "${HERE}/docker-compose.yml" /opt/aivi/compose/docker-compose.yml

log "Log mode switch, dev mode while the system is tested"
install -m 0755 "${HERE}/jetson_logmode.sh" /usr/local/sbin/aivi-logmode
/usr/local/sbin/aivi-logmode dev

log "Done. Reboot to finish the update: sudo reboot"
