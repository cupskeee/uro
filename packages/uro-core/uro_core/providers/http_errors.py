"""A safe reason for a failed HTTP provider call (D-110).

The raw httpx exception text can carry a credential (an illegal-header error embeds the
Authorization value), so it is never shown or stored (D-47 review). What IS safe: the status code
and the provider's own error body — Anthropic's `{"error": {"type", "message"}}`, OpenAI's
`{"error": {"type"|"code", "message"}}`. The message is still scrubbed of the caller's key and
capped, in case a provider ever echoes part of the request.
"""

from __future__ import annotations

import json

import httpx

_MAX_MESSAGE = 300


def http_failure_detail(
    exc: httpx.HTTPError, *, secrets: tuple[str | None, ...] = ()
) -> str | None:
    """`HTTP 400 invalid_request_error: temperature is not supported…`, or None when the failure
    has no response (a connect error, a timeout) — the exception's type already says that much.
    The response body must have been read (a streamed response: `await resp.aread()` first)."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    resp = exc.response
    parts = [f"HTTP {resp.status_code}"]
    kind, message = _provider_error(resp)
    if kind:
        parts[0] += f" {kind}"
    if message:
        for secret in secrets:
            if secret and len(secret) >= 8:
                message = message.replace(secret, "[redacted]")
        if len(message) > _MAX_MESSAGE:
            message = message[:_MAX_MESSAGE] + "…"
        return f"{parts[0]}: {message}"
    return parts[0]


def _provider_error(resp: httpx.Response) -> tuple[str | None, str | None]:
    try:
        body = json.loads(resp.content or b"")
    except (ValueError, httpx.ResponseNotRead):
        return None, None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, str):  # some OpenAI-compatible servers: {"error": "message"}
        return None, _clean(err)
    if not isinstance(err, dict):
        return None, None
    kind = err.get("type") or err.get("code")
    message = err.get("message")
    return (
        _clean(kind) if isinstance(kind, str) else None,
        _clean(message) if isinstance(message, str) else None,
    )


def _clean(text: str) -> str:
    return " ".join(text.split())  # one line: no control characters or newlines into logs
