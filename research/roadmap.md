<!--
Status legend: planned | in-progress | done | blocked

This file is intentionally verbose and operational. It is the single source of
truth for what we are building and how we validate it.
-->

# ha-voice-agent — Research Roadmap

## Repository / Branch Policy (binding)

- Integration branch: `dev` (treat as protected)
- Production branches: `release/*`
- All work happens on single-topic branches off latest `dev`.
- No secrets committed. Use `.env` / `.env.example`.

## Hard Constraints (must remain true)

1. Offline-first: runtime must not require cloud services.
2. VRAM budget: whole stack feasible on 24 GB VRAM (RTX 3090 Ti).
3. Separate Docker Compose setups for prod and dev.
4. Dev must capture prompts/responses/tool-calls/audio for dataset building.
5. No secrets committed.

## Target Directory Structure (must exist exactly)

```text
compose/
  prod/docker-compose.yml
  dev/docker-compose.yml
  dev/docker-compose.override.yml
services/
  llm_proxy/
scripts/
dev/
  models/{llm,stt,tts}
  cache/{hf,vllm}
  logs/
  datasets/{llm,stt,meta}
docs/
training/
THIRD_PARTY_NOTICES.md
```

## VRAM Budget (baseline target)

This table will be kept accurate as configs solidify.

| Component | Mode | Target VRAM | Notes |
|---|---:|---:|---|
| vLLM (Qwen2.5-7B-Instruct) | prod/dev | 14–18 GB | FP16/BF16; tuned `gpu_memory_utilization`; `max_model_len` 4096–8192 |
| KV cache | prod/dev | 2–5 GB | depends on context length + concurrency |
| Whisper STT (GPU optional) | dev/prod optional | 0–3 GB | default CPU; GPU option is selectable |
| TTS (Piper) | prod/dev | ~0 GB | CPU by default |
| Safety margin / overhead | prod/dev | 1–3 GB | driver/runtime overhead |

## Global Acceptance Tests (run repeatedly)

1. **Dev compose boot:**

   ```bash
   docker compose -f compose/dev/docker-compose.yml up -d
   ```

2. **Health checks:**
   - vLLM: `GET /health` => 200
   - llm_proxy: `GET /health` => 200
   - wyoming services: ports open

3. **Minimal LLM functional test:**

   ```bash
   curl -s http://localhost:<LLM_PROXY_PORT>/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"<served>","messages":[{"role":"user","content":"Say hi in one sentence."}]}'
   ```

4. **Tool-call JSON validity test:** send a tool schema + prompt requiring a
   tool call; response must contain JSON-valid arguments.

---

# Roadmap Items

## 0) Repo hygiene + baseline project scaffolding

- **Status:** planned
- **Goal:** establish correct repo structure, licensing, and no-secrets baseline.
- **Deliverables:**
  - `LICENSE` (Apache-2.0 unless conflict)
  - `.editorconfig`
  - `.gitignore` expanded (datasets/logs/cache/models downloads etc.)
  - `pyproject.toml` (llm_proxy + tooling)
  - `compose/**` moved to required layout (`compose/prod`, `compose/dev`)
  - `.env.example` (no secrets)
- **Acceptance:** `git status` clean after adding only intended tracked files.

## 1) Production stack (compose/prod)

- **Status:** planned
- **Goal:** minimal overhead, minimal logs, pinned images, restart policies,
  health checks.
- **Services:**
  - vLLM OpenAI-compatible endpoint (default model: Qwen/Qwen2.5-7B-Instruct)
  - Wyoming STT: `rhasspy/wyoming-whisper` (CPU default)
  - Wyoming TTS: `rhasspy/wyoming-piper`
- **Deliverables:** `compose/prod/docker-compose.yml`.
- **Acceptance:**
  - `docker compose -f compose/prod/docker-compose.yml up -d`
  - vLLM `/health` 200

## 2) Development stack (compose/dev + override)

- **Status:** planned
- **Goal:** adds `llm_proxy` and dataset capture volumes; optional heavy debug
  capture via `docker-compose.override.yml`.
- **Deliverables:**
  - `compose/dev/docker-compose.yml`
  - `compose/dev/docker-compose.override.yml`
  - dev volumes to `./dev/{logs,datasets,cache,models}`
- **Acceptance:**
  - dev compose boots
  - llm_proxy `/health` 200
  - can run curl chat completion through llm_proxy

## 3) llm_proxy service (FastAPI, OpenAI-compatible subset)

- **Status:** planned
- **Goal:** stabilize tool calling and capture training data.
- **Endpoints:**
  - `GET /health`
  - `GET /v1/models`
  - `POST /v1/chat/completions`
- **Features:**
  - pass-through to vLLM
  - tool-call JSON validation
  - single retry with JSON “repair” prompt if invalid
  - structured logging JSONL into `dev/datasets/llm/*.jsonl`
  - include request_id, timestamps, prompt/messages, tool schema, model output,
    tool calls, latency, token counts (if returned)
- **Acceptance:**
  - malformed tool JSON triggers exactly 1 repair attempt
  - JSONL logs written with stable schema

## 4) Scripts (models, smoke tests, utilities)

- **Status:** planned
- **Goal:** reproducible offline-first model acquisition + sanity checks.
- **Deliverables:**
  - `scripts/check_gpu_docker.sh`
  - `scripts/download_llm.sh`
  - `scripts/download_stt.sh`
  - `scripts/download_tts.sh`
  - `scripts/healthcheck_stack.sh` (optional)
- **Acceptance:**
  - `scripts/check_gpu_docker.sh` passes on target host
  - download scripts run without GPU and place models in correct locations

## 5) Dataset schema + capture

- **Status:** planned
- **Goal:** stable JSONL schemas under `dev/datasets/{llm,stt,meta}`.
- **Deliverables:**
  - `docs/training_pipeline.md` defines dataset schemas
  - llm_proxy writes LLM interaction JSONL
  - STT audio capture plan (either via HA integration notes or local proxying)
- **Acceptance:** sample dataset entries validate against schema definition.

## 6) Fine-tuning scaffolding (LLM LoRA/QLoRA)

- **Status:** planned
- **Goal:** reproducible scripts/configs for LoRA fine-tuning focusing on tool
  calling.
- **Deliverables:**
  - `training/llm/` scripts (Transformers + PEFT)
  - dataset preparation utilities
  - run instructions (CPU-only prep; GPU training on 3090 Ti)
- **Acceptance:** “dry run” training command starts and finds dataset + config.

## 7) STT adaptation placeholder

- **Status:** planned
- **Goal:** provide data-prep + evaluation scaffolding; full STT training is
  optional if too heavy.
- **Deliverables:**
  - `training/stt/` with placeholders and clear docs
  - evaluation harness that runs on captured STT dataset

## 8) Evaluation scripts

- **Status:** planned
- **Metrics:**
  - tool-call JSON validity rate
  - “two-step” device control compliance (discover_entities => perform_action)
  - latency + tokens/s smoke test
- **Deliverables:** `training/eval/*.py` (or similar) + docs.

## 9) Documentation (Home Assistant + MCP + operations)

- **Status:** planned
- **Deliverables:**
  - `docs/home_assistant_setup.md`
  - `docs/mcp_assist_setup.md`
  - `docs/training_pipeline.md`
  - README updated: diagram, VRAM budget, run prod/dev, dataset capture,
    fine-tuning entrypoints

## 10) Third-party notices

- **Status:** planned
- **Deliverables:** `THIRD_PARTY_NOTICES.md` (licenses + pinned images/models).

---

# Change Log (human maintained)

- 2026-02-16: initialized roadmap.
