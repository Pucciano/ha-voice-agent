# ha-voice-agent

A fully local, GPU-accelerated, open-source voice agent for Home Assistant and beyond.

The goal of this project is to build a **self-hosted voice assistant stack** consisting of:

- A local LLM served via vLLM
- Local Speech-to-Text (STT)
- Local Text-to-Speech (TTS)
- Tool integration (Home Assistant MCP, calendar, email, web, etc.)
- Integrated training data collection
- Fine-tuning pipeline for continuous improvement

**Hardware target:**  
The complete stack (LLM + STT + TTS + inference) must run within **24 GB of GPU VRAM** (e.g., RTX 3090 Ti).

---

# 1. Architecture

## 1.1 High-Level Overview

```

User (Microphone)
↓
STT (Whisper / faster-whisper)
↓
LLM (vLLM OpenAI-compatible API)
↓
Tool Layer (MCP / external APIs)
↓
TTS (Piper)
↓
Audio Output

```

All components run containerized (Docker).

---

# 2. Technology Stack

## 2.1 LLM Layer

### vLLM

- High-performance GPU inference engine
- OpenAI-compatible REST API
- Efficient KV cache management
- Tool calling support
- Optimized for batching and low-latency inference

**License:** Apache License 2.0

### Supported Models (Target: <24 GB VRAM)

Primary candidates:

| Model | Parameters | Notes |
|--------|------------|--------|
| Qwen2.5-7B-Instruct | 7B | Strong tool-calling performance |
| Mistral-7B-Instruct | 7B | Fast and lightweight |
| Llama 3 8B | 8B | Balanced general-purpose model |

Recommended configuration:

- `dtype: float16`
- `max_model_len: 4096`
- `gpu_memory_utilization: 0.80–0.88`
- Prefix caching enabled
- Proper tool parser configuration

---

## 2.2 STT Layer

### faster-whisper

- Based on CTranslate2
- GPU acceleration supported
- Multilingual
- Low VRAM footprint with INT8 models

Recommended models:

- `medium-int8`
- `small-int8`

**License:** MIT License

Typical GPU memory usage:
- ~2–3 GB

---

## 2.3 TTS Layer

### Piper (Wyoming-Piper)

- ONNX-based speech synthesis
- Extremely lightweight
- Typically CPU-based (GPU optional)

**License:** MIT License

TTS does not significantly impact GPU memory unless explicitly configured to do so.

---

## 2.4 Tool Layer

### Home Assistant MCP

- Device discovery
- perform_action
- get_entity_details
- Script and automation execution

### Extensible Tool Integrations

The architecture allows extension to:

- IMAP / SMTP (email interaction)
- CalDAV / Google Calendar
- Web search
- RAG systems (Qdrant / pgvector)
- Infrastructure APIs (Proxmox, NAS, etc.)

---

# 3. VRAM Budgeting (24 GB Target)

Example allocation:

| Component | VRAM |
|------------|--------|
| LLM (7B FP16) | 14–18 GB |
| KV Cache | 2–3 GB |
| Whisper (GPU INT8) | 2–3 GB |
| Overhead | 1–2 GB |

Total: < 24 GB

Optimization strategies:

- Reduce `max_model_len`
- Tune `gpu_memory_utilization`
- Use INT8 Whisper
- Keep TTS on CPU
- Disable unnecessary logging in production

---

# 4. Project Structure

```

ha-voice-agent/
├── compose/
│   ├── docker-compose.yml
│   ├── docker-compose.dev.override.yml
│   └── docker-compose.prod.override.yml
│
├── models/
│   ├── llm/
│   ├── stt/
│   └── tts/
│
├── dev/
│   ├── logs/
│   ├── debug/
│   ├── datasets/
│   │   ├── llm/
│   │   ├── stt/
│   │   └── meta/
│   └── training/
│       ├── llm/
│       └── stt/
│
├── scripts/
│   ├── download_models.sh
│   ├── export_training_data.py
│   ├── prepare_llm_dataset.py
│   └── train_llm_lora.py
│
└── README.md

```

---

# 5. Development vs Production

## 5.1 Development Mode

Purpose:

- Full logging
- Storage of:
  - Prompts
  - Responses
  - Tool calls
  - Errors
  - Audio streams
- Dataset generation

Output stored under:

```

dev/logs/
dev/datasets/
dev/debug/

```

Startup:

```

docker compose 
-f compose/docker-compose.yml 
-f compose/docker-compose.dev.override.yml 
up -d

```

---

## 5.2 Production Mode

Purpose:

- Minimal logging
- No audio dumps
- Optimized GPU utilization
- Stable operation

Startup:

```

docker compose 
-f compose/docker-compose.yml 
-f compose/docker-compose.prod.override.yml 
up -d

```

Production deployments must always run from the `release` branch.

---

# 6. Training Data Collection

## 6.1 LLM Training Data

Stored as JSONL:

```

{
"messages": [
{"role": "system", "content": "..."},
{"role": "user", "content": "..."},
{"role": "assistant", "content": "..."},
{"role": "tool", "content": "..."}
]
}

```

Collected examples include:

- Successful tool calls
- Failed tool calls
- JSON parsing failures
- MCP misuse
- Hallucinations

Objectives:

- Improve tool reliability
- Eliminate JSONDecodeError issues
- Enforce deterministic behavior

---

## 6.2 STT Training Data

Format:

```

audio.wav
transcript.txt
metadata.json

```

Focus areas:

- Room names
- Device names
- Proper nouns
- Accents and dialects

---

# 7. Training Pipeline

## 7.1 LLM Fine-Tuning

- LoRA-based fine-tuning
- Optional QLoRA (4-bit)
- Hugging Face Transformers
- PEFT framework

Workflow:

1. Prepare dataset
2. Launch training script
3. Save adapter weights
4. Load adapter into vLLM

Adapters allow improved tool stability without retraining the full base model.

---

## 7.2 Whisper Fine-Tuning

- Optional Hugging Face-based fine-tuning
- Export to CTranslate2 format
- Focus on smart-home vocabulary and environment-specific commands

---

# 8. Git Workflow

## Branch Strategy

| Branch | Purpose |
|--------|----------|
| dev | Active development |
| release | Production-ready state |
| feat/* | New features |
| fix/* | Bug fixes |

Production systems must always deploy from `release`.

---

# 9. Security Principles

- Fully local operation
- No mandatory cloud dependencies
- No telemetry
- Local network isolation
- Optional API key support
- Optional reverse proxy

---

# 10. Open Source Licensing

This project integrates multiple open-source components. Their licenses must be respected.

| Project | License |
|----------|----------|
| vLLM | Apache License 2.0 |
| faster-whisper | MIT License |
| Piper | MIT License |
| Transformers | Apache License 2.0 |
| PEFT | Apache License 2.0 |

The ha-voice-agent project itself is open-source and must remain compatible with these upstream licenses.

---

# 11. Roadmap

Phase 1:
- Stable MCP workflow
- Tool-calling optimization (Qwen / Mistral)
- GPU-enabled Whisper

Phase 2:
- Structured dataset pipeline
- Automated data export
- LoRA adapter integration

Phase 3:
- Multi-tool agent
- Calendar integration
- Email integration

Phase 4:
- Persistent memory layer
- Multi-agent architecture
- Automated retraining cycles

---

# 12. Vision

A fully local, GPU-optimized, open-source voice agent with:

- Deterministic tool calling
- Structured error logging
- Continuous self-improvement
- Reproducible infrastructure
- < 24 GB VRAM total requirement

The objective is a stable, extensible, production-ready local voice agent architecture.
