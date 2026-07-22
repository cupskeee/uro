"""OpenAI ChatGPT-subscription ("Codex") OAuth device-authorization flow + token lifecycle (D-47).

Adapter-layer (imports httpx): the core ring must never import this. This is the auth half of the
`codex` provider kind — a login against a consumer ChatGPT subscription via OpenAI's *device*
authorization endpoints (NOT the Codex-CLI localhost-PKCE flow, and NOT a paid API key). PKCE is
SERVER-managed here: the `code_verifier` comes back from the poll and is replayed into the exchange,
so this client generates no `code_challenge`.

⚠️ UNOFFICIAL / ToS: this drives Codex's public client id against a consumer subscription and
disguises inference as the ChatGPT web UI (Origin/Referer). It may breach OpenAI's terms — the same
posture as the reference it mirrors. Endpoints, client id and User-Agents are env-overridable.

The inference half (the Responses-API adapter that consumes the tokens) is `adapters/codex.py`.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

import httpx

# --- configuration (env-overridable; the baked defaults are the working reference values) --------

_ISSUER = os.getenv("URO_CODEX_OAUTH_ISSUER", "https://auth.openai.com").rstrip("/")
CLIENT_ID = os.getenv("URO_CODEX_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann").strip()
DEVICE_USERCODE_URL = f"{_ISSUER}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{_ISSUER}/api/accounts/deviceauth/token"
OAUTH_TOKEN_URL = f"{_ISSUER}/oauth/token"
# The token endpoint validates redirect_uri against the grant even though no browser redirect fires.
REDIRECT_URI = f"{_ISSUER}/deviceauth/callback"
DEFAULT_VERIFICATION_URI = f"{_ISSUER}/codex/device"
CODEX_BASE_URL = os.getenv("URO_CODEX_BASE_URL", "https://chatgpt.com/backend-api/codex").rstrip(
    "/"
)
# auth.openai.com sits behind Cloudflare, which 1010-bans default python UAs → send a browser UA for
# every OAuth call. The inference UA is separate (the backend is more permissive).
_OAUTH_UA = os.getenv(
    "URO_CODEX_OAUTH_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)
INFERENCE_UA = os.getenv("URO_CODEX_USER_AGENT", "uro Codex client")
# Fallback catalog; the real list comes from the /models endpoint once authenticated.
DEFAULT_MODELS = ["gpt-5", "gpt-5-codex"]
REFRESH_SKEW_SECONDS = 120


class CodexAuthError(Exception):
    """A hard, non-retriable failure in the Codex OAuth flow (e.g. a Cloudflare/WAF block)."""


class CodexReauthRequired(CodexAuthError):
    """The grant is dead (expired/denied/refresh rejected) — the operator must re-authorize."""


class CodexRateLimited(CodexAuthError):
    """The OAuth endpoint returned 429 — back off and retry the whole login later."""


def _oauth_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _OAUTH_UA,
    }


def codex_inference_headers(access_token: str) -> dict[str, str]:
    """Headers that disguise a Responses-API call as the Codex web UI. `Origin`/`Referer` are
    load-bearing (the backend gates on them). `chatgpt-account-id` is derived from the access
    token's OpenAI auth claim when present — the modern Codex backend requires it to route the
    request to the subscription's account (omitted if the token doesn't carry it)."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/codex",
        "User-Agent": INFERENCE_UA,
        "Authorization": f"Bearer {access_token}",
    }
    account_id = chatgpt_account_id(access_token)
    if account_id:
        headers["chatgpt-account-id"] = account_id
    return headers


