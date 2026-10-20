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
after 15 minutes without playback; Bluesound support quotes 5-15 seconds to
wake. The selected analog input counts as playback, even when silent: after
30 minutes of silence the living room speaker played at once.

A stopped speaker starts the input quickly. The satellite's
`Lautsprecher-Anlauf messen` timed the living room speaker from the moment
Home Assistant was told to switch it: the first sound came after 1.1-1.2
seconds and full level (a fade-in of about 150 ms) after 1.2-1.4 seconds,
the same after 2 and after 27 minutes of standstill. Already playing the
input, it sounded after 0.16 seconds. Answers start 3.4-5.1 seconds after
the wake word, so a speaker stopped for that long loses nothing of an
answer. Whether its amplifier had gone to standby after 27 minutes is not
known; BluOS does not report it.

So `config/homeassistant/aivi_jack_release.yaml` stops the analog input once
the jack has been idle for 10 minutes: no switch to the input, nothing played
on `Audioausgang`, no wake word and no self test. The speaker can then sleep
its amplifier, and the next wake word selects the input again. It stops only
the analog input, never a network stream: with the satellite's
`Audioausgabe` on the room speaker, answers play over the network and replace
the input anyway.

While the jack route is active, the speaker keeps a fixed volume, because the
echo canceller needs a constant echo path; the loudness is set on the
satellite's media player `Audioausgang`. Home Assistant can play any media on
that player, Music Assistant reaches it through Sendspin (and the Spotify app
through Music Assistant's Spotify Connect plugin), and the speaker switches
to its analog input when it starts.
Music that the speaker plays on its own (Spotify Connect, BluOS presets)
bypasses the satellite and stays outside the echo reference.

Pause and play have to reach that source. Pausing the speaker (its button,
the BluOS app, the wall display, a voice command for the room) only silences
its analog input; the music plays on, and the next wake word restarts the
input for the answer. So `config/homeassistant/aivi_jack_playback.yaml`
pauses `Audioausgang` when the speaker stops playing its analog input; over
Sendspin that pauses Music Assistant, and Music Assistant pauses Spotify.
When a person starts the paused input again, `Audioausgang` resumes. A
restart for an answer, a timer gong or the self test resumes nothing.

Timer alarms always take the jack, whatever `Audioausgabe` says: the gong and
the spoken "Der Timer … ist abgelaufen" play on `Audioausgang`, so a finished
timer also switches a speaker that plays network answers to its analog input
(`docs/satellite_flashing.md`, section 8).

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
answer is missing, time the speaker with `Lautsprecher-Anlauf messen` after
it sat stopped as long as before the failure; if it starts later than about
3 seconds, switch BluOS `Amplifier Standby` off. Network answers still play
nothing while the speaker is grouped with another room; the jack route takes
the speaker out of its group.

Offline playback-guard regression tests:
`uvx --python 3.12 --with pyyaml==6.0.3 --with jinja2==3.1.6 --from pytest==8.3.5 pytest tests/homeassistant`
The tests evaluate the shipped templates with controlled timestamps and
contexts; the speaker's real state reporting still needs a live check.

Live verification on 2026-10-20: deployed `aivi_jack_playback` through the HA
config API, with a backup of the previous config. A quiet local Piper test
recording on the ordinary media pipeline was paused from the real BluOS
speaker; the satellite paused too. Manual speaker play resumed the satellite
0.77 seconds after the output automation ran. A synthetic satellite wake
event restarted only the speaker input and left the satellite paused.
HA traces confirmed both the manual-resume action and the automatic-resume
guard, without template errors. Both players were stopped and the original
satellite volume restored afterwards. This does not verify Spotify/Sendspin
or a physical wake phrase/button press.
