"""Configuration loading for llm_proxy."""

import dataclasses
import os
import pathlib


@dataclasses.dataclass(frozen=True)
class Settings:
    """Runtime settings for the proxy service."""

    vllm_base_url: str
    served_model_name: str
    log_level: str
    capture_enabled: bool
    capture_dir: pathlib.Path
    log_dir: pathlib.Path
    max_repair_retries: int
    upstream_timeout_seconds: float


def _to_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def load_settings() -> Settings:
    """Load settings from process environment variables."""

    capture_dir_value = os.getenv("LLM_PROXY_CAPTURE_DIR", "/datasets/llm")
    log_dir_value = os.getenv("LLM_PROXY_LOG_DIR", "/logs")
    max_retries_text = os.getenv("LLM_PROXY_MAX_REPAIR_RETRIES", "1")
    upstream_timeout_text = os.getenv(
        "LLM_PROXY_UPSTREAM_TIMEOUT_SECONDS",
        "600",
    )
    try:
        max_retries = max(0, int(max_retries_text))
    except ValueError:
        max_retries = 1
    try:
        upstream_timeout_seconds = max(1.0, float(upstream_timeout_text))
    except ValueError:
        upstream_timeout_seconds = 600.0

    return Settings(
        vllm_base_url=os.getenv("VLLM_BASE_URL", "http://vllm:8000/v1"),
        served_model_name=os.getenv(
            "VLLM_SERVED_MODEL_NAME",
            "qwen2.5-7b-instruct",
        ),
        log_level=os.getenv("LLM_PROXY_LOG_LEVEL", "INFO").upper(),
        capture_enabled=_to_bool(
            os.getenv("LLM_PROXY_ENABLE_CAPTURE"),
            default=True,
        ),
        capture_dir=pathlib.Path(capture_dir_value),
        log_dir=pathlib.Path(log_dir_value),
        max_repair_retries=max_retries,
        upstream_timeout_seconds=upstream_timeout_seconds,
    )
