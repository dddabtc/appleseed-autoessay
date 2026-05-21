"""Explicit first-user bootstrap for native-password deployments."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autoessay.config import get_settings
from autoessay.db import SessionLocal
from autoessay.models import User, utcnow


def bootstrap_initial_admin(db_session: Session | None = None) -> bool:
    """Create the first password user only when explicit env config is set.

    Returns True when a user was created or an existing username-only row
    was given the configured password hash. Returns False when bootstrap
    is disabled or a password-capable user already exists.
    """

    settings = get_settings()
    password_hash = settings.initial_admin_password_hash
    if not password_hash:
        return False
    username = settings.initial_admin_username
    if not username:
        raise RuntimeError(
            "AUTOESSAY_INITIAL_ADMIN_USERNAME is required when "
            "AUTOESSAY_INITIAL_ADMIN_PASSWORD_HASH is set",
        )
    if not password_hash.startswith(("$2a$", "$2b$", "$2y$")):
        raise RuntimeError("AUTOESSAY_INITIAL_ADMIN_PASSWORD_HASH must be a bcrypt hash")

    owns_session = db_session is None
    session = db_session or SessionLocal()
    try:
        existing_password_users = session.scalar(
            select(func.count()).select_from(User).where(User.password_hash.is_not(None)),
        )
        if existing_password_users:
            return False

        user = session.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(
                id=f"initial_admin_{uuid4().hex[:16]}",
                username=username,
                display_name=settings.initial_admin_display_name,
                created_at=utcnow(),
            )
            session.add(user)
        user.password_hash = password_hash
        if user.display_name is None:
            user.display_name = settings.initial_admin_display_name
        if owns_session:
            session.commit()
        else:
            session.flush()
        return True
    finally:
        if owns_session:
            session.close()


__all__ = ["bootstrap_initial_admin"]
