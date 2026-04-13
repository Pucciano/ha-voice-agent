# Hardware stack

This document describes the physical hardware that makes up the ha-voice-agent
deployment, covering the inference server, training infrastructure, satellite
devices, and audio output.

## Inference server

The runtime stack runs on a single machine equipped with an NVIDIA RTX 3090 Ti
providing 24 GB of VRAM and 128 GB of system memory. All Docker services
(vLLM, faster-whisper, Piper, and the llm_proxy in development mode) share
this GPU. The 24 GB VRAM budget is the hard constraint that determines model
choice, context window size, and whether the STT model runs on GPU or CPU.

The VRAM allocation below reflects the default configuration with Qwen3-8B
served in FP16, an 8192-token context window, and Whisper large-v3 on GPU.

| Component | VRAM | Notes |
|---|---:|---|
| Qwen3-8B (FP16) | ~16 GB | 8B parameters at 2 bytes each |
| KV cache (8192 context) | ~2-3 GB | single sequence, prefix caching enabled |
| Whisper large-v3 (float16) | ~3 GB | CTranslate2 runtime |
| Piper TTS | ~0 GB | CPU by default |
| CUDA overhead | ~1-2 GB | driver context and runtime buffers |

If the combined allocation proves too tight in practice, the first lever is
switching Whisper to CPU by setting `STT_DEVICE=cpu` in the environment. This
frees roughly 3 GB of VRAM at the cost of slightly higher transcription
latency. The second option is reducing the context window to 4096 tokens.

Storage on the inference server follows a predictable layout. Production model
weights live under `/opt/ha-voice/models` with separate subdirectories for
`llm`, `stt`, and `tts`. The HuggingFace and vLLM caches reside under
`/opt/ha-voice/cache`. During development, the repository-local `dev/` tree
mirrors this structure for convenience.

## Training infrastructure

Training is handled by a separate machine with two NVIDIA A100 PCIe
accelerators. This hardware is used exclusively for LoRA fine-tuning of the
LLM and optional STT adaptation work. The training rig does not participate in
the runtime voice pipeline at all.

The typical workflow involves three steps. First, training data accumulates on
the inference server through the llm_proxy capture pipeline during normal voice
assistant operation. Second, the captured JSONL files are transferred to the
training machine, prepared into SFT format, and used to train a LoRA adapter.
Third, the resulting adapter weights are copied back to the inference server
and placed in the model directory so that vLLM can load them alongside the base
model. Detailed instructions for each step are in `docs/training_pipeline.md`
and `docs/training_infrastructure.md`.

## Satellite devices

Each room that needs voice input is equipped with a satellite consisting of two
components: a ReSpeaker board with the XVF3800 voice processor and an ESP32S3
microcontroller running ESPHome.

The XVF3800 is a dedicated DSP that handles far-field audio capture from a
multi-microphone array. It performs acoustic echo cancellation, noise
suppression, and beamforming in hardware, delivering a cleaned single-channel
audio stream to the ESP32S3. This offloads all signal processing from the
microcontroller and produces significantly better input quality than a raw
microphone would.

The ESP32S3 runs an ESPHome firmware image with two key responsibilities.
First, it performs on-device wake-word detection using the microWakeWord
framework, which runs a small neural network locally on the microcontroller
without any cloud or server dependency. Second, once the wake word is detected,
it opens a Wyoming protocol stream to Home Assistant and forwards the audio
from the XVF3800 until the utterance is complete. This means the inference
server only receives audio after the wake word has been positively identified,
keeping network traffic and server load to a minimum.

Configuration of the satellite firmware, wake-word selection, and sensitivity
tuning is covered in `docs/satellite_setup.md`.

## Audio output

Synthesized speech is played back through a Bluesound Pulse Flex speaker. Home
Assistant controls the speaker via its media_player integration, which
communicates with the Bluesound hardware using the BluOS Custom Integration
API. When the TTS pipeline produces an audio response, Home Assistant sends it
to the configured media_player entity, and the Pulse Flex plays it back over
the local network. Volume control and zone assignment are handled through the
same media_player interface. Further details on the BluOS API integration are
in `docs/audio_output.md`.
