"""PR-B: bcrypt wrapper unit tests."""

from __future__ import annotations

import time

import pytest

from autoessay.auth.passwords import (
    PasswordTooLong,
    dummy_verify,
    hash_password,
    verify_password,
)


def test_hash_then_verify_roundtrip() -> None:
    h = hash_password("local test password")
    assert h.startswith("$2b$")
    assert verify_password("local test password", h)


def test_verify_rejects_wrong_password() -> None:
    h = hash_password("correct horse battery staple")
    assert not verify_password("wrong", h)


def test_verify_returns_false_when_hash_is_none() -> None:
    assert not verify_password("anything", None)


def test_verify_returns_false_for_malformed_hash() -> None:
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_hash_rejects_inputs_over_72_bytes() -> None:
    with pytest.raises(PasswordTooLong):
        hash_password("a" * 73)


def test_dummy_verify_runs_bcrypt_work() -> None:
    """Spend at least a few ms on bcrypt so timing matches the
    happy path. Doesn't measure exact equality (CI noise is too high
    for that) — we only assert dummy_verify isn't a no-op.
    """

    started = time.perf_counter()
    dummy_verify("anything")
    elapsed = time.perf_counter() - started
    # rounds=12 bcrypt is 50-300ms on commodity hardware; even on
    # very fast CI we expect at least ~5ms.
    assert elapsed > 0.005
