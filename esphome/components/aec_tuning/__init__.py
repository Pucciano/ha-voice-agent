"""Echo canceller settings and self test for the XVF3800 (issue #17).

AUDIO_MGR_SYS_DELAY and AUDIO_MGR_REF_GAIN are kept in flash and written at
boot. The self test plays test signals through a mixer input speaker, reads
the XVF3800 output channels from the microphone, measures the echo path and
sets both values; see analysis.h. YAML reads the results through the
component's methods; see esphome/packages/aivi-aec.yaml.
"""

from esphome import automation
import esphome.codegen as cg
from esphome.components import microphone, ota, speaker
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_MICROPHONE, CONF_SPEAKER

AUTO_LOAD = ["audio", "ring_buffer"]
DEPENDENCIES = ["microphone", "speaker", "respeaker_xvf3800"]

CONF_ON_STATUS = "on_status"
CONF_RESPEAKER_ID = "respeaker_id"

respeaker_xvf3800_ns = cg.esphome_ns.namespace("respeaker_xvf3800")
RespeakerXVF3800 = respeaker_xvf3800_ns.class_("RespeakerXVF3800")

aec_tuning_ns = cg.esphome_ns.namespace("aec_tuning")
AecTuning = aec_tuning_ns.class_("AecTuning", cg.Component)

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(AecTuning),
            cv.Required(CONF_RESPEAKER_ID): cv.use_id(RespeakerXVF3800),
            # A mixer input: the test signal plays next to everything else.
            cv.Required(CONF_SPEAKER): cv.use_id(speaker.Speaker),
            # Left and right XVF3800 output channels.
            cv.Optional(
                CONF_MICROPHONE, default={}
            ): microphone.microphone_source_schema(
                min_bits_per_sample=16,
                max_bits_per_sample=16,
                min_channels=2,
                max_channels=2,
            ),
            cv.Optional(CONF_ON_STATUS): automation.validate_automation(
                single=True
            ),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.only_on_esp32,
)

FINAL_VALIDATE_SCHEMA = cv.Schema(
    {
        cv.Optional(
            CONF_MICROPHONE
        ): microphone.final_validate_microphone_source_schema(
            "aec_tuning", sample_rate=16000
        ),
    },
    extra=cv.ALLOW_EXTRA,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    respeaker = await cg.get_variable(config[CONF_RESPEAKER_ID])
    cg.add(var.set_respeaker(respeaker))
    spkr = await cg.get_variable(config[CONF_SPEAKER])
    cg.add(var.set_speaker(spkr))
    mic_source = await microphone.microphone_source_to_code(
        config[CONF_MICROPHONE]
    )
    cg.add(var.set_microphone_source(mic_source))
    ota.request_ota_state_listeners()

    if CONF_ON_STATUS in config:
        await automation.build_automation(
            var.get_status_trigger(),
            [(cg.std_string, "status")],
            config[CONF_ON_STATUS],
        )
