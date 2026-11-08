"""Anthropic Messages API provider (docs/04).

Different shape from OpenAI: the system prompt is a top-level field (not a message),
`max_tokens` is required, and streaming uses Anthropic's own SSE event types. No
embeddings endpoint — embed() raises, and the router binds an embedding-capable
provider to the `embedder` role instead.

Current models refuse two things older ones took (D-110): sampling parameters (`temperature`;
a 400 on Opus 4.7+, Sonnet 5+, Fable, and non-default values on Sonnet/Haiku 5.5) and an
assistant prefill (a 400 from the 4.6 generation on). Both are sent only to the models known to
accept them (`_takes_sampling`, `_takes_prefill`); any other id — a new model, an alias, a proxy's
name — gets neither, which every model accepts. JSON mode without a prefill relies on the prompt
plus the callers' own `{…}` slicing. A binding's reasoning effort goes out as
`output_config.effort`.

Structured outputs: when a caller passes a JSON Schema (the planner's and extractor's) and the
model is one Anthropic lists as taking `output_config.format` (`_takes_format`), the schema goes
out in the strict form the API accepts (`strict_schema`) and no prefill is sent (the two don't
combine). If the API still refuses the format with a 400, the call is retried once without it and
this adapter stops sending it, so a schema the API won't compile costs one failed call, not a beat.

Thinking: the models that think by default (`_thinks_by_default`: every 5.x) spend thinking tokens
out of `max_tokens`, so a stage's cap sized for its answer alone can end in a truncated answer.
Those models get `_THINKING_HEADROOM` on top of the stage's cap. `max_tokens` is a ceiling, not a
charge: only the tokens produced are billed.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from uro_core.errors import ProviderError
from uro_core.providers.base import CompletionRequest, Message, report_usage
from uro_core.providers.http_errors import http_failure_detail

_VERSION = "2023-06-01"

# Ids of the models that still take `temperature` (D-110): Claude 2/3, and the 4.x line up to 4.6
# (`claude-sonnet-4-20250514`, `claude-opus-4-1`, `claude-haiku-4-5`, `claude-sonnet-4-6`, …).
# Opus 4.7/4.8 and every 5.x refuse it. Pinned to the old generations on purpose: a model this list
# doesn't know is newer, and omitting the parameter is valid on every model.
_SAMPLING_MODELS = re.compile(
    r"^claude-(instant|2|3)|^claude-(opus|sonnet|haiku)-4(-[0-6])?(-\d{8})?$"
)
# Ids that still take an assistant prefill: the same line minus 4.6 (Opus/Sonnet 4.6 refuse it).
_PREFILL_MODELS = re.compile(
    r"^claude-(instant|2|3)|^claude-(opus|sonnet|haiku)-4(-[0-5])?(-\d{8})?$"
)


# Ids that take structured outputs (`output_config.format`): Anthropic's list is Fable/Mythos 5+,
# Opus 5+, Opus 4.8, Sonnet 5+, Haiku 5+, Haiku 4.5, and the legacy Opus 4.5 and 4.1. A positive
# list, unlike the two above: here the safe default for an unknown id is to NOT send the field.
_FORMAT_MODELS = re.compile(
    r"^claude-(fable|mythos|opus|sonnet|haiku)-([5-9]|\d{2,})(-|$)"
    r"|^claude-opus-4-(8|5|1)(-|$)|^claude-haiku-4-5(-|$)"
)
# Ids that think when `thinking` is omitted: the 5.x line (Opus 4.8/4.7 don't, unless asked).
_THINKING_MODELS = re.compile(r"^claude-(fable|mythos|opus|sonnet|haiku)-([5-9]|\d{2,})(-|$)")
# What a thinking-by-default model gets on top of a stage's own cap. Sized for the engine's short
# structured stages and narration at the default effort; it keeps a non-streamed call well under
# the request timeout.
_THINKING_HEADROOM = 12_000

# JSON Schema string formats the API accepts; any other keyword it can't enforce moves into the
# description, where the model can still read it.
_STRING_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)


def _takes_format(model: str) -> bool:
    return bool(_FORMAT_MODELS.search(model))


def _thinks_by_default(model: str) -> bool:
    return bool(_THINKING_MODELS.search(model))


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A JSON Schema (Pydantic's) in the form Anthropic's structured outputs accept: every object
    closed (`additionalProperties: false`), `oneOf` as `anyOf`, and each keyword the API can't
    enforce (`default`, length and number bounds, unknown string formats) moved into the
    description. `required` is kept as given, so optional fields stay optional."""
    src = dict(schema)
    out: dict[str, Any] = {}
    defs = src.pop("$defs", None)
    if defs is not None:
        out["$defs"] = {name: strict_schema(d) for name, d in defs.items()}
    ref = src.pop("$ref", None)
    if ref is not None:
        out["$ref"] = ref
        return out
    kind = src.pop("type", None)
    variants = src.pop("anyOf", None) or src.pop("oneOf", None)
    all_of = src.pop("allOf", None)
    if isinstance(variants, list):
        out["anyOf"] = [strict_schema(v) for v in variants]
    elif isinstance(all_of, list):
        out["allOf"] = [strict_schema(v) for v in all_of]
    elif kind is not None:
        out["type"] = kind
    for key in ("enum", "const", "description", "title"):
        if key in src:
            out[key] = src.pop(key)
    if kind == "object":
        out["properties"] = {k: strict_schema(v) for k, v in src.pop("properties", {}).items()}
        src.pop("additionalProperties", None)
        out["additionalProperties"] = False
        if "required" in src:
            out["required"] = src.pop("required")
    elif kind == "string":
        fmt = src.pop("format", None)
        if fmt in _STRING_FORMATS:
            out["format"] = fmt
        elif fmt:
            src["format"] = fmt
    elif kind == "array":
        if "items" in src:
            out["items"] = strict_schema(src.pop("items"))
        if src.get("minItems") in (0, 1):
            out["minItems"] = src.pop("minItems")
    if src:  # what the API can't enforce, the model can still read
        rest = ", ".join(f"{k}: {json.dumps(v)}" for k, v in src.items())
        out["description"] = (
            f"{out['description']}\n\n{{{rest}}}" if "description" in out else (f"{{{rest}}}")
        )
    return out


