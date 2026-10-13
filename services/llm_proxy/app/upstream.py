"""Turn a client request into the request for the upstream server.

Clients such as Home Assistant send OpenAI parameters that other
OpenAI-compatible servers reject or do not need, e.g. `user`, which the
Mistral API refuses. The settings drop those, replace the client's model
alias with the upstream model and add provider options such as
`reasoning_effort`.
"""

import copy

import app.config as config


def build_payload(payload: dict, settings: config.Settings) -> dict:
    """Return the upstream request body; the client payload stays unchanged."""

    upstream = {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key not in settings.drop_params
    }
    if settings.upstream_model is not None:
        upstream["model"] = settings.upstream_model
    upstream.update(copy.deepcopy(settings.extra_body))
    return upstream


def headers(settings: config.Settings) -> dict[str, str]:
    """Headers for every upstream request; the client's never pass through."""

    if not settings.upstream_api_key:
        return {}
    return {"Authorization": f"Bearer {settings.upstream_api_key}"}
