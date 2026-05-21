"""bcrypt password hashing helpers for the native-password auth path
(PR-B; Casdoor replacement).

`hash_password` / `verify_password` wrap bcrypt at rounds=12, the
codex round-2 default. `dummy_verify` runs a no-op bcrypt verify on a
constant pre-computed hash so the response time on `username not
found` / `password_hash IS NULL` matches the success path — closes
the timing side-channel that lets an attacker enumerate which
usernames exist.

bcrypt input is truncated at 72 bytes (the algorithm's hard limit).
We reject inputs longer than that explicitly rather than silently
truncating, so a passphrase-length password fails loudly instead of
matching every prefix that hashes to the same suffix.
"""

from __future__ import annotations

import bcrypt

_BCRYPT_ROUNDS = 12
_BCRYPT_MAX_INPUT_BYTES = 72

# Pre-computed hash used by `dummy_verify` so unknown-user / NULL-hash
# branches still spend bcrypt CPU. Hash is for the literal string
# "dummy" — irrelevant since we only call verify with a non-matching
# password to consume the wall-clock cost.
_DUMMY_HASH = b"$2b$12$FYPHMR8eMt1iDztlWfSZ3.6qs5/cFwfV7uybzHkq6Ffh1JQyGF0Mi"


class PasswordTooLong(ValueError):
    """Raised when input exceeds bcrypt's 72-byte cap."""


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX_INPUT_BYTES:
        raise PasswordTooLong("password exceeds 72 bytes after UTF-8 encoding")
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        # Burn CPU even when the row has no hash so timing matches the
        # happy path. Caller is responsible for `return False` after
        # this returns.
        dummy_verify(password)
        return False
    encoded = password.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX_INPUT_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("ascii"))
    except ValueError:
        # Malformed hash on disk (e.g. a row migrated from a
        # different algorithm) — treat as auth failure rather than
        # 500ing.
        return False


def dummy_verify(password: str) -> None:
    encoded = password.encode("utf-8")[:_BCRYPT_MAX_INPUT_BYTES]
    try:
        bcrypt.checkpw(encoded, _DUMMY_HASH)
    except ValueError:
        return


__all__ = [
    "PasswordTooLong",
    "dummy_verify",
    "hash_password",
    "verify_password",
]
