# Bluetooth proxy

The Jetson in the living room gives Home Assistant Bluetooth through a USB
dongle. The proxy (`services/ble_proxy`) speaks the ESPHome native API with
Noise encryption, so Home Assistant adds it like an ESP32 Bluetooth proxy:
passive and active scanning, raw advertisements and up to three GATT
connections. The same container runs on a Raspberry Pi; only the host
preparation differs. This is option C of issue #29 for one room.

Uses in the living room:

- the Vogel's TVM 7675 TV mount, which only speaks Bluetooth (integration
  `vogels_motion_mount_ble`), and other BLE devices with a Home Assistant
  integration;
- presence: Private BLE Device for the iPhones, Bermuda for distance and,
  with more proxies, the room.

| Part | Where | What it does |
|---|---|---|
| `scripts/ble_proxy_host.sh` | host, as `/usr/local/sbin/aivi-ble-host` | makes the adapter work, switches the host's bluetoothd off |
| `aivi-rtk-btusb.service` | Jetson only | loads the patched Realtek driver at boot, with a crash guard |
| `aivi/ble-proxy` image | `compose/ble_proxy`, deployed to `/opt/aivi/ble-proxy` | dbus, bluetoothd (BlueZ 5.66) and the proxy; port 6053 |

## Findings (2026-10-20)

- The dongle ("Bluetooth 5.3 Radio", USB `0bda:a729`) is a Realtek
  RTL8761BU; the controller reports Bluetooth 5.1 (`hci_ver 0x0a`,
  `lmp_subver 0x8761`).
- The kernel 4.9 `btusb`/`btrtl` of L4T R32 takes it for an RTL8761A, loads
  `rtl8761a_fw.bin`, and the download times out (`0xfc20 tx timeout`); the
  adapter stays down with address `00:00:00:00:00:00`.
- L4T also ships Realtek's own driver `rtk_btusb` 3.1. It knows the chip,
  but finds the firmware by USB product id, and its table lacks `0xa729`.
  Built from NVIDIA's sources with that id added next to the RTL8761BU entry
  `0x8771`, and with the linux-firmware files `rtl8761bu_fw.bin` and
  `rtl8761bu_config.bin`, the adapter comes up.
- `rtk_btusb` 3.1 crashes the kernel when the dongle goes away while it
  loads the firmware (use after free in `download_patch`; seen once when the
  test unbound the dongle right after loading the driver; `kernel.panic=5`
  rebooted the Jetson). Hence no re-probing in the script, and a crash
  guard at boot.
- The host's BlueZ is 5.48; bleak needs 5.55. The container brings Debian
  bookworm's BlueZ 5.66, which runs on the 4.9 kernel (management interface
  1.14).
- Kernel 4.9 has no advertisement monitor, so the Jetson scans actively
  only. It also filters duplicate advertisements in the controller until
  discovery restarts, about every 10 s: 37–42 addresses gave about 4
  advertisements per second, but each device only one every 6 to 8 s, and
  Home Assistant's interval tracker measured 11 to 66 s per device (median
  16 s over 29 devices, the mount 12 s). Enough to see whether a phone is
  home, thin for Bermuda's room tracking. A kernel from 5.10 (Raspberry Pi
  OS) scans passively.
- The 4.9 kernel refuses an LE connection while discovery runs
  (`Operation already in progress`). The proxy pauses the scanner while it
  connects, as an ESP32 proxy does. Measured: connect to the mount in 4.9 s,
  ATT MTU 158, 6 services with 59 characteristics.
- The mount advertises as "Wohnzimmer" (random static address) with service
  `030013ac-4202-2d8e-ea11-3959c46ca10e`, which the Vogel's integration
  matches. It hangs on the TV plug, so it is gone while the TV is cut off.

## How it works

- The host provides the adapter only. bluetoothd, its D-Bus and the proxy run
  in one container with host networking (Bluetooth sockets exist only in the
  host's network namespace) and `NET_ADMIN`, not privileged. bluetoothd runs
  without plugins; the proxy powers the adapter itself.
- The API is the ESPHome native API with
  `Noise_NNpsk0_25519_ChaChaPoly_SHA256` and a 32-byte key, as `api:
  encryption: key` on an ESP32. The proxy refuses plaintext clients; a client
  without the key cannot list, scan or connect. The protobuf messages come
  from `aioesphomeapi` 46.6.0, the version of Home Assistant 2026.10.
- Raw advertisements come from the kernel's management socket (Device Found
  events with AD data, address type and RSSI), the way Home Assistant reads a
  local adapter. bluetoothd does the scanning; the proxy sends the
  advertisements in batches every 100 ms.
