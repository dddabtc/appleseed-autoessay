from __future__ import annotations

import pytest
from sqlalchemy import select

from autoessay.auth.bootstrap import bootstrap_initial_admin
from autoessay.auth.passwords import hash_password, verify_password
from autoessay.config import get_settings
from autoessay.models import User


def test_bootstrap_initial_admin_disabled_without_hash(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTOESSAY_INITIAL_ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("AUTOESSAY_INITIAL_ADMIN_USERNAME", raising=False)
    get_settings.cache_clear()

    with app_session() as session:
        assert bootstrap_initial_admin(session) is False
        assert session.scalar(select(User)) is None


def test_bootstrap_initial_admin_requires_explicit_username(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_INITIAL_ADMIN_PASSWORD_HASH", hash_password("local setup"))
    monkeypatch.delenv("AUTOESSAY_INITIAL_ADMIN_USERNAME", raising=False)
    get_settings.cache_clear()

    with app_session() as session, pytest.raises(RuntimeError):
        bootstrap_initial_admin(session)


def test_bootstrap_initial_admin_creates_first_password_user(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password_hash = hash_password("local setup")
    monkeypatch.setenv("AUTOESSAY_INITIAL_ADMIN_USERNAME", "first-user")
    monkeypatch.setenv("AUTOESSAY_INITIAL_ADMIN_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("AUTOESSAY_INITIAL_ADMIN_DISPLAY_NAME", "First User")
    get_settings.cache_clear()

    with app_session() as session:
        assert bootstrap_initial_admin(session) is True
        user = session.scalar(select(User).where(User.username == "first-user"))
        assert user is not None
        assert user.display_name == "First User"
        assert verify_password("local setup", user.password_hash)
        assert bootstrap_initial_admin(session) is False
