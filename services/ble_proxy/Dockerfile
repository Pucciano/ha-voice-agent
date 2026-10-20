# Bluetooth proxy for Home Assistant (docs/ble_proxy.md). Built on the proxy
# host, the Jetson or a Raspberry Pi with a 64-bit OS:
#   docker build -t aivi/ble-proxy:0.1.0 services/ble_proxy
FROM python:3.12-slim-bookworm

# The container runs its own BlueZ (5.66 in bookworm), so the host's version
# does not matter: L4T R32 ships 5.48, bleak needs 5.55 or later.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bluez dbus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/ble-proxy

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY main.conf /etc/bluetooth/main.conf
COPY entrypoint.sh ./entrypoint.sh
COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 6053

ENTRYPOINT ["/srv/ble-proxy/entrypoint.sh"]
