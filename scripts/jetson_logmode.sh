#!/usr/bin/env bash
set -euo pipefail

# Switch where the Jetson keeps its logs. Installed as /usr/local/sbin/aivi-logmode.
#
#   prod    nothing is written to the SD card: journald keeps logs in RAM
#           only, and containers log nothing
#   dev     journald on the card (at most 256 MB), containers keep
#           3 x 10 MB each
#   status  show the current settings
#
# rsyslog stays off in both modes; journald is the only system log.
# Usage: sudo aivi-logmode prod|dev|status

MODE="${1:-status}"
JOURNALD_DROPIN=/etc/systemd/journald.conf.d/90-aivi-logmode.conf
DAEMON_JSON=/etc/docker/daemon.json
COMPOSE_DIR=/opt/aivi/compose

show_status() {
  echo "journald: $(sed -n 's/^Storage=//p' "${JOURNALD_DROPIN}" 2>/dev/null || echo default)"
  echo "rsyslog:  $(systemctl is-enabled rsyslog 2>/dev/null || true)"
  echo "docker:   $(docker info --format '{{.LoggingDriver}}' 2>/dev/null || echo unknown)"
}

# Merge the log driver into daemon.json and keep everything else, such as the
# nvidia runtime that JetPack registers there.
set_docker_log_driver() {
  python3 - "${DAEMON_JSON}" "$1" <<'EOF'
import json
import sys

path, mode = sys.argv[1], sys.argv[2]
try:
    with open(path) as fh:
        config = json.load(fh)
except FileNotFoundError:
    config = {}
if mode == "prod":
    config["log-driver"] = "none"
    config.pop("log-opts", None)
else:
    config["log-driver"] = "local"
    config["log-opts"] = {"max-size": "10m", "max-file": "3"}
with open(path, "w") as fh:
    json.dump(config, fh, indent=4)
    fh.write("\n")
EOF
}

case "${MODE}" in
  prod | dev) ;;
  status)
    show_status
    exit 0
    ;;
  *)
    echo "Usage: aivi-logmode prod|dev|status" >&2
    exit 2
    ;;
esac

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

mkdir -p "$(dirname "${JOURNALD_DROPIN}")"
if [[ "${MODE}" == prod ]]; then
  printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=32M\nForwardToSyslog=no\n' > "${JOURNALD_DROPIN}"
else
  printf '[Journal]\nStorage=persistent\nSystemMaxUse=256M\nForwardToSyslog=no\n' > "${JOURNALD_DROPIN}"
  mkdir -p /var/log/journal
  systemd-tmpfiles --create --prefix /var/log/journal
fi
systemctl restart systemd-journald
systemctl disable --now rsyslog >/dev/null 2>&1 || true

set_docker_log_driver "${MODE}"
systemctl restart docker

# The log driver is fixed when a container is created, so recreate running
# services to apply the new mode.
if [[ -f "${COMPOSE_DIR}/docker-compose.yml" ]] &&
  [[ -n "$(docker compose --project-directory "${COMPOSE_DIR}" ps -q 2>/dev/null)" ]]; then
  docker compose --project-directory "${COMPOSE_DIR}" up -d --force-recreate
fi

if [[ "${MODE}" == prod && -d /var/log/journal ]]; then
  echo "Note: logs from dev mode remain in /var/log/journal until removed."
fi
show_status
