# ha-voice-agent

Offline-first voice-agent stack for Home Assistant with:

- local LLM serving (vLLM, OpenAI-compatible API)
- local STT (Wyoming Faster-Whisper / CTranslate2)
- local TTS (Wyoming Piper)
- dev capture pipeline for prompts/responses/tool-calls
- training/evaluation scaffolding for iterative improvement

Target hardware budget: **24 GB VRAM** (e.g. RTX 3090 Ti).

## Architecture

Runtime data flow:

1. User audio -> STT (`wyoming-whisper` using faster-whisper)
2. Text request -> LLM (`vllm` directly, or `llm-proxy` in dev)
3. Tool-calling output -> Home Assistant / MCP tool layer
4. Response text -> TTS (`wyoming-piper`)

Compose split:

- **Production:** `compose/prod/docker-compose.yml`
- **Development:** `compose/dev/docker-compose.yml`
- **Dev debug capture override:**
  `compose/dev/docker-compose.override.yml`

## Repository layout

```text
compose/
  prod/docker-compose.yml
  dev/docker-compose.yml
  dev/docker-compose.override.yml
services/
  llm_proxy/
  stt_faster_whisper/
scripts/
training/
  eval/
  llm/
  stt/
docs/
dev/
  models/{llm,tts}
  cache/{hf,vllm}
  logs/
  datasets/{llm,stt,meta}
models/
  stt/large-v3-ct2
```

## VRAM budget (baseline)

| Component | Target VRAM |
|---|---:|
| vLLM + 7B instruct model | 14-18 GB |
| KV cache | 2-5 GB |
| Whisper STT (GPU optional) | 0-3 GB |
| Piper TTS (CPU default) | ~0 GB |
| Runtime overhead | 1-3 GB |

## Quick start

### 1) Prerequisites

- Docker + Docker Compose plugin
- NVIDIA driver + NVIDIA Container Toolkit (for GPU path)
- Python 3.12+ (scripts/eval helpers)

Check GPU container runtime:

```bash
scripts/check_gpu_docker.sh
```

### 2) Download models (offline-first local paths)

LLM:

```bash
scripts/download_llm.sh Qwen/Qwen2.5-7B-Instruct
```

STT (CT2 model in `models/stt/large-v3-ct2`):

```bash
scripts/download_stt.sh Systran/faster-whisper-large-v3
```

TTS:

```bash
scripts/download_tts.sh en_US-lessac-medium
```

### 3) Start development stack

```bash
docker compose -f compose/dev/docker-compose.yml up -d
```

Optional debug/packet capture mode:

```bash
docker compose \
  -f compose/dev/docker-compose.yml \
  -f compose/dev/docker-compose.override.yml \
  up -d
```

### 4) Start production stack

```bash
docker compose -f compose/prod/docker-compose.yml up -d
```

## Health and smoke tests

Health checks:

```bash
scripts/healthcheck_stack.sh
```

LLM proxy smoke tests:

```bash
scripts/smoke_test_llm_proxy.sh
```

## llm_proxy behavior (dev)

Service: `services/llm_proxy`

Endpoints:

- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions`

Features:

- pass-through to vLLM
- tool-call JSON argument validation
- one-shot repair retry when JSON is invalid
- JSONL capture into `dev/datasets/llm/`

## Training pipeline

Detailed instructions: `docs/training_pipeline.md`

Common commands:

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

Evaluation scripts:

```bash
python training/eval/eval_tool_call_validity.py --dataset dev/datasets/llm
python training/eval/eval_two_step_compliance.py --dataset dev/datasets/llm
python training/eval/eval_latency_tokens.py --dataset dev/datasets/llm
```

## Home Assistant and MCP docs

- Home Assistant setup: `docs/home_assistant_setup.md`
- MCP/tool-calling setup: `docs/mcp_assist_setup.md`

## Makefile helpers

```bash
make dev-up
make dev-up-debug
make dev-down
make prod-up
make prod-down
make test
```

## Branch workflow

- Integration branch: `dev`
- Production delivery branches: `release/*`
- Use topic branches (`feat/*`, `fix/*`, `chore/*`) and PRs

## Licensing

- Project license: Apache-2.0 (`LICENSE`)
- Third-party notices: `THIRD_PARTY_NOTICES.md`