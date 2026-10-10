# Audio output

This document describes how synthesized speech from the TTS pipeline reaches
the user through a Bluesound Pulse Flex speaker, controlled via the BluOS
Custom Integration API through Home Assistant.

## Bluesound Pulse Flex

The Pulse Flex is a compact wireless speaker from Bluesound that supports the
BluOS ecosystem. It connects to the local network over Wi-Fi or Ethernet and
exposes a REST-based control interface through the BluOS Custom Integration
API. The full API specification is available at
https://content-bluesound-com.s3.amazonaws.com/uploads/BluOS-Custom-Integration-API_v1.7.pdf

Home Assistant communicates with the speaker through its built-in media_player
integration for Bluesound devices. Once the speaker is discovered or manually
added in Home Assistant, it appears as a media_player entity that can receive
TTS audio, adjust volume, and manage playback state.

## TTS routing

Piper generates the spoken answer, and Home Assistant serves it from its TTS
proxy. The AIVI satellite reports the answer's URL in the event
`esphome.aivi_tts_uri`; the satellite's select `Audioausgabe` decides where
it plays:

- **Over the network** (the room speaker's name, the default): the automation
  `config/homeassistant/aivi_tts_router.yaml` plays the answer on the room's
  Pulse Flex. The satellite's XVF3800 does not know what the speaker plays, so
  it has no echo reference.
- **Through the jack** (`Klinke`): the satellite plays the answer itself. Its
  3.5 mm jack is cabled to the Pulse Flex's analog input, and
  `config/homeassistant/aivi_jack_output.yaml` switches the speaker to that
  input. The XVF3800 takes everything the satellite plays as the echo
  canceller's reference.

The steps are in `docs/satellite_flashing.md`, sections 6 and 7.

## Satellite jack route

The signal path is ESP32 (I2S, GPIO44) → XVF3800 → TLV320AIC3104 codec →
3.5 mm jack → Pulse Flex analog input. The XVF3800 taps the reference before
the codec, so the satellite's volume, set in the codec, does not change the
reference level.

The Pulse Flex's analog input delays by at least 50 ms (A/V mode on; BluOS
offers no shorter setting), and the XVF3800 can delay its reference by at most
16 ms (`AUDIO_MGR_SYS_DELAY`, 256 samples). About 40 ms of the echo
canceller's 192 ms filter therefore carry no echo, and roughly 150 ms remain
for the room's reverberation. The satellite's self test measures the delay,
the reverberation time and the echo attenuation and sets the delay and the
reference gain.

BluOS has no Auto Sense for analog inputs, so Home Assistant selects the
input. Selecting it starts a continuous stream at once, signal or not. The
input has no gain, automatic level or compressor, so the echo path stays
linear. With `Amplifier Standby` on, the Pulse Flex sleeps its amplifier
after 15 minutes without playback and needs 5-15 seconds to wake; switch it
off for a speaker on the jack.

While the jack route is active, the speaker keeps a fixed volume, because the
echo canceller needs a constant echo path; the loudness is set on the
satellite's media player `Audioausgang`. Home Assistant can play any media on
that player, and the speaker switches to its analog input when it starts.
Music that the speaker plays on its own (Spotify Connect, BluOS presets)
bypasses the satellite and stays outside the echo reference.

## Volume and zone configuration

Volume can be controlled through the Home Assistant media_player interface,
either via automations, the dashboard, or voice commands processed by the LLM
itself. The BluOS API also supports grouping multiple Bluesound speakers into
zones for synchronized playback, though the typical deployment uses a single
Pulse Flex per room paired with one satellite.

If multiple rooms each have their own satellite and Pulse Flex, the voice
pipeline in Home Assistant can be configured per area so that responses are
played back on the speaker in the same room where the wake word was detected.
This requires assigning each satellite and its corresponding Bluesound speaker
to the same Home Assistant area.

## Troubleshooting

If TTS audio does not reach the speaker, verify that the Bluesound device
appears as a healthy media_player entity in Home Assistant. Test playback
independently by sending a TTS notification through the HA developer tools
before involving the full voice pipeline.

If there is noticeable latency between the voice command and the spoken
response, the bottleneck is most likely the LLM inference step rather than the
audio routing. Check vLLM latency metrics through the llm_proxy capture data
or the evaluation scripts before investigating the audio chain.

If answers through the jack stay silent, check that `Analog Input` is in the
speaker's source list (BluOS `Hide source` off) and look for warnings of the
logger `aivi_jack_output` in the Home Assistant log. If only the start of an
answer is missing, the speaker's amplifier was in standby: switch BluOS
`Amplifier Standby` off. Network answers still play nothing while the
speaker is grouped with another room; the jack route takes the speaker out
of its group.
