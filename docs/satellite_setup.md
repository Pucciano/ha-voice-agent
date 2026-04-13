# Satellite setup

This guide covers the hardware assembly and firmware configuration for the
voice satellite devices used with ha-voice-agent. Each satellite combines a
ReSpeaker board with the XVF3800 voice processor and an ESP32S3 running
ESPHome.

## Hardware overview

The ReSpeaker with XVF3800 is a far-field voice capture board designed for
smart speaker and voice assistant applications. The XVF3800 DSP handles
multi-microphone array processing including beamforming, acoustic echo
cancellation, and noise suppression. It outputs a cleaned single-channel audio
stream that is significantly better suited for speech recognition than raw
microphone input, especially in noisy rooms or at a distance.

The ESP32S3 serves as the host microcontroller. It receives the processed audio
from the XVF3800 over I2S, runs wake-word detection locally, and handles
network communication with Home Assistant. The ESP32S3 was chosen for its
dual-core processor, built-in Wi-Fi, and sufficient memory to run small neural
network models for on-device wake-word recognition.

For detailed hardware specifications and pin configuration, refer to the
ReSpeaker XVF3800 documentation at
https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/

## ESPHome firmware

The ESP32S3 runs ESPHome with the voice_assistant component. The firmware
configuration needs to include the I2S microphone input from the XVF3800, the
microWakeWord component for on-device wake-word detection, and the Wyoming
streaming output to Home Assistant.

A minimal ESPHome YAML configuration for the satellite covers these areas:

The I2S audio source connects to the XVF3800 output pins. The exact pin
assignment depends on the specific ReSpeaker board revision, so consult the
board documentation for the correct GPIO mapping.

The microWakeWord component loads a wake-word model onto the ESP32S3 and
continuously evaluates incoming audio against it. When the model confidence
exceeds the configured threshold, the satellite transitions from idle listening
to active streaming mode. Several pre-trained wake-word models are available
through the ESPHome ecosystem, and the choice of wake word and sensitivity
threshold should be tuned based on the deployment environment to balance
responsiveness against false activation rates.

Once triggered, the voice_assistant component opens a Wyoming protocol
connection to the Home Assistant instance and streams the captured audio until
the server signals that the utterance is complete. The Wyoming connection
target is the Home Assistant host address, and the satellite registers itself
as a voice pipeline device in the HA UI.

## Home Assistant integration

After flashing the firmware and powering on the satellite, Home Assistant
should discover it automatically through the ESPHome integration. The satellite
appears as a device with voice assistant capabilities. To wire it into the
voice pipeline, navigate to Settings, Devices and Services, and open the
ESPHome device. Assign the satellite to the voice pipeline that uses the local
STT (Wyoming Faster-Whisper on port 10300), the LLM endpoint, and the local
TTS (Wyoming Piper on port 10200).

The audio output for TTS responses is routed through the Bluesound Pulse Flex
media_player entity rather than through the satellite hardware itself. This
separation allows high-quality audio playback on a dedicated speaker while
keeping the satellite focused on input capture. See `docs/audio_output.md` for
the Bluesound configuration.

## Troubleshooting

If the satellite does not appear in Home Assistant after flashing, verify that
the ESP32S3 has connected to the correct Wi-Fi network and that mDNS traffic
is not blocked between the satellite and the HA host.

If wake-word detection triggers too frequently, lower the sensitivity threshold
in the microWakeWord configuration. If it fails to trigger reliably, check that
the I2S pin mapping matches the XVF3800 output and that the audio gain levels
are appropriate. The XVF3800 output should already be normalized, but board
revisions may differ.

If audio streams to HA but STT produces empty or garbled transcriptions,
confirm that the sample rate and bit depth in the ESPHome I2S configuration
match what faster-whisper expects. The Wyoming protocol supports 16kHz 16-bit
mono PCM, which is the standard for speech recognition.