- Connections use bleak over BlueZ. Home Assistant writes the CCCD after a
  notify request, as an ESP32 needs it; BlueZ refuses such writes, so the
  proxy turns them into StartNotify or StopNotify.
- A connection belongs to the API session that opened it. When Home
  Assistant disconnects or restarts, the proxy disconnects its devices and
  frees the slots.
- The device MAC in Home Assistant is the adapter's address. A dongle that
  moves to another host stays the same Home Assistant device.
- Not supported: pairing and bonding (the mount uses a PIN over GATT), and
  connection parameters.

## Prepare the host

### Jetson (L4T R32)

Copy the script and run it in a terminal, because sudo asks for the password:

```bash
scp scripts/ble_proxy_host.sh aivi@<jetson-ip>:/tmp/
```

```bash
ssh -t aivi@<jetson-ip> sudo bash /tmp/ble_proxy_host.sh setup
```

It masks the host's `bluetooth.service`, installs the RTL8761BU firmware
from linux-firmware (pinned tag and checksums) as `/lib/firmware/rtl8761bu_*`,
downloads `rtk_btusb` from the OE4T mirror of NVIDIA's kernel sources
(pinned commit and checksums), builds it unchanged and checks that the
srcversion equals the installed driver's, adds the product id, builds again,
installs `/lib/modules/$(uname -r)/updates/aivi/rtk_btusb.ko`, blacklists
`btusb` and `rtk_btusb` for udev and installs `aivi-rtk-btusb.service`. That
service loads the driver at boot. A marker file
(`/var/lib/aivi/rtk_btusb.loading`) exists during the load; if the kernel
crashed during the last load, the service refuses to load the driver again,
so a crash cannot loop the boot. It needs the kernel headers
(`nvidia-l4t-kernel-headers`, part of JetPack) and `make`.

Reboot once and check that the adapter comes up by itself:

```bash
ssh aivi@<jetson-ip> aivi-ble-host status
```

`hci0: driver rtk_btusb` with a real address is right. Rollback: `sudo rm
/lib/modules/$(uname -r)/updates/aivi/rtk_btusb.ko /etc/modprobe.d/aivi-ble-proxy.conf
&& sudo systemctl disable aivi-rtk-btusb.service && sudo depmod -a && sudo
systemctl unmask bluetooth.service`.

### Raspberry Pi 3B+

Use Raspberry Pi OS Lite 64-bit (bookworm or later), so the image is the same
arm64 build as on the Jetson, and Ethernet: the Pi's onboard Bluetooth shares
its radio with Wi-Fi. Install Docker from Docker's Debian repository
(docs.docker.com, "Install Docker Engine on Debian"), then run the same
script; on the Pi it only installs `firmware-realtek` and switches the
host's bluetoothd off:

```bash
sudo bash /tmp/ble_proxy_host.sh setup
```

With the dongle and the onboard adapter both present, `hci0` is not fixed.
Either set `dtoverlay=disable-bt` in `/boot/firmware/config.txt` (dongle
only) or set `BLE_PROXY_ADAPTER` to the right `hciN` (see
`aivi-ble-host status`). A Pi needs a proper 5.1 V supply; undervoltage
shows up as Bluetooth timeouts (`vcgencmd get_throttled`).

## Install or update the proxy

Build the image on the host and copy the compose file:

```bash
ssh aivi@<jetson-ip> 'rm -rf /tmp/aivi-ble && mkdir /tmp/aivi-ble'
scp -r services/ble_proxy compose/ble_proxy/docker-compose.yml aivi@<jetson-ip>:/tmp/aivi-ble/
ssh aivi@<jetson-ip> docker build -t aivi/ble-proxy:0.1.0 /tmp/aivi-ble/ble_proxy
```

The first install creates the key. The line below makes a key on the Mac and
puts it only into the clipboard; on the Jetson, a hidden prompt takes it
(Cmd+V, Enter) and writes `/opt/aivi/ble-proxy/.env` (root:docker, 0640).
Keep the key in the clipboard for Home Assistant:

