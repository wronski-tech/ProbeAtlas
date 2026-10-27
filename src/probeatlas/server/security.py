"""Password hashing and opaque token handling.

* Passwords: scrypt (stdlib) with a per-password random salt.
* Tokens (sessions, API keys, agent and enrollment tokens): 256-bit random
  secrets with a type prefix. Only their SHA-256 digest is stored; a fast hash
  is appropriate because the secrets are high-entropy and not guessable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

PREFIX_SESSION = "pas"
PREFIX_API_KEY = "pak"
PREFIX_AGENT = "paa"
PREFIX_ENROLLMENT = "pae"
PREFIX_WEBHOOK_SECRET = "pah"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    salt_b64, digest_b64 = base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt_b64}${digest_b64}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = encoded.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    digest = hashlib.scrypt(
        password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), dklen=32
    )
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


# A precomputed hash so login timing does not reveal whether an email exists.
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def new_api_key() -> tuple[str, str]:
    """Return ``(display_prefix, full_key)``. The prefix identifies the key in UIs."""
    ident = secrets.token_hex(6)
    return f"{PREFIX_API_KEY}_{ident}", f"{PREFIX_API_KEY}_{ident}_{secrets.token_urlsafe(32)}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
