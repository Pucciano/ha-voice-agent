# THIRD_PARTY_NOTICES

This project uses third-party software and model artifacts.

## Container images

| Component | Image | Digest / Tag | License |
|---|---|---|---|
| vLLM server | `vllm/vllm-openai` | `v0.19.0` | Apache-2.0 |
| Wyoming Faster-Whisper (local build) | `ha-voice-agent/wyoming-faster-whisper:local` | Built from `python:3.12-slim-bookworm` | See upstream dependencies |
| Wyoming Piper | `rhasspy/wyoming-piper` | `sha256:c874e4a04657ae3381332ee5d0c8c70a310dae6722892840f530ac0890b44eb3` | MIT (project-level) |
| Debug network tools | `nicolaka/netshoot` | `latest` | See upstream repository |

## Python dependencies (stt_faster_whisper image)

| Package | Version | License (upstream) |
|---|---|---|
| wyoming-faster-whisper | 3.1.0 | MIT |
| faster-whisper | transitive | MIT |
| ctranslate2 | transitive | MIT |

## Python dependencies (llm_proxy)

| Package | Version | License (upstream) |
|---|---|---|
| fastapi | 0.116.1 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| orjson | 3.11.3 | Apache-2.0 / MIT |
| pydantic | 2.11.9 | MIT |
| python-dotenv | 1.1.1 | BSD-3-Clause |
| uvicorn | 0.35.0 | BSD-3-Clause |

## Training dependencies

| Package | Version | License (upstream) |
|---|---|---|
| transformers | 4.56.1 | Apache-2.0 |
| peft | 0.17.1 | Apache-2.0 |
| datasets | 4.0.0 | Apache-2.0 |
| jsonschema | 4.25.1 | MIT |
| numpy | 2.3.2 | BSD-3-Clause |

## Models

Default runtime model targets:

- LLM: `Qwen/Qwen3-8B`
- STT: `Systran/faster-whisper-large-v3` (mounted at `models/stt/large-v3-ct2`)
- TTS voice: `en_US-lessac-medium`

Model licenses and usage terms are controlled by their upstream publishers.
Users are responsible for compliance with each model's terms.
