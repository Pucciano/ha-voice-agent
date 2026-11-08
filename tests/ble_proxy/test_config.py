"""Settings from the environment and the mDNS record."""

import pytest
from helpers import KEY, KEY_TEXT
from app.api import DeviceIdentity
from app.backend import ScannerMode
from app.config import ConfigError, from_env
from app.mdns import txt_properties


def test_defaults_need_only_the_key() -> None:
    settings = from_env({"BLE_PROXY_API_KEY": KEY_TEXT})
    assert settings.api_key == KEY
    assert settings.name == "aivi-ble-proxy"
    assert settings.adapter == "hci0"
    assert settings.port == 6053
    assert settings.max_connections == 3
    assert settings.scan_mode is ScannerMode.ACTIVE
    assert settings.address is None
    assert KEY_TEXT not in repr(settings)


def test_overrides() -> None:
    settings = from_env(
        {
            "BLE_PROXY_API_KEY": KEY_TEXT,
            "BLE_PROXY_NAME": "aivi-ble-living-room",
            "BLE_PROXY_FRIENDLY_NAME": "Bluetooth Wohnzimmer",
            "BLE_PROXY_AREA": "Wohnzimmer",
            "BLE_PROXY_ADAPTER": "hci1",
            "BLE_PROXY_PORT": "6054",
            "BLE_PROXY_MAX_CONNECTIONS": "2",
            "BLE_PROXY_SCAN_MODE": "passive",
            "BLE_PROXY_ADDRESS": "192.0.2.10",
            "BLE_PROXY_LOG_LEVEL": "debug",
        }
    )
    assert settings.name == "aivi-ble-living-room"
    assert settings.adapter == "hci1"
    assert settings.port == 6054
    assert settings.max_connections == 2
    assert settings.scan_mode is ScannerMode.PASSIVE
    assert settings.address == "192.0.2.10"
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    "changes",
    [
        {"BLE_PROXY_API_KEY": ""},
        {"BLE_PROXY_API_KEY": "c2hvcnQ="},
        {"BLE_PROXY_API_KEY": "not base64 !"},
        {"BLE_PROXY_NAME": "Wohnzimmer"},
        {"BLE_PROXY_NAME": "-leading"},
        {"BLE_PROXY_ADAPTER": "usb0"},
        {"BLE_PROXY_PORT": "0"},
        {"BLE_PROXY_MAX_CONNECTIONS": "many"},
        {"BLE_PROXY_SCAN_MODE": "sometimes"},
        {"BLE_PROXY_LOG_LEVEL": "LOUD"},
    ],
)
def test_bad_values_are_refused(changes) -> None:
    env = {"BLE_PROXY_API_KEY": KEY_TEXT, **changes}
    with pytest.raises(ConfigError) as err:
        from_env(env)
    assert KEY_TEXT not in str(err.value)


def test_mdns_record_has_what_home_assistant_reads() -> None:
    identity = DeviceIdentity(
        name="aivi-ble-living-room",
        friendly_name="Bluetooth Wohnzimmer",
        mac_address="8C:88:4B:45:5F:60",
    )
    txt = txt_properties(identity)
    assert txt["mac"] == "8c884b455f60"
    assert txt["friendly_name"] == "Bluetooth Wohnzimmer"
    assert txt["api_encryption"] == "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
