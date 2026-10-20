"""Entry point: python -m app.main, configured by the environment (config.py).

Waits for the adapter, then serves the ESPHome API and announces itself by
mDNS. It exits when the adapter disappears; the container's restart policy
starts it again, and it waits for the adapter anew.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from . import __version__
from .api import ApiServer, DeviceIdentity
from .bluez import BlueZBackend
from .config import ConfigError, Settings, from_env
from .mdns import Announcer, default_address
from .mgmt import MgmtError
from .proxy import STATS_INTERVAL, BluetoothProxy

_LOGGER = logging.getLogger(__name__)
ADAPTER_RETRY = 5.0


async def _wait_for_adapter(
    backend: BlueZBackend, adapter: str, stop: asyncio.Event
) -> str | None:
    warned = False
    while not stop.is_set():
        try:
            return await backend.open()
        except (OSError, MgmtError, TimeoutError) as err:
            if not warned:
                _LOGGER.warning("Waiting for %s: %s", adapter, err)
                warned = True
        try:
            await asyncio.wait_for(stop.wait(), ADAPTER_RETRY)
        except TimeoutError:
            pass
    return None


async def _log_stats(proxy: BluetoothProxy) -> None:
    while True:
        await asyncio.sleep(STATS_INTERVAL)
        proxy.log_stats()


async def run(settings: Settings) -> int:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    failure: list[str] = []

    def fatal(reason: str) -> None:
        _LOGGER.error("%s; exiting", reason)
        failure.append(reason)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    backend = BlueZBackend(settings.adapter, fatal)
    mac = await _wait_for_adapter(backend, settings.adapter, stop)
    if mac is None:
        return 0
    identity = DeviceIdentity(
        name=settings.name,
        friendly_name=settings.friendly_name,
        mac_address=mac,
        suggested_area=settings.area,
    )
    proxy = BluetoothProxy(
        backend, settings.max_connections, configured_mode=settings.scan_mode
    )
    server = ApiServer(identity, settings.api_key, proxy)
    port = await server.start("0.0.0.0", settings.port)
    await backend.start(settings.scan_mode)
    address = settings.address or default_address()
    announcer = Announcer(identity, address, port)
    await announcer.start()
    stats = loop.create_task(_log_stats(proxy))
    _LOGGER.info(
        "aivi-ble-proxy %s: %s (%s, %s) on %s:%d, %d connection slots",
        __version__,
        identity.name,
        settings.adapter,
        mac,
        address,
        port,
        settings.max_connections,
    )
    try:
        await stop.wait()
    finally:
        stats.cancel()
        await announcer.close()
        await server.close()
        await proxy.close()
        await backend.close()
    return 1 if failure else 0


def main() -> None:
    try:
        settings = from_env(os.environ)
    except ConfigError as err:
        print(f"ble-proxy: {err}", file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sys.exit(asyncio.run(run(settings)))


if __name__ == "__main__":
    main()
