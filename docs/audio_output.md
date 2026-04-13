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

When the voice pipeline produces a spoken response, the audio flows through
several stages. Piper generates the speech audio and makes it available through
the Wyoming TTS protocol on port 10200. Home Assistant receives the audio from
the Wyoming integration and routes it to the configured media_player entity
for the relevant zone. The Bluesound Pulse Flex then streams and plays the
audio over its built-in speaker.

The routing is configured in the Home Assistant voice pipeline settings. When
setting up the pipeline, select the Bluesound media_player entity as the audio
output target. This ensures that TTS responses from the voice assistant are
played on the Pulse Flex rather than through the satellite hardware.

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
