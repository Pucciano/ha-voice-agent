"""Configuration loading for llm_proxy."""

import dataclasses
import json
import os
import pathlib


@dataclasses.dataclass(frozen=True)
class Settings:
    """Runtime settings for the proxy service.

    The two keys are left out of the repr, so a logged settings object never
    shows them.
    """

    upstream_base_url: str
    upstream_api_key: str = dataclasses.field(repr=False)
    upstream_model: str | None
    model_alias: str
    client_api_key: str = dataclasses.field(repr=False)
    require_client_key: bool
    drop_params: frozenset[str]
    extra_body: dict
    daily_request_limit: int
    log_level: str
    capture_enabled: bool
    capture_dir: pathlib.Path
    capture_marker: pathlib.Path | None
    log_dir: pathlib.Path
    max_repair_retries: int
    upstream_timeout_seconds: float
    unavailable_message: str

    @property
    def alias_mode(self) -> bool:
        """True when the proxy maps its alias to a fixed upstream model."""

        return self.upstream_model is not None


def _to_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def _to_int(value: str | None, default: int, minimum: int) -> int:
    if value is None:
        return default
    try:
        return max(minimum, int(value))
    except ValueError:
        return default


def _extra_body(value: str | None) -> dict:
    if not value or not value.strip():
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("LLM_UPSTREAM_EXTRA_BODY must be a JSON object")
    return parsed


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Load settings from process environment variables.

    The LLM_UPSTREAM_* names select any OpenAI-compatible server. The older
    VLLM_* names still work for the vLLM stacks in compose/dev and
    compose/prod.
    """

    env = os.environ if environ is None else environ
    upstream_timeout_text = env.get("LLM_PROXY_UPSTREAM_TIMEOUT_SECONDS", "600")
    try:
        upstream_timeout_seconds = max(1.0, float(upstream_timeout_text))
    except ValueError:
        upstream_timeout_seconds = 600.0
    marker = env.get("LLM_PROXY_CAPTURE_MARKER", "").strip()

    return Settings(
        upstream_base_url=env.get(
            "LLM_UPSTREAM_BASE_URL",
            env.get("VLLM_BASE_URL", "http://vllm:8000/v1"),
        ).rstrip("/"),
        upstream_api_key=env.get("LLM_UPSTREAM_API_KEY", ""),
        upstream_model=env.get("LLM_UPSTREAM_MODEL", "").strip() or None,
        model_alias=env.get(
            "LLM_PROXY_MODEL_ALIAS",
            env.get("VLLM_SERVED_MODEL_NAME", "aivi-8b-instruct"),
        ),
        client_api_key=env.get("LLM_PROXY_API_KEY", ""),
        require_client_key=_to_bool(
            env.get("LLM_PROXY_REQUIRE_API_KEY"), default=False
        ),
        drop_params=frozenset(
            name.strip()
            for name in env.get("LLM_UPSTREAM_DROP_PARAMS", "").split(",")
            if name.strip()
        ),
        extra_body=_extra_body(env.get("LLM_UPSTREAM_EXTRA_BODY")),
        daily_request_limit=_to_int(
            env.get("LLM_PROXY_DAILY_REQUEST_LIMIT"), default=0, minimum=0
        ),
        log_level=env.get("LLM_PROXY_LOG_LEVEL", "INFO").upper(),
        capture_enabled=_to_bool(
            env.get("LLM_PROXY_ENABLE_CAPTURE"),
            default=True,
        ),
        capture_dir=pathlib.Path(
            env.get("LLM_PROXY_CAPTURE_DIR", "/datasets/llm")
        ),
        capture_marker=pathlib.Path(marker) if marker else None,
        log_dir=pathlib.Path(env.get("LLM_PROXY_LOG_DIR", "/logs")),
        max_repair_retries=_to_int(
            env.get("LLM_PROXY_MAX_REPAIR_RETRIES"), default=1, minimum=0
        ),
        upstream_timeout_seconds=upstream_timeout_seconds,
        unavailable_message=env.get(
            "LLM_PROXY_UNAVAILABLE_MESSAGE",
            "The language model is not reachable.",
        ),
    )
