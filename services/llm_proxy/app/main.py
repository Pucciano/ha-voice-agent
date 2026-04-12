"""OpenAI-compatible LLM proxy service with tool-call JSON repair."""

import contextlib
import datetime
import json
import logging
import os
import time
import typing
import uuid
import urllib.parse

import fastapi
import httpx

import app.capture as capture
import app.config as config
import app.tool_calls as tool_calls

SETTINGS = config.load_settings()
LOGGER = logging.getLogger("llm_proxy")
LOGGER.setLevel(SETTINGS.log_level)
if not LOGGER.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    handler.setFormatter(formatter)
    LOGGER.addHandler(handler)

APP_LIFESPAN_CLIENT_KEY = "http_client"

@contextlib.asynccontextmanager
async def _lifespan(
    application: fastapi.FastAPI,
) -> typing.AsyncIterator[None]:
    """Initialize and cleanup shared resources for the API service."""

    capture.ensure_directories([SETTINGS.capture_dir, SETTINGS.log_dir])
    timeout = httpx.Timeout(
        timeout=SETTINGS.upstream_timeout_seconds,
        connect=min(30.0, SETTINGS.upstream_timeout_seconds),
    )
    application.state.http_client = httpx.AsyncClient(timeout=timeout)
    try:
        yield
    finally:
        client = getattr(
            application.state,
            APP_LIFESPAN_CLIENT_KEY,
            None,
        )
        if isinstance(client, httpx.AsyncClient):
            await client.aclose()


api = fastapi.FastAPI(
    title="ha-voice-agent llm_proxy",
    version="0.1.0",
    lifespan=_lifespan,
)
app = api


@api.get("/health")
async def health() -> dict[str, typing.Any]:
    """Return basic service health and upstream availability state."""

    upstream_ok = False
    upstream_status_code: int | None = None
    client = _http_client()
    health_url = _upstream_health_url()
    try:
        response = await client.get(health_url, timeout=5.0)
        upstream_status_code = response.status_code
        upstream_ok = response.status_code == 200
    except httpx.HTTPError:
        upstream_ok = False

    return {
        "status": "ok",
        "service": "llm_proxy",
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "upstream_ok": upstream_ok,
        "upstream_status_code": upstream_status_code,
        "upstream_health_url": health_url,
    }


@api.get("/v1/models")
async def list_models() -> fastapi.responses.JSONResponse:
    """Proxy model listing endpoint to vLLM."""

    client = _http_client()
    upstream_url = f"{SETTINGS.vllm_base_url}/models"
    try:
        response = await client.get(upstream_url)
        payload = _decode_json(response)
        return fastapi.responses.JSONResponse(
            status_code=response.status_code,
            content=payload,
        )
    except httpx.HTTPError as exc:
        fallback = {
            "object": "list",
            "data": [
                {
                    "id": SETTINGS.served_model_name,
                    "object": "model",
                    "owned_by": "local",
                }
            ],
            "warning": f"upstream unavailable: {exc}",
        }
        return fastapi.responses.JSONResponse(status_code=200, content=fallback)


