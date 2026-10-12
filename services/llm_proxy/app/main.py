"""OpenAI-compatible LLM proxy service with tool-call JSON repair.

The upstream is any OpenAI-compatible server: vLLM in compose/dev, a cloud
provider on the Jetson (docs/jetson_setup.md, section 11). Clients see one
model alias, so the upstream model can change without touching them.
"""

import asyncio
import contextlib
import datetime
import hmac
import json
import logging
import os
import time
import typing
import uuid

import fastapi
import httpx

import app.capture as capture
import app.config as config
import app.limits as limits
import app.tool_calls as tool_calls
import app.upstream as upstream

LOGGER = logging.getLogger("llm_proxy")

# The OpenAI client libraries retry 401 and 429 answers unless told not to.
# A retry cannot change the proxy's own refusals.
_NO_RETRY = {"x-should-retry": "false"}


def _configure_logging(level: str) -> None:
    LOGGER.setLevel(level)
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        )
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)


def _error(
    status_code: int,
    message: str,
    error_type: str,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> fastapi.responses.JSONResponse:
    error: dict[str, typing.Any] = {"message": message, "type": error_type}
    if code is not None:
        error["code"] = code
    return fastapi.responses.JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers=headers,
    )


def _unauthorized(
    request: fastapi.Request, settings: config.Settings
) -> fastapi.responses.JSONResponse | None:
    """Refuse a request without the client key, if one is configured.

    With LLM_PROXY_REQUIRE_API_KEY a missing key refuses everything, so a
    forgotten .env entry never opens a paid upstream to the network.
    """

    if not settings.client_api_key:
        if not settings.require_client_key:
            return None
    else:
        scheme, _, token = request.headers.get("authorization", "").partition(
            " "
        )
        if scheme.lower() == "bearer" and hmac.compare_digest(
            token.strip().encode(), settings.client_api_key.encode()
        ):
            return None
    return _error(
        401,
        "Invalid API key for llm_proxy.",
        "invalid_request_error",
        code="invalid_api_key",
        headers=_NO_RETRY,
    )


def _count_tool_calls(response_body: dict) -> int:
    choices = response_body.get("choices")
    if not isinstance(choices, list) or not choices:
        return 0
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    calls = message.get("tool_calls") if isinstance(message, dict) else None
    return len(calls) if isinstance(calls, list) else 0


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


