# ha-voice-agent

A fully local, GPU-accelerated voice agent stack for Home Assistant. Every
component runs on-premise without cloud dependencies, from wake-word detection
on the satellite hardware through speech recognition, language model inference,
tool execution, speech synthesis, and audio playback.

## Architecture

The voice pipeline spans two physical layers. Satellite devices handle wake-word
detection and far-field audio capture, then stream audio over the Wyoming
protocol to Home Assistant. The inference server receives the transcribed text,
runs it through the LLM with tool-calling support, and returns synthesized
speech to the target speaker.

```text
Satellite (ESP32S3 + ReSpeaker XVF3800)
  microWakeWord trigger -> Wyoming audio stream -> Home Assistant
                                                        |
                                                        v
                                        STT (faster-whisper, Wyoming)
                                                        |
                                                        v
                                        LLM (Qwen3-8B via vLLM)
                                                        |
                                                        v
                                        Tool execution (HA MCP)
                                                        |
                                                        v
                                        TTS (Piper, Wyoming)
                                                        |
                                                        v
                                        Audio output (Bluesound Pulse Flex)
```

The satellite listens continuously for the configured wake word using
microWakeWord running directly on the ESP32S3. Once triggered, the ReSpeaker
XVF3800 voice processor captures far-field audio with its multi-microphone
array, applies noise suppression and beamforming, and forwards the cleaned
audio stream to Home Assistant via the Wyoming protocol. From there, the
inference server takes over: faster-whisper transcribes the audio, vLLM
processes the text with optional tool calls against Home Assistant entities,
and Piper generates the spoken response. The final audio is played back through
a Bluesound Pulse Flex speaker using the BluOS Custom Integration API.

## Hardware stack

The inference server and training hardware are deliberately separated.
Inference runs on a single consumer GPU with a strict 24 GB VRAM budget,
while training happens on dedicated datacenter-class accelerators.

**Inference server:** NVIDIA RTX 3090 Ti with 24 GB VRAM, 128 GB system
memory. This machine hosts all Docker services (vLLM, faster-whisper, Piper,
llm_proxy) and serves as the runtime for day-to-day voice assistant operation.

**Training rig:** 2x NVIDIA A100 PCIe. Used exclusively for LoRA fine-tuning
and STT adaptation. Training data is captured on the inference server during
normal operation, transferred to the training machine, and the resulting
adapters are deployed back to the inference server.

**Satellites:** ReSpeaker with XVF3800 voice processor for far-field audio
capture, paired with an ESP32S3 running ESPHome. The ESP32S3 handles on-device
wake-word detection via microWakeWord and streams audio to Home Assistant over
the Wyoming protocol after trigger.

**Audio output:** Bluesound Pulse Flex, controlled through the BluOS Custom
Integration API via Home Assistant's media_player entity.

## VRAM budget

The entire runtime stack must fit within the 24 GB available on the RTX 3090
Ti. The allocation below reflects Qwen3-8B served in FP16 with an 8192-token
context window and Whisper large-v3 running on the same GPU.

| Component | VRAM | Notes |
|---|---:|---|
| Qwen3-8B (FP16) | ~16 GB | 8B parameters at 2 bytes each |
| KV cache (8192 context) | ~2-3 GB | single sequence, prefix caching enabled |
| Whisper large-v3 (float16) | ~3 GB | CTranslate2 runtime on GPU |
| Piper TTS | ~0 GB | runs on CPU by default |
| CUDA overhead | ~1-2 GB | driver context and runtime buffers |

The `gpu_memory_utilization` is set to 0.88 in production and 0.90 in
development to leave sufficient headroom for the STT model and system overhead.

## Repository layout

```text
compose/
  prod/docker-compose.yml          production stack (vLLM, STT, TTS)
  dev/docker-compose.yml           development stack (adds llm_proxy)
  dev/docker-compose.override.yml  debug capture and packet inspection
  dev/vllm.config.yaml             vLLM engine configuration for dev
services/
  llm_proxy/                       FastAPI proxy with tool-call repair
  stt_faster_whisper/              custom Whisper container build
scripts/                           model download, health checks, smoke tests
training/
  eval/                            evaluation scripts (validity, latency, compliance)
  llm/                             LoRA fine-tuning and dataset preparation
  stt/                             STT dataset preparation and evaluation
docs/                              setup guides and pipeline documentation
dev/
  models/{llm,tts}                 local model storage for development
  cache/{hf,vllm}                  HuggingFace and vLLM cache directories
  logs/                            service logs
  datasets/{llm,stt,meta}          captured training data
models/
  stt/large-v3-ct2                 Whisper CTranslate2 model
```

## Quick start

### Prerequisites

The inference server needs Docker with the Compose plugin and the NVIDIA
Container Toolkit for GPU passthrough. Python 3.12 or newer is required for
the training and evaluation scripts. Verify that the GPU is accessible inside
containers before proceeding:

