"""LoRA training entrypoint for Qwen3-8B tool-calling fine-tuning."""

import argparse
import functools
import json
import pathlib
import typing

import datasets
import peft
import torch
import transformers

DEFAULT_BASE_MODEL = "dev/models/llm/Qwen3-8B"
DEFAULT_CONFIG_PATH = "training/llm/lora_config.json"


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        description="Train a Qwen3 LoRA adapter for tool-calling behavior."
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="Path to prepared JSONL dataset",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Output directory for adapter checkpoints",
    )
    parser.add_argument(
        "--base-model",
        default=DEFAULT_BASE_MODEL,
        help="Base model identifier or local path",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Path to LoRA config JSON",
    )
    parser.add_argument(
        "--resume-from-checkpoint",
        default="",
        help="Optional checkpoint path for resume",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate setup and preprocessing without launching training",
    )
    return parser.parse_args()


def load_json(path: pathlib.Path) -> dict[str, typing.Any]:
    """Load a JSON file as dictionary."""

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected object in {path}")
    return data


def _read_bool(
    config: dict[str, typing.Any],
    key: str,
    default: bool,
) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"Expected boolean for config[{key!r}]")


def _read_int(
    config: dict[str, typing.Any],
    key: str,
    default: int,
) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Expected integer for config[{key!r}]")
    return value


def _read_float(
    config: dict[str, typing.Any],
    key: str,
    default: float,
) -> float:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Expected number for config[{key!r}]")
    return float(value)


def _read_string(
    config: dict[str, typing.Any],
    key: str,
    default: str,
) -> str:
    value = config.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"Expected string for config[{key!r}]")
    return value


def _read_string_list(
    config: dict[str, typing.Any],
    key: str,
    default: list[str],
) -> list[str]:
    value = config.get(key, default)
    if not isinstance(value, list):
        raise ValueError(f"Expected list for config[{key!r}]")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"Expected string list for config[{key!r}]")
    return value


def _format_messages(
    example: dict[str, typing.Any],
    tokenizer: transformers.PreTrainedTokenizerBase,
) -> dict[str, str]:
    messages = example.get("messages", [])
    if not isinstance(messages, list):
        return {"text": ""}

    try:
        text = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=False,
        )
    except (TypeError, ValueError):
        return {"text": ""}

    if not isinstance(text, str):
        return {"text": ""}

    return {"text": text}


def _has_text(example: dict[str, typing.Any]) -> bool:
    text = example.get("text", "")
    return isinstance(text, str) and bool(text)


def _tokenize_text(
    example: dict[str, typing.Any],
    tokenizer: transformers.PreTrainedTokenizerBase,
    max_seq_length: int,
) -> dict[str, list[int]]:
    text = example.get("text", "")
    if not isinstance(text, str) or not text:
        return {
            "input_ids": [],
            "attention_mask": [],
        }

    encoded = tokenizer(
        text,
        truncation=True,
        max_length=max_seq_length,
        padding=False,
    )
    input_ids = encoded.get("input_ids", [])
    attention_mask = encoded.get("attention_mask", [])
    if not isinstance(input_ids, list):
        input_ids = []
    if not isinstance(attention_mask, list):
        attention_mask = []

    return {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
    }


def _has_input_ids(example: dict[str, typing.Any]) -> bool:
    input_ids = example.get("input_ids", [])
    return isinstance(input_ids, list) and bool(input_ids)


def _load_training_dataset(
    dataset_path: pathlib.Path,
    tokenizer: transformers.PreTrainedTokenizerBase,
    max_seq_length: int,
) -> datasets.Dataset:
    dataset = datasets.load_dataset(
        "json",
        data_files=str(dataset_path),
        split="train",
    )

    formatted = dataset.map(
        functools.partial(_format_messages, tokenizer=tokenizer),
        remove_columns=dataset.column_names,
    )
    formatted = formatted.filter(_has_text)

    tokenized = formatted.map(
        functools.partial(
            _tokenize_text,
            tokenizer=tokenizer,
            max_seq_length=max_seq_length,
        ),
        remove_columns=formatted.column_names,
    )
    tokenized = tokenized.filter(_has_input_ids)
    if len(tokenized) == 0:
        raise ValueError("No training examples found after preprocessing.")
    return tokenized


def _resolve_torch_dtype(config: dict[str, typing.Any]) -> torch.dtype:
    use_bf16 = _read_bool(config, "bf16", True)
    use_fp16 = _read_bool(config, "fp16", False)
    if use_bf16 and use_fp16:
        raise ValueError("Only one of bf16 and fp16 can be enabled.")
    if use_bf16:
        return torch.bfloat16
    if use_fp16:
        return torch.float16
    return torch.float32


