#!/usr/bin/env python3
"""Offline tests for secrets_box — Fernet encryption of per-user secrets (ADR 0016).

Run with: python test_secrets_box.py
"""

import secrets_box as sb


def test_roundtrip_with_explicit_key():
    key = sb.generate_key()
    token = sb.encrypt("li_at=abc123; JSESSIONID=xyz", key=key)
    assert token != "li_at=abc123; JSESSIONID=xyz", "ciphertext must not be the plaintext"
    assert sb.decrypt(token, key=key) == "li_at=abc123; JSESSIONID=xyz"
    print("OK — encrypt/decrypt round-trips with an explicit key")


def test_roundtrip_via_env():
    import os

    key = sb.generate_key()
    saved = os.environ.get(sb.KEY_ENV)
    os.environ[sb.KEY_ENV] = key
    try:
        token = sb.encrypt("my-phantombuster-key")
        assert sb.decrypt(token) == "my-phantombuster-key"
    finally:
        if saved is None:
            os.environ.pop(sb.KEY_ENV, None)
        else:
            os.environ[sb.KEY_ENV] = saved
    print("OK — encrypt/decrypt use APP_SECRETS_KEY from the environment")


def test_fail_closed_without_key():
    import os

    saved = os.environ.pop(sb.KEY_ENV, None)
    try:
        try:
            sb.encrypt("secret")
            assert False, "encrypt must refuse without APP_SECRETS_KEY"
        except sb.SecretsKeyError:
            pass
    finally:
        if saved is not None:
            os.environ[sb.KEY_ENV] = saved
    print("OK — fail-closed: encrypting without a key raises SecretsKeyError")


def test_wrong_key_raises():
    token = sb.encrypt("secret", key=sb.generate_key())
    try:
        sb.decrypt(token, key=sb.generate_key())  # a different key
        assert False, "decrypt with the wrong key must raise"
    except sb.SecretsKeyError:
        pass
    # A malformed key is also rejected loudly.
    try:
        sb.encrypt("secret", key="not-a-valid-fernet-key")
        assert False, "a malformed key must raise"
    except sb.SecretsKeyError:
        pass
    print("OK — wrong/malformed key raises SecretsKeyError, not a silent wrong value")


if __name__ == "__main__":
    test_roundtrip_with_explicit_key()
    test_roundtrip_via_env()
    test_fail_closed_without_key()
    test_wrong_key_raises()
    print("\nAll secrets_box tests passed.")
