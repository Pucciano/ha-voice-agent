#!/bin/bash
set -euo pipefail

# Starts the container's own system bus, bluetoothd and the proxy, and stops
# all three when one of them ends, so the restart policy brings back a clean
# set. The host must not run bluetoothd (scripts/ble_proxy_host.sh).

# Passive scanning needs bluetoothd's advertisement monitor, which is
# experimental and needs kernel 5.10 or later. The L4T R32 kernel (4.9)
# scans actively only.
experimental=()
IFS=. read -r major minor _ <<<"$(uname -r)"
if ((major > 5 || (major == 5 && minor >= 10))); then
  experimental=(--experimental)
fi

mkdir -p /run/dbus
rm -f /run/dbus/pid /run/dbus/system_bus_socket
dbus-daemon --system --nofork --nopidfile &
for _ in $(seq 1 50); do
  [[ -S /run/dbus/system_bus_socket ]] && break
  sleep 0.1
done

# Only the GATT client is needed; every plugin stays off.
/usr/libexec/bluetooth/bluetoothd --nodetach --noplugin='*' "${experimental[@]}" &

python -m app.main &

stop_all() {
  local pids
  mapfile -t pids < <(jobs -p)
  ((${#pids[@]} == 0)) || kill -TERM "${pids[@]}" 2>/dev/null || true
}
trap stop_all TERM INT
status=0
wait -n || status=$?
stop_all
wait || true
exit "${status}"