def _build_training_args(
    config: dict[str, typing.Any],
    output_dir: pathlib.Path,
) -> transformers.TrainingArguments:
    return transformers.TrainingArguments(
        output_dir=str(output_dir),
        overwrite_output_dir=True,
        num_train_epochs=_read_float(config, "num_train_epochs", 3.0),
        learning_rate=_read_float(config, "learning_rate", 2e-4),
        per_device_train_batch_size=_read_int(
            config,
            "per_device_train_batch_size",
            1,
        ),
        gradient_accumulation_steps=_read_int(
            config,
            "gradient_accumulation_steps",
            16,
        ),
        warmup_ratio=_read_float(config, "warmup_ratio", 0.03),
        weight_decay=_read_float(config, "weight_decay", 0.01),
        max_grad_norm=_read_float(config, "max_grad_norm", 1.0),
        max_steps=_read_int(config, "max_steps", -1),
        lr_scheduler_type=_read_string(config, "lr_scheduler_type", "cosine"),
        optim=_read_string(config, "optim", "adamw_torch"),
        bf16=_read_bool(config, "bf16", True),
        fp16=_read_bool(config, "fp16", False),
        gradient_checkpointing=_read_bool(
            config,
            "gradient_checkpointing",
            True,
        ),
        dataloader_num_workers=_read_int(config, "dataloader_num_workers", 0),
        logging_steps=_read_int(config, "logging_steps", 10),
        save_strategy="steps",
        save_steps=_read_int(config, "save_steps", 200),
        save_total_limit=_read_int(config, "save_total_limit", 2),
        report_to=[],
        remove_unused_columns=False,
    )


def _validate_config(config: dict[str, typing.Any]) -> None:
    if _read_bool(config, "qlora", False):
        raise ValueError(
            "Config requests qlora=true, but this script is configured for "
            "full-precision LoRA training."
        )
    target_modules = _read_string_list(
        config,
        "target_modules",
        [
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
    )
    if not target_modules:
        raise ValueError("LoRA target_modules must not be empty.")


def main() -> None:
    """Train a Qwen3-8B LoRA adapter on prepared JSONL messages."""

    args = parse_args()
    dataset_path = pathlib.Path(args.dataset)
    config_path = pathlib.Path(args.config)
    output_dir = pathlib.Path(args.output_dir)

    if not dataset_path.exists():
        raise FileNotFoundError(f"Dataset does not exist: {dataset_path}")
    if not config_path.exists():
        raise FileNotFoundError(f"Config does not exist: {config_path}")

    config = load_json(config_path)
    _validate_config(config)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not torch.cuda.is_available() and not args.dry_run:
        raise RuntimeError(
            "CUDA GPU is required for full training in this workflow."
        )

    tokenizer = transformers.AutoTokenizer.from_pretrained(
        args.base_model,
        trust_remote_code=True,
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    max_seq_length = _read_int(config, "max_seq_length", 4096)
    train_dataset = _load_training_dataset(
        dataset_path=dataset_path,
        tokenizer=tokenizer,
        max_seq_length=max_seq_length,
    )

    print("Qwen3 LoRA training")
    print(f"- base model: {args.base_model}")
    print(f"- dataset: {dataset_path}")
    print(f"- examples: {len(train_dataset)}")
    print(f"- output dir: {output_dir}")
    print(f"- config: {config_path}")

    if args.dry_run:
        model_config = transformers.AutoConfig.from_pretrained(
            args.base_model,
            trust_remote_code=True,
        )
        print(f"- model type: {model_config.model_type}")
        print("Dry run complete; no training executed.")
        return

    torch_dtype = _resolve_torch_dtype(config)
    model = transformers.AutoModelForCausalLM.from_pretrained(
        args.base_model,
        trust_remote_code=True,
        torch_dtype=torch_dtype,
    )
    model.config.use_cache = False

    lora_config = peft.LoraConfig(
        r=_read_int(config, "r", 16),
        lora_alpha=_read_int(config, "lora_alpha", 32),
        lora_dropout=_read_float(config, "lora_dropout", 0.05),
        target_modules=_read_string_list(
            config,
            "target_modules",
            [
                "q_proj",
                "k_proj",
                "v_proj",
                "o_proj",
                "gate_proj",
                "up_proj",
                "down_proj",
            ],
        ),
        bias="none",
        task_type=peft.TaskType.CAUSAL_LM,
    )
    model = peft.get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    training_args = _build_training_args(config, output_dir)
    data_collator = transformers.DataCollatorForLanguageModeling(
        tokenizer=tokenizer,
        mlm=False,
    )

    trainer = transformers.Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        data_collator=data_collator,
    )
    resume_from_checkpoint = args.resume_from_checkpoint or None
    trainer.train(resume_from_checkpoint=resume_from_checkpoint)
    trainer.save_model(str(output_dir))
    tokenizer.save_pretrained(str(output_dir))

    print(f"Training complete. Adapter saved to {output_dir}")


if __name__ == "__main__":
    main()
