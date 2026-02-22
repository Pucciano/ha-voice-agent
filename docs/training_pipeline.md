# Training pipeline

This document describes dataset capture, preparation, and evaluation workflows.

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

The stack provides two pathways:

1. **Network-level debug capture** (dev override):
   - `stt-packet-capture` writes pcap traces to `dev/logs/`.
2. **Structured dataset ingest:**
   - use `scripts/stt_capture_ingest.py` to store audio+transcript+metadata rows.

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
