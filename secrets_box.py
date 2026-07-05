"""At-rest encryption for per-user secrets — the Phantombuster API key a `User`
runs phantoms under (ADR 0016, the per-user model from ADR 0012). The LinkedIn
session itself is not stored here; Phantombuster manages it (ADR 0016 amendment
2026-07-05).

Fernet (AES-128-CBC + HMAC-SHA256) keyed off the `APP_SECRETS_KEY` env var.
**Fail-closed:** handling a secret without a configured key raises, so a real
credential can never be written in plaintext by accident. Generate a key with:

    python -m secrets_box keygen     # prints a value for APP_SECRETS_KEY

This is the minimal real secrets-management story the per-user model needs; key
rotation, per-environment keys, and KMS-backed storage are a later concern
(deferred — see docs/plans/linkedin-ingestion.md).
"""

from __future__ import annotations

import os
import sys

from cryptography.fernet import Fernet, InvalidToken

KEY_ENV = "APP_SECRETS_KEY"


class SecretsKeyError(RuntimeError):
    """`APP_SECRETS_KEY` is missing or malformed, or ciphertext doesn't match it."""


def _fernet(key: str | None = None) -> Fernet:
    raw = key if key is not None else os.getenv(KEY_ENV)
    if not raw:
        raise SecretsKeyError(
            f"{KEY_ENV} is not set — refusing to handle a secret without an "
            "encryption key. Generate one with `python -m secrets_box keygen` "
            f"and set {KEY_ENV}."
        )
    try:
        return Fernet(raw.encode() if isinstance(raw, str) else raw)
    except (ValueError, TypeError) as err:
        raise SecretsKeyError(f"{KEY_ENV} is not a valid Fernet key: {err}") from err


def encrypt(plaintext: str, key: str | None = None) -> str:
    """Encrypt a secret to a URL-safe token. Raises SecretsKeyError if no key."""
    return _fernet(key).encrypt(plaintext.encode()).decode()


def decrypt(token: str, key: str | None = None) -> str:
    """Decrypt a token produced by `encrypt`. Raises SecretsKeyError if the key is
    missing/wrong or the token is corrupt."""
    try:
        return _fernet(key).decrypt(token.encode()).decode()
    except InvalidToken as err:
        raise SecretsKeyError(
            "ciphertext could not be decrypted — wrong key or corrupt token"
        ) from err


def generate_key() -> str:
    """A fresh Fernet key, suitable as the value of APP_SECRETS_KEY."""
    return Fernet.generate_key().decode()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="secrets_box", description="Per-user secret encryption helpers."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen", help=f"print a new {KEY_ENV} value")
    args = parser.parse_args(argv)
    if args.cmd == "keygen":
        print(generate_key())
    return 0


if __name__ == "__main__":
    sys.exit(main())