def create_app(
    settings: config.Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> fastapi.FastAPI:
    """Build the service; tests pass a transport that fakes the upstream."""

    _configure_logging(settings.log_level)
    daily_limit = limits.DailyLimit(settings.daily_request_limit)
    chat_url = f"{settings.upstream_base_url}/chat/completions"
    models_url = f"{settings.upstream_base_url}/models"

    @contextlib.asynccontextmanager
    async def lifespan(
        application: fastapi.FastAPI,
    ) -> typing.AsyncIterator[None]:
        # With a marker the drive's own tooling creates the directory.
        if settings.capture_enabled and settings.capture_marker is None:
            try:
                capture.ensure_directories(
                    [settings.capture_dir, settings.log_dir]
                )
            except OSError as exc:
                LOGGER.error("cannot create capture directories: %s", exc)
        timeout = httpx.Timeout(
            timeout=settings.upstream_timeout_seconds,
            connect=min(30.0, settings.upstream_timeout_seconds),
        )
        application.state.http_client = httpx.AsyncClient(
            timeout=timeout, transport=transport
        )
        LOGGER.info(
            "llm_proxy ready: upstream %s, model %s as %s",
            settings.upstream_base_url,
            settings.upstream_model or "from the request",
            settings.model_alias,
        )
        try:
            yield
        finally:
            await application.state.http_client.aclose()

    api = fastapi.FastAPI(
        title="ha-voice-agent llm_proxy",
        version="0.2.0",
        lifespan=lifespan,
    )

    def http_client() -> httpx.AsyncClient:
        return api.state.http_client

    def capture_state() -> str:
        if not settings.capture_enabled:
            return "disabled"
        reason = capture.unavailable_reason(
            settings.capture_dir, settings.capture_marker
        )
        return reason or "ready"

    @api.get("/health")
    async def health(
        check_upstream: bool = fastapi.Query(False, alias="upstream"),
    ) -> dict[str, typing.Any]:
        """Return the proxy's state; `?upstream=true` also asks the upstream."""

        body: dict[str, typing.Any] = {
            "status": "ok",
            "service": "llm_proxy",
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "upstream_url": settings.upstream_base_url,
            "upstream_model": settings.upstream_model,
            "model_alias": settings.model_alias,
            "requests_today": daily_limit.used,
            "daily_request_limit": settings.daily_request_limit,
            "capture": await asyncio.to_thread(capture_state),
        }
        if check_upstream:
            body["upstream_ok"] = False
            body["upstream_status_code"] = None
            try:
                response = await http_client().get(
                    models_url, headers=upstream.headers(settings), timeout=5.0
                )
                body["upstream_status_code"] = response.status_code
                body["upstream_ok"] = response.status_code == 200
            except httpx.HTTPError:
                pass
        return body

    @api.get("/v1/models", response_model=None)
    async def list_models(
        request: fastapi.Request,
    ) -> fastapi.responses.JSONResponse:
        """List the alias, or the upstream's models when there is none."""

        denied = _unauthorized(request, settings)
        if denied is not None:
            return denied
        alias_entry = {
            "id": settings.model_alias,
            "object": "model",
            "created": 0,
            "owned_by": "aivi",
        }
        if settings.alias_mode:
            return fastapi.responses.JSONResponse(
                status_code=200,
                content={"object": "list", "data": [alias_entry]},
            )
        try:
            response = await http_client().get(
                models_url, headers=upstream.headers(settings)
            )
            return fastapi.responses.JSONResponse(
                status_code=response.status_code,
                content=_decode_json(response),
            )
        except httpx.HTTPError as exc:
            fallback = {
                "object": "list",
                "data": [alias_entry],
                "warning": f"upstream unavailable: {exc}",
            }
            return fastapi.responses.JSONResponse(
                status_code=200, content=fallback
            )

    async def post_chat_completion(
        payload: dict,
    ) -> tuple[int, dict[str, typing.Any]]:
        try:
            response = await http_client().post(
                chat_url,
                json=upstream.build_payload(payload, settings),
                headers=upstream.headers(settings),
            )
        except httpx.TimeoutException as exc:
            LOGGER.warning("upstream timeout: %s", exc)
            return 504, {
                "error": {
                    "message": (
                        "Upstream request timed out in llm_proxy. Increase "
                        "LLM_PROXY_UPSTREAM_TIMEOUT_SECONDS if needed."
                    ),
                    "type": "upstream_timeout",
                    "upstream_url": chat_url,
                }
            }
        except httpx.HTTPError as exc:
            LOGGER.error("upstream request failed: %s", exc)
            return 502, {
                "error": {
                    "message": f"Upstream request failed in llm_proxy. {exc}",
                    "type": "upstream_http_error",
                    "upstream_url": chat_url,
                }
            }

        body = _decode_json(response)
        if response.status_code >= 400:
            error = body.get("error", body)
            message = error.get("message") if isinstance(error, dict) else None
            LOGGER.warning(
                "upstream answered %d: %s",
                response.status_code,
                str(message or error)[:300],
            )
        return response.status_code, body

    async def capture_if_enabled(record: dict[str, typing.Any]) -> None:
        if not settings.capture_enabled:
            return
        reason = await asyncio.to_thread(
            capture.unavailable_reason,
            settings.capture_dir,
            settings.capture_marker,
        )
        if reason is not None:
            LOGGER.info("capture skipped: %s", reason)
            return
        try:
            await asyncio.to_thread(
                capture.write_jsonl_record, settings.capture_dir, record
            )
        except OSError as exc:
            LOGGER.error("failed to write capture record: %s", exc)

    @api.post("/v1/chat/completions", response_model=None)
    async def chat_completions(
        request: fastapi.Request,
    ) -> fastapi.responses.JSONResponse:
        """Proxy chat completions with one-shot tool-call JSON repair."""

        denied = _unauthorized(request, settings)
        if denied is not None:
            return denied

        request_id = str(uuid.uuid4())
        started_at = time.perf_counter()
        request_timestamp = datetime.datetime.now(datetime.UTC).isoformat()

        try:
            payload = await request.json()
        except ValueError:
            return _error(
                400, "Request body must be JSON.", "invalid_request_error"
            )
        if not isinstance(payload, dict):
            return _error(
                400,
                "Request body must be a JSON object.",
                "invalid_request_error",
            )
        if payload.get("stream") is True:
            return _error(
                400,
                "Streaming is not supported by llm_proxy.",
                "invalid_request_error",
            )
        if not daily_limit.try_acquire():
            LOGGER.warning(
                "daily request limit of %d reached",
                settings.daily_request_limit,
            )
            return _error(
                429,
                f"Daily request limit of {settings.daily_request_limit} "
                "reached in llm_proxy.",
                "rate_limit_exceeded",
                code="daily_request_limit",
                headers=_NO_RETRY,
            )

        final_payload = payload
        retries_used = 0

        final_status, final_response_json = await post_chat_completion(payload)
        invalid_tool_calls = tool_calls.validate_tool_call_arguments(
            final_response_json
        )

        if invalid_tool_calls and settings.max_repair_retries > 0:
            retries_used = 1
            final_payload = tool_calls.build_repair_request(
                original_request=payload,
                invalid_response=final_response_json,
                problems=invalid_tool_calls,
            )
            final_status, final_response_json = await post_chat_completion(
                final_payload
            )

        latency_ms = round((time.perf_counter() - started_at) * 1000.0, 3)
        capture_record = {
            "request_id": request_id,
            "request_timestamp": request_timestamp,
            "response_timestamp": datetime.datetime.now(
                datetime.UTC
            ).isoformat(),
            "latency_ms": latency_ms,
            "upstream_url": chat_url,
            "client_model": payload.get("model"),
            "upstream_model": settings.upstream_model or payload.get("model"),
            "request": payload,
            "request_after_repair": final_payload if retries_used else None,
            "response": final_response_json,
            "response_status": final_status,
            "invalid_tool_calls": invalid_tool_calls,
            "repair_retry_used": retries_used,
            "host": os.getenv("HOSTNAME", "unknown"),
            "usage": final_response_json.get("usage"),
        }
        await capture_if_enabled(capture_record)
        LOGGER.info(
            "chat %s: status %d in %.0f ms, %d tool calls, repair %d",
            request_id[:8],
            final_status,
            latency_ms,
            _count_tool_calls(final_response_json),
            retries_used,
        )

        return fastapi.responses.JSONResponse(
            status_code=final_status,
            content=final_response_json,
        )

    return api


app = create_app(config.load_settings())
api = app