```bash
scripts/check_gpu_docker.sh
```

### Download models

The stack operates offline-first, so all model weights must be downloaded
before the first start. Each script places files in the expected directory
structure automatically.

```bash
scripts/download_llm.sh                                  # Qwen3-8B by default
scripts/download_stt.sh Systran/faster-whisper-large-v3   # CTranslate2 format
scripts/download_tts.sh en_US-lessac-medium               # Piper ONNX voice
```

### Start the development stack

The development stack includes the llm_proxy service, which sits between Home
Assistant and vLLM to validate tool-call JSON, attempt one-shot repair on
malformed responses, and capture every interaction as JSONL for later training.

```bash
docker compose -f compose/dev/docker-compose.yml up -d
```

For additional debug logging and network packet capture on the STT channel:

```bash
docker compose \
  -f compose/dev/docker-compose.yml \
  -f compose/dev/docker-compose.override.yml \
  up -d
```

### Start the production stack

Production runs without the proxy layer. Home Assistant connects directly to
vLLM, and logging is kept to a minimum.

```bash
docker compose -f compose/prod/docker-compose.yml up -d
```

### Verify the stack

```bash
scripts/healthcheck_stack.sh
scripts/smoke_test_llm_proxy.sh   # dev stack only
```

## Service ports

| Service | Port | Protocol |
|---|---:|---|
| vLLM (OpenAI-compatible API) | 10100 | HTTP |
| Wyoming Faster-Whisper (STT) | 10300 | Wyoming/TCP |
| Wyoming Piper (TTS) | 10200 | Wyoming/TCP |
| llm_proxy (dev only) | 18001 | HTTP |

## llm_proxy

The proxy service in `services/llm_proxy` is the central piece of the
development workflow. It exposes the same OpenAI-compatible endpoints as vLLM
(`/health`, `/v1/models`, `/v1/chat/completions`) while adding tool-call JSON
validation with a single retry repair mechanism. Every request-response pair is
written to JSONL files under `dev/datasets/llm/`, capturing timestamps,
latency, token counts, tool-call validity, and whether a repair attempt was
needed. This captured data feeds directly into the LoRA fine-tuning pipeline.

## Training

Training runs on dedicated A100 hardware, not on the inference server. The
workflow starts with data captured by the llm_proxy during normal voice
assistant operation, which is then prepared into the SFT format and used for
LoRA fine-tuning. Detailed instructions are in `docs/training_pipeline.md`.

```bash
python training/llm/prepare_dataset.py \
  --input-dir dev/datasets/llm \
  --output dev/datasets/meta/llm_sft.jsonl

python training/llm/train_lora.py \
  --dataset dev/datasets/meta/llm_sft.jsonl \
  --output-dir dev/models/llm/adapters/qwen_tooling \
  --config training/llm/lora_config.json \
  --dry-run
```

Evaluation scripts measure tool-call validity rates, two-step device control
compliance, and inference latency:

```bash
python training/eval/eval_tool_call_validity.py --dataset dev/datasets/llm
python training/eval/eval_two_step_compliance.py --dataset dev/datasets/llm
python training/eval/eval_latency_tokens.py --dataset dev/datasets/llm
```

## Documentation

Further setup and operational guides live in the `docs/` directory:

- `docs/home_assistant_setup.md` covers Wyoming integration, LLM endpoint
  configuration, and system prompt guidance.
- `docs/mcp_assist_setup.md` explains tool-calling with MCP clients through
  the llm_proxy.
- `docs/training_pipeline.md` details dataset schemas, the capture-to-training
  workflow, and evaluation metrics.
- `docs/satellite_flashing.md` is the per-room runbook for flashing a
  satellite board (XVF3800 firmware, ESP32, verification).

## Makefile helpers

```bash
make dev-up           # start development stack
make dev-up-debug     # start with debug capture overlay
make dev-down         # stop development stack
make prod-up          # start production stack
make prod-down        # stop production stack
make test             # run compileall and basic checks
make sat-xvf3800      # one-time XVF3800 I2S firmware flash over USB DFU
make sat-chip DEVICE=/dev/cu.usbmodemXXXX           # identify the ESP32
make sat-flash SAT=living-room DEVICE=<port-or-ip>   # build and flash
make sat-logs SAT=living-room DEVICE=<port-or-ip>    # follow the log
```

## Branch workflow

All development happens on topic branches created from `dev`. The `dev` branch
serves as the integration branch and is treated as protected. Feature work uses
`feat/*` prefixes, bug fixes use `fix/*`, and housekeeping uses `chore/*`.
Changes are merged back into `dev` through pull requests.

## Licensing

The project is released under the Apache-2.0 license. Third-party component
licenses and pinned versions are documented in `THIRD_PARTY_NOTICES.md`.