async def request_device_code(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """Begin a login: ask OpenAI for a device code. Returns the short `user_code` to display, the
    `device_auth_id` to poll with, plus `verification_uri`/`interval`/`expires_in`."""
    try:
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
            resp = await client.post(
                DEVICE_USERCODE_URL, headers=_oauth_headers(), json={"client_id": CLIENT_ID}
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise CodexAuthError(f"device-code request failed: {exc}") from exc
    if not data.get("device_auth_id") or not data.get("user_code"):
        raise CodexAuthError("device-code response missing device_auth_id/user_code")
    return {
        "device_auth_id": str(data["device_auth_id"]),
        "user_code": str(data["user_code"]),
        "verification_uri": str(data.get("verification_uri") or DEFAULT_VERIFICATION_URI),
        "interval": int(data.get("interval") or 5),
        "expires_in": int(data.get("expires_in") or 900),
    }


async def poll_device_auth(
    device_auth_id: str,
    user_code: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any] | None:
    """Poll once. Return the approved payload ({authorization_code, code_verifier, ...}) when the
    user has authorized, or None while still pending. Raise on a terminal/blocked state."""
    try:
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
            resp = await client.post(
                DEVICE_TOKEN_URL,
                headers=_oauth_headers(),
                json={"device_auth_id": device_auth_id, "user_code": user_code},
            )
    except httpx.HTTPError as exc:
        raise CodexAuthError(f"device poll failed: {exc}") from exc
    if resp.status_code in (403, 404):
        body = resp.text.lower()
        if "cloudflare" in body or "error 10" in body:
            raise CodexAuthError("device poll blocked by Cloudflare (needs a browser User-Agent)")
        return None  # still pending — the user hasn't approved the code yet
    if resp.status_code == 429:
        return None  # rate-limited mid-poll → keep waiting
    if resp.status_code >= 400:
        raise CodexReauthRequired(f"device poll failed ({resp.status_code})")
    data: dict[str, Any] = resp.json()
    err = data.get("error")
    if err in ("expired_token", "access_denied", "denied"):
        raise CodexReauthRequired(f"device auth {err}")
    if not data.get("authorization_code") or not data.get("code_verifier"):
        return None  # no code yet → still pending
    return data


async def _token_request(
    body: dict[str, str], *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": _OAUTH_UA,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
            resp = await client.post(OAUTH_TOKEN_URL, headers=headers, data=body)
    except httpx.HTTPError as exc:
        raise CodexReauthRequired(f"token request failed: {exc}") from exc
    if resp.status_code == 429:
        raise CodexRateLimited("token endpoint rate-limited")
    if resp.status_code >= 400:
        raise CodexReauthRequired(f"token request failed ({resp.status_code})")
    data: dict[str, Any] = resp.json()
    if not data.get("access_token"):
        raise CodexReauthRequired("token response missing access_token")
    return data


async def exchange_code(
    authorization_code: str,
    code_verifier: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Turn the approved authorization_code (+ server-supplied code_verifier) into tokens."""
    return await _token_request(
        {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": code_verifier,
        },
        transport=transport,
    )


async def refresh_access_token(
    refresh_token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """Rotate an expiring/expired access token. The response may carry a new refresh_token."""
    return await _token_request(
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID},
        transport=transport,
    )


async def discover_codex_models(
    access_token: str,
    *,
    base_url: str = CODEX_BASE_URL,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, str]]:
    """List the subscription's available Codex models as `[{id, modality:"chat"}]` (all chat; the
    backend has no embedding models). Hidden models dropped, sorted by priority; falls back to the
    baked DEFAULT_MODELS on any failure."""
    try:
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
            resp = await client.get(
                f"{base_url.rstrip('/')}/models?client_version=1.0.0",
                headers=codex_inference_headers(access_token),
            )
            resp.raise_for_status()
            data = resp.json()
        raw = data.get("models") or []
        visible = [
            m
            for m in raw
            if isinstance(m, dict)
            and m.get("slug")
            and m.get("visibility") not in ("hide", "hidden")
        ]
        visible.sort(key=lambda m: (m.get("priority", 1_000_000), str(m["slug"])))
        models = [{"id": str(m["slug"]), "modality": "chat"} for m in visible]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        models = []
    return models or [{"id": s, "modality": "chat"} for s in DEFAULT_MODELS]


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT's payload claims, NO signature check (best-effort; {} on failure)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _decode_jwt_exp(token: str) -> int | None:
    """Read only the `exp` claim of a JWT without verifying the signature (best-effort)."""
    exp = _decode_jwt_claims(token).get("exp")
    try:
        return int(exp) if exp is not None else None
    except (TypeError, ValueError):
        return None


def chatgpt_account_id(access_token: str) -> str | None:
    """Best-effort: the ChatGPT account id from the access token's OpenAI auth claim, for the
    `chatgpt-account-id` header the Codex Responses backend requires. The claim lives under the
    namespaced `https://api.openai.com/auth` object (or, on some tokens, at the top level). None
    if the token doesn't carry it."""
    claims = _decode_jwt_claims(access_token)
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        acct = auth.get("chatgpt_account_id") or auth.get("user_id")
        if acct:
            return str(acct)
    acct = claims.get("chatgpt_account_id")
    return str(acct) if acct else None


def token_is_expiring(access_token: str, *, skew: int = REFRESH_SKEW_SECONDS) -> bool:
    """True if the access token expires within `skew` seconds — or if its `exp` can't be read (in
    which case refresh defensively rather than send a token that might already be dead)."""
    exp = _decode_jwt_exp(access_token)
    if exp is None:
        return True
    return exp <= time.time() + skew
