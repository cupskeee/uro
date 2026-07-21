"""App-level secret encryption for at-rest provider credentials (D-47, docs/20).

Fernet (AES-128-CBC + HMAC, from `cryptography`) under a single key-encryption-key held in env
(`URO_SECRET_KEY`) that MUST live OUTSIDE the database — otherwise a backup / replica / log dump of
the encrypted column is defeated by the same dump. Fail-closed: encrypting without a key is refused,
so a misconfigured instance never silently persists plaintext.

Lives in the adapters ring (not the core), so importing `cryptography` here is allowed by the
hexagonal import contract.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

KEK_ENV = "URO_SECRET_KEY"


class SecretsUnavailable(RuntimeError):
    """The credential cipher can't operate — no/invalid `URO_SECRET_KEY`, or an undecryptable
    ciphertext. A RuntimeError so the CLI/`_run_async` renders it as a clean `error: …` line."""


def _cipher() -> Fernet:
    key = os.environ.get(KEK_ENV)
    if not key:
        raise SecretsUnavailable(
            f"{KEK_ENV} is not set — credential storage is disabled. Generate a key with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'` "
            f"and export it as {KEK_ENV} (keep it out of the database)."
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise SecretsUnavailable(
            f"{KEK_ENV} is not a valid Fernet key (expected a 32-byte url-safe base64 key)."
        ) from exc


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a credential for storage. Raises SecretsUnavailable if no valid KEK is configured."""
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored credential. Raises SecretsUnavailable on a missing/invalid KEK or if the
    ciphertext can't be decrypted with the current key (e.g. the KEK was rotated)."""
    try:
        return _cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise SecretsUnavailable(
            f"a stored credential could not be decrypted — is {KEK_ENV} the key it was made with?"
        ) from exc