```bash
openssl rand -base64 32 | tr -d '\n' | pbcopy && ssh -t aivi@<jetson-ip> 'read -rsp "BLE proxy key (Cmd+V, Enter): " k && echo && printf "BLE_PROXY_API_KEY=%s\n" "$k" | sudo sh -c "install -d -m 0750 -g docker /opt/aivi/ble-proxy && install -m 0644 /tmp/aivi-ble/docker-compose.yml /opt/aivi/ble-proxy/docker-compose.yml && umask 027 && cat > /opt/aivi/ble-proxy/.env && chgrp docker /opt/aivi/ble-proxy/.env" && unset k && cd /opt/aivi/ble-proxy && docker compose up -d'
```

An update only builds the image (with a new tag in the compose file when the
code changes) and recreates the container:

```bash
ssh aivi@<jetson-ip> 'cd /opt/aivi/ble-proxy && docker compose up -d'
```

The defaults name the device `aivi-ble-living-room` ("Bluetooth
Wohnzimmer", area Wohnzimmer). Another room sets `BLE_PROXY_NAME`,
`BLE_PROXY_FRIENDLY_NAME` and `BLE_PROXY_AREA` in `.env`; the other settings
are listed in `services/ble_proxy/app/config.py`. Never run `docker compose
config` or `docker inspect` on this project unfiltered: both print the key.

## Home Assistant

1. Settings → Devices & services: Home Assistant has discovered
   "Bluetooth Wohnzimmer" (ESPHome). Add it, paste the key, area Wohnzimmer.
   The Bluetooth integration then lists the proxy as a remote adapter, and
   Settings → Bluetooth → Advertisement monitor shows what it hears.
   Without discovery: add the ESPHome integration with `<jetson-ip>` and port
   6053.
2. Vogel's mount: install `Nailik/vogels_motion_mount_ble` through HACS
   (tested with v0.1.9), restart Home Assistant. The mount is discovered by
   its service UUID; enter its PIN. The TV must be on its plug, since the
   mount's power supply hangs on it.
3. Presence of the iPhones: the core integration **Private BLE Device** needs
   each phone's identity resolving key (IRK); its documentation describes how
   to get it. It gives a device tracker and a distance per phone. **Bermuda**
   (HACS, v0.8.7) adds distance and area sensors; with one proxy it only
   tells "near the living room", room accuracy needs a proxy per room
   (issue #29, option B).

## Verify

- `aivi-ble-host status` on the host: adapter with driver and address.
- `docker compose ps` in `/opt/aivi/ble-proxy`: the container runs.
- Home Assistant: the ESPHome device is connected; the Bluetooth page shows
  the proxy with recent advertisements, among them "Wohnzimmer" (the mount).
- In dev log mode, `docker compose logs ble-proxy` shows the API sessions,
  connections and, every 5 minutes, the advertisement rate, the number of
  addresses and the slots in use.

## Logging

On the Jetson, `aivi-logmode` decides ([Jetson setup](jetson_setup.md#logging)):
in prod nothing is kept. The proxy logs no advertisement content and no key;
it logs device addresses when it connects.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `aivi-ble-host status`: `00:00:00:00:00:00` or no adapter | Firmware or driver missing, or `btusb` took the dongle | Run `setup` again; `dmesg \| grep rtk_btusb` |
| Status: `rtk_btusb.loading exists` | The last driver load crashed the kernel | Check `/sys/fs/pstore`; replug the dongle while the Jetson is off, then `sudo rm /var/lib/aivi/rtk_btusb.loading && sudo systemctl start aivi-rtk-btusb` |
| Log: `Scanner failed to start: Resource Not Ready` | Adapter not powered yet | Retries every 10 s by itself; persistent: rfkill (`aivi-ble-host status`) |
| Connect fails with `Operation already in progress` | Another discovery still running on the 4.9 kernel | The proxy pauses its own scanner; check that no other BlueZ client (host bluetoothd) runs |
| Home Assistant: invalid encryption key | Key in Home Assistant differs from `.env` | Enter the key again in the ESPHome entry, or write a new one ([install](#install-or-update-the-proxy)) |
| Proxy not discovered | mDNS does not cross VLANs, or a wrong `BLE_PROXY_ADDRESS` | Add it by IP; the log names the announced address |
| Mount unavailable in Home Assistant | TV plug off | Expected; the TV power scripts switch the plug on first |
| Log `adapter hci0 disappeared`, then `Waiting for hci0` | Dongle unplugged or driver gone; the container restarts and waits | Plug in; on the Jetson only with the Jetson off (driver crash), then check `aivi-ble-host status` |