def _takes_sampling(model: str) -> bool:
    return bool(_SAMPLING_MODELS.search(model))


def _takes_prefill(model: str) -> bool:
    return bool(_PREFILL_MODELS.search(model))


logger = logging.getLogger(__name__)


def _split(messages: list[Message]) -> tuple[str | None, list[dict[str, str]]]:
    """Anthropic wants the system prompt separate from the user/assistant turns."""
    system = "\n\n".join(m.content for m in messages if m.role == "system") or None
    convo = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    return system, convo


class AnthropicProvider:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        max_tokens: int = 2048,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._model = model
        self._reasoning_effort = reasoning_effort  # D-110: output_config.effort; None = default
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._transport = transport
        self._format_refused = False  # set once the API 400s a structured-output request

    @property
    def model(self) -> str:
        """The model id this adapter calls — read by `ProviderRouter.model_for` for metering."""
        return self._model

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _VERSION,
            "content-type": "application/json",
        }

    def _body(
        self, req: CompletionRequest, *, stream: bool, convo: list[dict[str, str]] | None = None
    ) -> dict[str, object]:
        system, split_convo = _split(req.messages)
        max_tokens = req.max_tokens or self._max_tokens
        if _thinks_by_default(self._model):  # thinking is spent out of max_tokens
            max_tokens += _THINKING_HEADROOM
        body: dict[str, object] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": convo if convo is not None else split_convo,
            "stream": stream,
        }
        if _takes_sampling(self._model):
            body["temperature"] = req.temperature
        output_config: dict[str, object] = {}
        if self._reasoning_effort:
            output_config["effort"] = self._reasoning_effort
        if self._sends_format(req):
            assert req.json_schema is not None
            output_config["format"] = {
                "type": "json_schema",
                "schema": strict_schema(req.json_schema),
            }
        if output_config:
            body["output_config"] = output_config
        if system is not None:
            body["system"] = system
        return body

    def _sends_format(self, req: CompletionRequest) -> bool:
        return (
            req.json_mode
            and req.json_schema is not None
            and not self._format_refused
            and _takes_format(self._model)
        )

    def _failed(self, exc: httpx.HTTPError) -> ProviderError:
        detail = http_failure_detail(exc, secrets=(self._api_key,))
        # The message names only the exception type and the safe detail — never `exc`'s own text.
        msg = f"anthropic request failed ({type(exc).__name__})"
        return ProviderError(f"{msg}: {detail}" if detail else msg, detail=detail)

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}/v1/messages",
                    headers=self._headers(),
                    json=self._body(req, stream=True),
                ) as resp,
            ):
                if resp.is_error:
                    await resp.aread()  # so the error body can say why (D-110)
                resp.raise_for_status()
                tokens_in: int | None = None
                tokens_out: int | None = None
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data:
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    kind = event.get("type")
                    if kind == "message_start":  # input tokens arrive up front (D-66)
                        usage = (event.get("message") or {}).get("usage") or {}
                        tokens_in = usage.get("input_tokens", tokens_in)
                    if isinstance(event.get("usage"), dict):  # output: cumulative, keep the last
                        tokens_out = event["usage"].get("output_tokens", tokens_out)
                    if kind == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield delta["text"]
                    elif kind == "message_delta":
                        # carries stop_reason; a max_tokens cutoff is a normal 200 stream.
                        if (event.get("delta") or {}).get("stop_reason") == "max_tokens":
                            logger.warning("anthropic narration truncated at max_tokens")
                    elif kind == "message_stop":
                        break
                    elif kind == "error":
                        err = event.get("error") or {}
                        raise ProviderError(f"anthropic stream error: {err.get('message', err)}")
                report_usage(tokens_in=tokens_in, tokens_out=tokens_out)
        except httpx.HTTPError as exc:
            raise self._failed(exc) from exc

    async def complete(self, req: CompletionRequest) -> str:
        if self._sends_format(req):
            try:
                return await self._complete(req)
            except ProviderError as exc:
                if exc.detail is None or not exc.detail.startswith("HTTP 400"):
                    raise
                # The API wouldn't take the request: try once as plain JSON mode. If that works,
                # the schema was the cause, and this adapter stops sending one; if it fails too,
                # something else was wrong, and the schema stays.
                self._format_refused = True
                try:
                    raw = await self._complete(req)
                except ProviderError:
                    self._format_refused = False
                    raise
                logger.warning(
                    "anthropic refused a structured-output request (%s); answered without the "
                    "schema, and not sending one again from this connection",
                    exc.detail,
                )
                return raw
        return await self._complete(req)

    async def _complete(self, req: CompletionRequest) -> str:
        prefill = ""
        convo: list[dict[str, str]] | None = None
        if req.json_mode and not self._sends_format(req) and _takes_prefill(self._model):
            # Anthropic has no response_format; prefill an assistant "{" to force JSON. Newer
            # models refuse a prefill (D-110) and rely on the prompt + the caller's `{…}` slicing.
            _, base = _split(req.messages)
            convo = [*base, {"role": "assistant", "content": "{"}]
            prefill = "{"
        body = self._body(req, stream=False, convo=convo)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(
                    f"{self._base_url}/v1/messages", headers=self._headers(), json=body
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise self._failed(exc) from exc
        except (ValueError, TypeError) as exc:  # a 200 with a non-JSON body
            raise ProviderError(f"anthropic returned an unparseable body: {exc}") from exc
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):  # D-66
            report_usage(tokens_in=usage.get("input_tokens"), tokens_out=usage.get("output_tokens"))
        try:
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        except (AttributeError, TypeError, KeyError) as exc:
            raise ProviderError(f"anthropic returned an unexpected body shape: {exc}") from exc
        if data.get("stop_reason") == "max_tokens":
            # truncated output → likely invalid JSON; fail loudly so the caller degrades — carrying
            # the text, so the metering router still stores what came back (D-66 review).
            raise ProviderError(
                "anthropic response hit max_tokens (output truncated)",
                partial_response=prefill + text,
            )
        return prefill + text

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise ProviderError(
            "anthropic has no embeddings endpoint; bind the 'embedder' role to another provider"
        )
