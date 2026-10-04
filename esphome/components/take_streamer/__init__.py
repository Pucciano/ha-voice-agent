"""Stream wake word training takes to the Jetson recorder (issue #13).

The component reads the same microphone source as microWakeWord and sends it
over TCP to services/wake_word_recorder, framed as Wyoming events. Home
Assistant stays connected the whole time. YAML starts and stops takes through
the public methods of TakeStreamer; see
esphome/packages/aivi-wake-word-recording.yaml.
"""

import re

from esphome import automation
import esphome.codegen as cg
from esphome.components import microphone, ota, socket
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_MICROPHONE, CONF_PORT

AUTO_LOAD = ["audio", "ring_buffer", "socket"]
DEPENDENCIES = ["microphone", "network"]

CONF_BUFFER_DURATION = "buffer_duration"
CONF_HOST = "host"
CONF_ON_STATUS = "on_status"
CONF_SATELLITE_ID = "satellite_id"
CONF_TOKEN = "token"

take_streamer_ns = cg.esphome_ns.namespace("take_streamer")
TakeStreamer = take_streamer_ns.class_("TakeStreamer", cg.Component)


def _characters(pattern: str, maximum: int, minimum: int = 1):
    """A string of characters from `pattern`, e.g. "a-z0-9_"."""

    allowed = re.compile(f"[{pattern}]{{{minimum},{maximum}}}")

    def validate(value):
        value = cv.string_strict(value)
        if not allowed.fullmatch(value):
            raise cv.Invalid(
                f"use {minimum} to {maximum} of the characters {pattern}"
            )
        return value

    return validate


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(TakeStreamer),
            cv.Optional(
                CONF_MICROPHONE, default={}
            ): microphone.microphone_source_schema(
                min_bits_per_sample=16,
                max_bits_per_sample=16,
                min_channels=1,
                max_channels=1,
            ),
            # An address, not a name: a DNS lookup would block the loop.
            cv.Required(CONF_HOST): cv.ipv4address,
            cv.Optional(CONF_PORT, default=10800): cv.port,
            # Sent inside the take-start event, so only JSON-safe characters.
            cv.Required(CONF_TOKEN): _characters("A-Za-z0-9_-", 128, 16),
            cv.Required(CONF_SATELLITE_ID): _characters("a-z0-9_", 32),
            # Audio waiting for the network; a longer stall ends the take.
            cv.Optional(CONF_BUFFER_DURATION, default="5s"): cv.All(
                cv.positive_time_period_milliseconds,
                cv.Range(
                    min=cv.TimePeriod(seconds=1),
                    max=cv.TimePeriod(seconds=30),
                ),
            ),
            cv.Optional(CONF_ON_STATUS): automation.validate_automation(
                single=True
            ),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.only_on_esp32,
    socket.consume_sockets(1, "take_streamer"),
)

FINAL_VALIDATE_SCHEMA = cv.Schema(
    {
        cv.Optional(
            CONF_MICROPHONE
        ): microphone.final_validate_microphone_source_schema(
            "take_streamer", sample_rate=16000
        ),
    },
    extra=cv.ALLOW_EXTRA,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    mic_source = await microphone.microphone_source_to_code(
        config[CONF_MICROPHONE]
    )
    cg.add(var.set_microphone_source(mic_source))
    cg.add(var.set_host(str(config[CONF_HOST])))
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_token(config[CONF_TOKEN]))
    cg.add(var.set_satellite_id(config[CONF_SATELLITE_ID]))
    cg.add(
        var.set_buffer_duration(config[CONF_BUFFER_DURATION].total_milliseconds)
    )
    ota.request_ota_state_listeners()

    if CONF_ON_STATUS in config:
        await automation.build_automation(
            var.get_status_trigger(),
            [(cg.std_string, "status")],
            config[CONF_ON_STATUS],
        )
