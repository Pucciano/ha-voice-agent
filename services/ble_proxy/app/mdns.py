"""mDNS announcement as an ESPHome device, so Home Assistant discovers it.

Home Assistant's ESPHome integration reacts to _esphomelib._tcp services. It
needs the TXT key "mac" (lower case, no colons) and reads "api_encryption"
to ask for the key right away. Only the address of the interface towards the
network is announced, never the Docker bridges.
"""

from __future__ import annotations

import socket

from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from . import __version__
from .api import ESPHOME_VERSION, PROJECT_NAME, DeviceIdentity
from .noise_transport import PROTOCOL_NAME

SERVICE_TYPE = "_esphomelib._tcp.local."


def default_address() -> str:
    """Source address of the default route; sends nothing."""

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, only a route lookup
        return probe.getsockname()[0]


def txt_properties(identity: DeviceIdentity) -> dict[str, str]:
    return {
        "friendly_name": identity.friendly_name,
        "version": ESPHOME_VERSION,
        "mac": identity.mac_hex,
        "platform": "linux",
        "board": "aivi-ble-proxy",
        "network": "ethernet",
        "api_encryption": PROTOCOL_NAME.decode(),
        "project_name": PROJECT_NAME,
        "project_version": __version__,
    }


class Announcer:
    def __init__(self, identity: DeviceIdentity, address: str, port: int):
        self._zeroconf = AsyncZeroconf(
            interfaces=[address], ip_version=IPVersion.V4Only
        )
        self._info = AsyncServiceInfo(
            SERVICE_TYPE,
            f"{identity.name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(address)],
            port=port,
            properties=txt_properties(identity),
            server=f"{identity.name}.local.",
        )

    async def start(self) -> None:
        await self._zeroconf.async_register_service(self._info)

    async def close(self) -> None:
        await self._zeroconf.async_unregister_service(self._info)
        await self._zeroconf.async_close()