@api.post("/v1/chat/completions")
async def chat_completions(
    request: fastapi.Request,
) -> fastapi.responses.JSONResponse:
    """Proxy chat completions with one-shot tool-call JSON repair."""

    request_id = str(uuid.uuid4())
    started_at = time.perf_counter()
    request_timestamp = datetime.datetime.now(datetime.UTC).isoformat()

    payload = await request.json()
    if not isinstance(payload, dict):
        raise fastapi.HTTPException(
            status_code=400,
            detail="Request body must be a JSON object.",
        )

    if payload.get("stream") is True:
        raise fastapi.HTTPException(
            status_code=400,
            detail="Streaming is not supported by llm_proxy capture mode.",
        )

    final_payload = payload
    retries_used = 0
    invalid_tool_calls: list[dict[str, typing.Any]] = []

    response_status, response_json = await _post_chat_completion(payload)
    final_status = response_status
    final_response_json = response_json
    invalid_tool_calls = tool_calls.validate_tool_call_arguments(response_json)

    if invalid_tool_calls and SETTINGS.max_repair_retries > 0:
        retries_used = 1
        final_payload = tool_calls.build_repair_request(
            original_request=payload,
            invalid_response=response_json,
            problems=invalid_tool_calls,
        )
        response_status, response_json = await _post_chat_completion(
            final_payload
        )
        final_status = response_status
        final_response_json = response_json

    latency_ms = round((time.perf_counter() - started_at) * 1000.0, 3)
    capture_record = {
        "request_id": request_id,
        "request_timestamp": request_timestamp,
        "response_timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "latency_ms": latency_ms,
        "upstream_url": f"{SETTINGS.vllm_base_url}/chat/completions",
        "request": payload,
        "request_after_repair": final_payload if retries_used else None,
        "response": final_response_json,
        "response_status": final_status,
        "invalid_tool_calls": invalid_tool_calls,
        "repair_retry_used": retries_used,
        "host": os.getenv("HOSTNAME", "unknown"),
        "usage": final_response_json.get("usage"),
    }
    _capture_if_enabled(capture_record)

    return fastapi.responses.JSONResponse(
        status_code=final_status,
        content=final_response_json,
    )


async def _post_chat_completion(
    payload: dict,
) -> tuple[int, dict[str, typing.Any]]:
    client = _http_client()
    try:
        response = await client.post(
            f"{SETTINGS.vllm_base_url}/chat/completions",
            json=payload,
        )
    except httpx.TimeoutException as exc:
        LOGGER.warning(
            "upstream timeout while requesting chat completion: %s",
            exc,
        )
        return 504, {
            "error": {
                "message": (
                    "Upstream vLLM request timed out in llm_proxy. "
                    "Increase LLM_PROXY_UPSTREAM_TIMEOUT_SECONDS if needed."
                ),
                "type": "upstream_timeout",
                "upstream_url": (
                    f"{SETTINGS.vllm_base_url}/chat/completions"
                ),
            }
        }
    except httpx.HTTPError as exc:
        LOGGER.error("upstream request failed in llm_proxy: %s", exc)
        return 502, {
            "error": {
                "message": (
                    "Upstream vLLM request failed in llm_proxy. "
                    f"{exc}"
                ),
                "type": "upstream_http_error",
                "upstream_url": (
                    f"{SETTINGS.vllm_base_url}/chat/completions"
                ),
            }
        }

    return response.status_code, _decode_json(response)


def _decode_json(response: httpx.Response) -> dict[str, typing.Any]:
    text = response.text
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return {"raw": parsed}
    except json.JSONDecodeError:
        return {
            "error": {
                "message": "Upstream response was not valid JSON.",
                "status_code": response.status_code,
                "raw": text,
            }
        }


def _http_client() -> httpx.AsyncClient:
    client = getattr(api.state, APP_LIFESPAN_CLIENT_KEY, None)
    if not isinstance(client, httpx.AsyncClient):
        raise fastapi.HTTPException(
            status_code=500,
            detail="HTTP client is not initialized.",
        )
    return client


def _capture_if_enabled(record: dict[str, typing.Any]) -> None:
    if not SETTINGS.capture_enabled:
        return
    try:
        capture.write_jsonl_record(SETTINGS.capture_dir, record)
    except OSError as exc:
        LOGGER.error("failed to write capture record: %s", exc)


def _upstream_health_url() -> str:
    """Return the expected vLLM /health URL based on configured /v1 base URL."""

    parsed = urllib.parse.urlsplit(SETTINGS.vllm_base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        health_path = f"{path[:-3]}/health"
    else:
        health_path = f"{path}/health"
    rebuilt = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, health_path, "", "")
    )
    return rebuilt
