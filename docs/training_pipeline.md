# Training pipeline

This document describes dataset capture, preparation, and evaluation workflows.

Note that all training runs on dedicated A100 hardware, not on the inference
server. The inference GPU (RTX 3090 Ti, 24 GB VRAM) does not have headroom for
training alongside the runtime stack. Data is captured on the inference server
during normal operation, transferred to the training machine, and the resulting
adapters are deployed back. The full infrastructure setup including hardware
details, environment configuration, and the data transfer workflow is described
in `docs/training_infrastructure.md`.

## 1) Dataset layout

```text
dev/datasets/
  llm/   # llm_proxy JSONL captures
  stt/   # audio/transcript/metadata samples
  meta/  # extra diagnostics/exports
```

## 2) LLM capture schema (JSONL)

Each line in `dev/datasets/llm/llm_capture_YYYY-MM-DD.jsonl` includes:

- `request_id`
- `request_timestamp`
- `response_timestamp`
- `latency_ms`
- `request` (system/user/tool schema payload)
- `request_after_repair` (if repair retry used)
- `response` (model output)
- `invalid_tool_calls`
- `repair_retry_used`
- `usage`

This supports auditing and supervised fine-tuning conversion.

## 3) STT capture workflow

The stack provides three pathways:

1. **Network-level debug capture** (dev override):
   - `stt-packet-capture` writes pcap traces to `dev/logs/`.
2. **Structured dataset ingest:**
   - use `scripts/stt_capture_ingest.py` to store audio+transcript+metadata rows.
     The transcript is given by a person, so these samples count as reviewed
     (`verified: true`, `label_source: manual`).
3. **Request capture on the Jetson** (`services/stt_capture`):
   - while a satellite's switch `Anfragen aufzeichnen` is on, every voice
     request is stored on the Jetson's USB drive in the same layout, with the
     recogniser's text as a pseudo label (`verified: false`). Setup, fetching
     and the review steps are in `docs/jetson_setup.md`, section 9.

Example ingest:

```bash
python scripts/stt_capture_ingest.py \
  --audio sample.wav \
  --transcript "turn on kitchen lights" \
  --speaker user_a
```

## 4) LLM dataset preparation

Convert captures into SFT-ready JSONL:

```bash
python training/llm/prepare_dataset.py \
  --input-dir dev/datasets/llm \
  --output dev/datasets/meta/llm_sft.jsonl
```

## 5) Qwen3-8B LoRA training (CUDA)

Install training dependencies:

```bash
pip install -e '.[training]'
```

If needed, download the base model to the default local path:

```bash
scripts/download_llm.sh Qwen/Qwen3-8B dev/models/llm
```

Dry-run setup validation:

```bash
python training/llm/train_lora.py \
  --dataset dev/datasets/meta/llm_sft.jsonl \
  --output-dir dev/models/llm/adapters/qwen3_tooling_lora \
  --base-model dev/models/llm/Qwen3-8B \
  --config training/llm/lora_config.json \
  --dry-run
```

Run full-precision LoRA training:

```bash
python training/llm/train_lora.py \
  --dataset dev/datasets/meta/llm_sft.jsonl \
  --output-dir dev/models/llm/adapters/qwen3_tooling_lora \
  --base-model dev/models/llm/Qwen3-8B \
  --config training/llm/lora_config.json
```

The training output directory includes LoRA adapter weights and tokenizer
artifacts that can be reused for downstream evaluation/inference.

## 6) STT adaptation placeholder

Only reviewed samples (`verified: true` in `metadata.json`) are training
labels. A captured transcript is what Speech-to-Phrase recognised, chosen from
known sentences; empty and wrong results are review material, not labels.
`--include-unverified` adds unreviewed samples for review or evaluation only.

Create STT manifest:

```bash
python training/stt/prepare_dataset.py \
  --input-dir dev/datasets/stt \
  --output dev/datasets/meta/stt_manifest.jsonl
```

Evaluate manifest quality:

```bash
python training/stt/evaluate_manifest.py \
  --manifest dev/datasets/meta/stt_manifest.jsonl
```

## 7) Evaluation scripts

- Tool JSON validity:

```bash
python training/eval/eval_tool_call_validity.py --dataset dev/datasets/llm
```

- Two-step compliance:

```bash
python training/eval/eval_two_step_compliance.py --dataset dev/datasets/llm
```

- Latency and tokens/s:

```bash
python training/eval/eval_latency_tokens.py --dataset dev/datasets/llm
```

## 8) Reproducibility notes

- Keep model and image versions pinned.
- Store training configs in `training/llm/`.
- Never commit secrets; keep tokens in `.env` only.
