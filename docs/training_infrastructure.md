# Training infrastructure

This document covers the hardware setup, data transfer workflow, and adapter
deployment process for training LoRA adapters and fine-tuning STT models
outside the inference server.

## Hardware

Training runs on a dedicated machine equipped with two NVIDIA A100 PCIe
accelerators. This hardware is separate from the inference server (RTX 3090 Ti)
that runs the production voice pipeline. The separation is intentional: the
inference GPU has a strict 24 GB VRAM budget with no headroom for training
workloads, and running training alongside inference would degrade voice
assistant response times.

The A100 PCIe cards provide 40 or 80 GB of VRAM each depending on the variant,
which is sufficient for full-precision LoRA training on Qwen3-8B without
quantization or memory optimization tricks. For the default LoRA configuration
(rank 16, alpha 32, targeting dense projection layers), a single A100 handles
the training comfortably. The second GPU is available for parallel experiments
or larger batch sizes.

## Environment setup

The training machine needs Python 3.12 or newer, PyTorch with CUDA support,
and the project's training dependencies. From a clone of the repository, install
the training extras:

```bash
pip install -e '.[training]'
```

This pulls in transformers, peft, datasets, and their transitive dependencies.
The base model weights should be downloaded to the training machine separately
using the provided download script, since the training machine may not share
storage with the inference server:

```bash
scripts/download_llm.sh Qwen/Qwen3-8B /path/to/training/models/llm
```

## Data transfer workflow

Training data originates on the inference server, where the llm_proxy captures
every request-response interaction as JSONL files under `dev/datasets/llm/`.
These files need to be transferred to the training machine before they can be
used.

The transfer can be done with rsync, scp, or any file synchronization tool
that fits the network setup. A typical rsync command to pull new capture files
from the inference server looks like this:

```bash
rsync -avz inference-server:ha-voice-agent/dev/datasets/llm/ \
  /path/to/training/dev/datasets/llm/
```

Once the raw captures are on the training machine, convert them into the SFT
format expected by the training script:

```bash
python training/llm/prepare_dataset.py \
  --input-dir /path/to/training/dev/datasets/llm \
  --output /path/to/training/dev/datasets/meta/llm_sft.jsonl
```

## Training

With the prepared dataset and base model in place, launch the LoRA training
run. The configuration in `training/llm/lora_config.json` defines the adapter
rank, learning rate, number of epochs, and target modules. A dry-run validates
that the dataset and config are correctly wired before committing GPU time:

```bash
python training/llm/train_lora.py \
  --dataset /path/to/training/dev/datasets/meta/llm_sft.jsonl \
  --output-dir /path/to/training/adapters/qwen3_tooling_lora \
  --base-model /path/to/training/models/llm/Qwen3-8B \
  --config training/llm/lora_config.json \
  --dry-run
```

Remove the `--dry-run` flag to start the actual training run. The script
supports resuming from checkpoints via `--resume-from-checkpoint` if a run is
interrupted.

## Adapter deployment

After training completes, the adapter weights need to be copied back to the
inference server and placed where vLLM can find them. The adapter output
directory contains the LoRA weights and tokenizer artifacts.

```bash
rsync -avz /path/to/training/adapters/qwen3_tooling_lora/ \
  inference-server:ha-voice-agent/dev/models/llm/adapters/qwen3_tooling_lora/
```

To load the adapter in vLLM, the `--lora-modules` flag or the equivalent
configuration entry needs to point to the adapter directory. The exact
integration depends on the vLLM version and whether LoRA serving is enabled
in the compose configuration. As of vLLM v0.19.0, dynamic LoRA loading is
supported through the API, which means adapters can be swapped without
restarting the service.

## Evaluation

Before deploying a new adapter to production, run the evaluation scripts
against a held-out portion of the captured data to verify that tool-call
validity and compliance metrics have improved:

```bash
python training/eval/eval_tool_call_validity.py --dataset dev/datasets/llm
python training/eval/eval_two_step_compliance.py --dataset dev/datasets/llm
python training/eval/eval_latency_tokens.py --dataset dev/datasets/llm
```

Compare the results against the baseline metrics from before the adapter was
trained. The key metric is the tool-call JSON validity rate, which should
increase with each training iteration as the model learns to produce
well-formed tool arguments consistently.
